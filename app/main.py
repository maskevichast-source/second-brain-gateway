"""Second Brain Gateway: приём записей и превращение их в заметки по схеме.

Запуск: uvicorn app.main:create_app --factory
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from . import links as links_module
from . import llm as llm_module
from .config import Settings, load_settings
from .db import Database
from .service import build_private_response, build_response
from .textutil import find_urls, is_valid_capture_id, split_private

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


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"status": "error", "detail": detail}, status_code=status)


def create_app(
    settings: Settings | None = None,
    llm: Callable[..., Awaitable] | None = None,
    clock: Callable[[], datetime] | None = None,
    link_fetcher: Callable[..., Awaitable] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    llm = llm or llm_module.structure_capture
    link_fetcher = link_fetcher or links_module.fetch_link_info
    clock = clock or (lambda: datetime.now(ZoneInfo(settings.timezone)))

    db = Database(settings.data_dir / "brain.db", ttl_hours=settings.response_ttl_hours)
    db.init()
    limiter = RateLimiter(settings.rate_limit_per_minute)

    app = FastAPI(title="Second Brain Gateway", docs_url=None, redoc_url=None, openapi_url=None)

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

        # 3. Режим «без ИИ»: текст никуда не уходит и не кэшируется
        private, private_text = split_private(content)
        if private:
            if not private_text:
                return _error(422, "После «!» или «секрет:» нет текста")
            response = build_private_response(
                capture_id=capture_id, client=payload.client, text=private_text, now=clock()
            )
            logger.info("capture id=%s private chars=%d", capture_id, len(private_text))
            return response

        # 4. Идемпотентность: повторный запрос возвращает прежний ответ
        cached = db.get(capture_id, payload.revision)
        if cached:
            return cached

        now = clock()

        # 5. Названия по ссылкам, затем ИИ. Любой сбой даёт сохранение «как есть».
        urls = find_urls(content)
        link_infos = await link_fetcher(urls, settings) if urls else []
        result = await llm(
            settings,
            content=content,
            link_context=links_module.describe_for_llm(link_infos),
            clarifications=payload.user_clarifications,
            revision=payload.revision,
            now=now,
        )

        response = build_response(
            capture_id=capture_id,
            revision=payload.revision,
            client=payload.client,
            content=content,
            clarifications=payload.user_clarifications,
            result=result,
            links=link_infos,
            content_urls=urls,
            now=now,
            max_notes=settings.max_notes,
        )
        logger.info(
            "capture id=%s rev=%d notes=%d types=%s fallback=%s chars=%d",
            capture_id, payload.revision, response["count"],
            ",".join(f["type"] for f in response["files"]), response["fallback"], len(content),
        )

        db.save(capture_id=capture_id, revision=payload.revision, response=response)
        # Если параллельный дубль успел записаться раньше — отдаём первый ответ
        return db.get(capture_id, payload.revision) or response

    return app
