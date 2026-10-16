"""Second Brain Gateway: приём записей и превращение их в заметки по схеме.

Запуск: uvicorn app.main:create_app --factory
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from . import ada as ada_module
from . import ask as ask_module
from . import review as review_module
from . import scheduler as scheduler_module
from . import snapshot as snapshot_module
from . import stt as stt_module
from . import links as links_module
from . import people as people_module
from . import llm as llm_module
from .config import Settings, load_settings
from .db import Database
from .telegram import handle_update
from .service import build_private_response, build_response
from .textutil import find_urls, is_valid_capture_id, split_private
from .vault import load_notes

logger = logging.getLogger("second_brain")

SUPPORTED_TYPES = ("text", "url")


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # лишние поля (file_base64 и т.п.) игнорируем

    capture_id: str | None = None
    client: str = "ios"
    input_type: str = "text"
    content: str = ""
    revision: int = 1
    user_clarifications: str = ""
    context: str | None = None            # необязательно: профиль из хранилища (перекрывает сохранённый на сервере)
    known_people: list[dict] | None = None


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    role: str = ""
    aliases: list[str] = []


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    context: str | None = None
    people: list[PersonIn] | None = None


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = ""
    corpus: str = ""
    client: str = "ios"
    mode: str = "day"
    person: str = ""


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = limit
        self.window = window_seconds
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self.window:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


def clean_context(text: str | None, settings: Settings) -> str:
    return (text or "").strip()[: settings.max_context_chars]


def clean_people(items) -> list[dict]:
    result, seen = [], set()
    for item in items or []:
        try:
            person = PersonIn.model_validate(item if isinstance(item, dict) else {"name": str(item)})
        except ValidationError:
            continue
        name = person.name.strip()[:80]
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            "name": name,
            "role": person.role.strip()[:80],
            "aliases": [a.strip()[:60] for a in person.aliases if a.strip()][:8],
        })
    return result[:2000]


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"status": "error", "detail": detail}, status_code=status)


def create_app(
    settings: Settings | None = None,
    llm: Callable[..., Awaitable] | None = None,
    clock: Callable[[], datetime] | None = None,
    link_fetcher: Callable[..., Awaitable] | None = None,
    transcriber: Callable[..., Awaitable] | None = None,
    ask_llm: Callable[..., Awaitable] | None = None,
    expand_llm: Callable[..., Awaitable] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    transcriber_given = transcriber is not None
    llm = llm or llm_module.structure_capture
    link_fetcher = link_fetcher or links_module.fetch_link_info
    ask_llm = ask_llm or ask_module.ask_llm
    expand_llm = expand_llm or ask_module.expand_query
    transcriber = transcriber or (lambda file_id: stt_module.transcribe(settings, file_id))
    clock = clock or (lambda: datetime.now(ZoneInfo(settings.timezone)))

    db = Database(settings.data_dir / "brain.db", ttl_hours=settings.response_ttl_hours,
                  outbox_ttl_days=settings.outbox_ttl_days)
    db.init()
    limiter = RateLimiter(settings.rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app):
        task = None
        if settings.telegram_bot_token and settings.telegram_allowed_ids:
            task = asyncio.create_task(scheduler_module.run_loop(
                db, settings, clock, scheduler_module.telegram_sender(settings)))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Second Brain Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def process(
        *, capture_id: str, client: str, content: str, revision: int, clarifications: str,
        context: str | None = None, known: list[dict] | None = None,
    ) -> dict:
        """Превращает запись в заметки. Общая часть для /capture и Telegram."""
        # Режим «без ИИ»: текст никуда не уходит и не кэшируется
        private, private_text = split_private(content)
        if private:
            logger.info("capture id=%s private chars=%d", capture_id, len(private_text))
            return build_private_response(capture_id=capture_id, client=client, text=private_text, now=clock())

        # Идемпотентность: повторный запрос возвращает прежний ответ
        cached = db.get(capture_id, revision)
        if cached:
            return cached

        now = clock()
        if context is None:
            context = db.get_context()
        if known is None:
            known = db.known_people()

        # Названия по ссылкам, затем ИИ. Любой сбой даёт сохранение «как есть».
        urls = find_urls(content)
        link_infos = await link_fetcher(urls, settings) if urls else []
        result = await llm(
            settings,
            content=content,
            link_context=links_module.describe_for_llm(link_infos),
            clarifications=clarifications,
            revision=revision,
            now=now,
            context=context,
            people_text=people_module.describe_for_llm(known),
        )

        response = build_response(
            capture_id=capture_id,
            revision=revision,
            client=client,
            content=content,
            clarifications=clarifications,
            result=result,
            links=link_infos,
            content_urls=urls,
            now=now,
            max_notes=settings.max_notes,
            known_people=known,
        )
        logger.info(
            "capture id=%s rev=%d notes=%d types=%s fallback=%s chars=%d",
            capture_id, revision, response["count"],
            ",".join(f["type"] for f in response["files"]), response["fallback"], len(content),
        )

        db.save(capture_id=capture_id, revision=revision, response=response)
        if not response.get("needs_review") or revision != 1:
            for f in response.get("files", []):
                db.snapshot_put(snapshot_module.rows_from_markdown(f["markdown"]), settings.snapshot_max_rows)
        # Если параллельный дубль успел записаться раньше — отдаём первый ответ
        return db.get(capture_id, revision) or response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/capture")
    async def capture(request: Request):
        # 1. Авторизация — до чтения тела запроса
        provided = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(provided.encode("utf-8"), settings.capture_token.encode("utf-8")):
            return _error(401, "Неверный или отсутствующий x-api-key")

        if not limiter.allow():
            return _error(429, "Слишком много запросов, попробуйте через минуту")

        # 2. Разбор и проверка тела
        raw = await request.body()
        if len(raw) > settings.max_content_chars * 4 + 4096:
            return _error(413, "Запрос слишком большой")
        try:
            payload = CaptureRequest.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return _error(422, "Тело запроса должно быть корректным JSON с полями вроде input_type и content")

        if payload.input_type not in SUPPORTED_TYPES:
            return _error(422, "Пока поддерживаются только input_type: text и url (голос и фото превращает в текст сам шорткат)")
        if payload.revision not in (1, 2):
            return _error(422, "revision должен быть 1 или 2")

        content = payload.content.strip()
        if not content:
            return _error(422, "Поле content пустое")
        if len(content) > settings.max_content_chars:
            return _error(413, f"Заметка длиннее {settings.max_content_chars} символов")

        capture_id = (payload.capture_id or "").strip()
        if capture_id:
            if not is_valid_capture_id(capture_id):
                return _error(422, "capture_id: 8–64 символа, только латиница, цифры, - и _")
        else:
            capture_id = f"srv-{int(time.time() * 1000)}"

        # 3. Режим «без ИИ»: пустой текст после «!» — ошибка
        private, private_text = split_private(content)
        if private and not private_text:
            return _error(422, "После «!» или «секрет:» нет текста")
        return await process(
            capture_id=capture_id, client=payload.client, content=content,
            revision=payload.revision, clarifications=payload.user_clarifications,
            context=clean_context(payload.context, settings) if payload.context is not None else None,
            known=clean_people(payload.known_people) if payload.known_people is not None else None,
        )

    def authorized(request: Request) -> bool:
        provided = request.headers.get("x-api-key", "")
        return hmac.compare_digest(provided.encode("utf-8"), settings.capture_token.encode("utf-8"))

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        if not settings.telegram_secret:
            return _error(404, "Not found")
        provided = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(provided.encode("utf-8"), settings.telegram_secret.encode("utf-8")):
            return _error(401, "Bad secret")
        raw = await request.body()
        try:
            update = json.loads(raw)
        except ValueError:
            return {}
        try:
            reply = await handle_update(update, settings=settings, db=db, process=process, secretary=secretary, clock=clock,
                                       transcriber=transcriber if stt_module.configured(settings) or transcriber_given else None)
        except Exception as exc:  # noqa: BLE001 — Telegram не должен засыпать нас повторами
            logger.error("telegram: ошибка обработки (%s)", type(exc).__name__)
            message = update.get("message") if isinstance(update, dict) else None
            chat_id = ((message or {}).get("chat") or {}).get("id")
            if isinstance(chat_id, int) and settings.telegram_allowed_ids:
                return {"method": "sendMessage", "chat_id": chat_id,
                        "text": "Что-то пошло не так. Пришли сообщение ещё раз."}
            return {}
        return reply or {}

    @app.get("/profile")
    async def profile_get(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        return {"status": "success", "context": db.get_context(), "people": db.known_people()}

    @app.post("/profile")
    async def profile_set(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        raw = await request.body()
        if len(raw) > 600_000:
            return _error(413, "Слишком большой запрос")
        try:
            payload = ProfileIn.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return _error(422, "Тело запроса: {\"context\": \"текст\", \"people\": [{\"name\": \"Серик\", \"role\": \"друг\"}]}")
        if payload.context is not None:
            db.set_context(clean_context(payload.context, settings))
        if payload.people is not None:
            db.replace_people(clean_people([p.model_dump() for p in payload.people]))
        return {"status": "success", "context_chars": len(db.get_context()), "people": len(db.known_people())}

    @app.delete("/profile")
    async def profile_delete(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        db.clear_profile()
        return {"status": "success"}

    @app.get("/pending")
    async def pending(request: Request, limit: int = 50, service: int = 0):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        with_service = bool(service)               # служебные записи отдаём только скрипту ноутбука (?service=1)
        items = db.pending(max(1, min(limit, 200)), service=with_service)
        return {
            "status": "success",
            "count": len(items),
            "remaining": db.pending_count(service=with_service),
            "items": [
                {"id": i["id"], "kind": i["kind"], "filename": i["filename"], "folder": i["folder"], "markdown": i["markdown"],
                 "reminders": json.loads(i["reminders"] or "[]")}
                for i in items
            ],
        }

    @app.post("/ack")
    async def ack(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        try:
            ids = json.loads(await request.body()).get("ids", [])
            if not isinstance(ids, list):
                raise ValueError
            ids = [int(i) for i in ids]
        except (ValueError, TypeError, AttributeError):
            return _error(422, "Тело запроса: {\"ids\": [1, 2, 3]}")
        return {"status": "success", "removed": db.ack(ids)}

    async def read_vault_request(request: Request, need_question: bool):
        """Общая часть /ask и /digest. Возвращает (ответ-ошибка | None, запрос, заметки, число приватных)."""
        provided = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(provided.encode("utf-8"), settings.capture_token.encode("utf-8")):
            return _error(401, "Неверный или отсутствующий x-api-key"), None, None, 0
        if not limiter.allow():
            return _error(429, "Слишком много запросов, попробуйте через минуту"), None, None, 0
        raw = await request.body()
        if len(raw) > settings.max_corpus_chars * 3 + 4096:
            return _error(413, "Слишком много заметок в одном запросе"), None, None, 0
        try:
            payload = AskRequest.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return _error(422, "Тело запроса должно быть корректным JSON с полями question и corpus"), None, None, 0
        if need_question:
            question = payload.question.strip()
            if not question:
                return _error(422, "Поле question пустое"), None, None, 0
            if len(question) > settings.max_question_chars:
                return _error(413, f"Вопрос длиннее {settings.max_question_chars} символов"), None, None, 0
            payload.question = question
        if len(payload.corpus) > settings.max_corpus_chars:
            return _error(413, "Слишком много заметок в одном запросе"), None, None, 0
        if not payload.corpus.strip():          # присылать заметки не обязательно: берём срез на сервере
            return None, payload, snapshot_module.to_notes(db.snapshot_fronts()), 0
        notes, private = load_notes(payload.corpus)
        return None, payload, notes, private

    @app.post("/ask")
    async def ask(request: Request):
        error, payload, notes, private = await read_vault_request(request, need_question=True)
        if error:
            return error
        now = clock()
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        result = await ask_module.answer_question(
            settings, notes, payload.question, now,
            llm=lambda cfg, **kw: ask_llm(cfg, **kw), ada_text=ada_text,
            expander=expand_llm,
        )
        logger.info("ask notes=%d used=%d omitted=%d private=%d fallback=%s qchars=%d ada=%s",
                    len(notes), result.used_notes, result.omitted, private, result.fallback,
                    len(payload.question), bool(ada_text))
        return {
            "status": "success",
            "answer": result.answer,
            "sources": result.sources,
            "sources_text": "\n".join(f"• {title}" for title in result.sources),
            "notes_total": len(notes) + private,
            "notes_used": result.used_notes,
            "notes_omitted": result.omitted,
            "private_skipped": private,
            "fallback": result.fallback,
        }

    @app.post("/digest")
    async def digest(request: Request):
        error, payload, notes, private = await read_vault_request(request, need_question=False)
        if error:
            return error
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        week = payload.mode.strip().lower() in ("week", "неделя", "weekly")
        if week:
            text = review_module.build_week_review(notes, clock(), ada_text)
        else:
            text = ask_module.build_digest(notes, clock(), ada_brief=ada_text)
        logger.info("digest mode=%s notes=%d private=%d ada=%s", "week" if week else "day", len(notes), private, bool(ada_text))
        return {"status": "success", "digest": text, "notes_total": len(notes) + private}

    @app.post("/brief")
    async def brief(request: Request):
        error, payload, notes, private = await read_vault_request(request, need_question=False)
        if error:
            return error
        if not payload.person.strip():
            return _error(422, "Поле person пустое: напишите имя человека")
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        text = review_module.build_brief(notes, payload.person[:100], clock(), ada_text)
        logger.info("brief notes=%d private=%d", len(notes), private)
        return {"status": "success", "brief": text, "notes_total": len(notes) + private}

    @app.post("/snapshot")
    async def snapshot_sync(request: Request):
        """Полная синхронизация: заменяет срез заметками из хранилища (шорткат «Синхронизировать»)."""
        error, payload, notes, private = await read_vault_request(request, need_question=False)
        if error:
            return error
        if not payload.corpus.strip():
            return _error(422, "Поле corpus пустое: нечего синхронизировать")
        rows = [r for n in notes if (r := snapshot_module.row_from_note(n))]
        for item in db.pending():                       # ещё не выгруженное из Telegram остаётся в срезе
            rows += snapshot_module.rows_from_markdown(item["markdown"])
        db.snapshot_replace(rows, settings.snapshot_max_rows)
        count, _ = db.snapshot_info()
        logger.info("snapshot sync notes=%d private=%d stored=%d", len(notes), private, count)
        return {"status": "success", "stored": count, "private_skipped": private}

    @app.get("/snapshot")
    async def snapshot_info(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        count, synced = db.snapshot_info()
        return {"status": "success", "notes": count, "last_full_sync": synced}

    @app.delete("/snapshot")
    async def snapshot_delete(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        return {"status": "success", "removed": db.snapshot_clear()}

    async def secretary(kind: str, arg: str) -> str:
        """Ответы для Telegram по срезу на сервере."""
        now = clock()
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        if kind == "budget":
            return ada_text or "Финансы «Ады» не подключены (см. README, часть 7)."
        notes = snapshot_module.to_notes(db.snapshot_fronts())
        if not notes:
            return ("Срез хранилища пуст. Запиши что-нибудь через бота или запусти на iPhone шорткат "
                    "«Синхронизировать» (README, часть 8).")
        if kind == "day":
            return ask_module.build_digest(notes, now, ada_brief=ada_text)
        if kind == "week":
            return review_module.build_week_review(notes, now, ada_text)
        if kind == "health":
            return review_module.format_health(notes, now.date())
        if kind == "merge":
            return review_module.format_merge(notes)
        if kind == "remember":
            back = review_module.format_resurface(review_module.pick_resurface(notes, now.date(), count=3, min_age=14), now.date())
            return back or "Пока нет достаточно старых идей и хотелок, чтобы вспомнить."
        if kind == "brief":
            return review_module.build_brief(notes, arg, now, ada_text)
        result = await ask_module.answer_question(
            settings, notes, arg, now, llm=lambda cfg, **kw: ask_llm(cfg, **kw), ada_text=ada_text,
            expander=expand_llm)
        text = result.answer
        if result.sources:
            text += "\n\nИсточники:\n" + "\n".join(f"• {t}" for t in result.sources[:8])
        return text

    @app.get("/finance")
    async def finance(request: Request):
        if not authorized(request):
            return _error(401, "Неверный или отсутствующий x-api-key")
        if not ada_module.configured(settings):
            return {"status": "success", "configured": False, "summary": ""}
        return {"status": "success", "configured": True, "summary": await ada_module.fetch_summary(settings)}

    return app
