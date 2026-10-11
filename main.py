"""Second Brain Gateway — шаг 1: приём текста и ссылок.

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

from . import llm as llm_module
from .config import Settings, load_settings
from .db import Database
from .service import build_response
from .textutil import is_valid_capture_id

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
) -> FastAPI:
    settings = settings or load_settings()
    llm = llm or llm_module.structure_note
    clock = clock or (lambda: datetime.now(ZoneInfo(settings.timezone)))

    db = Database(settings.data_dir / "brain.db")
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
            return _error(422, "Пока поддерживаются только input_type: text и url (голос, фото и PDF — в следующих шагах)")
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

        # 3. Идемпотентность: повторный запрос возвращает прежний ответ
        cached = db.get(capture_id, payload.revision)
        if cached:
            return cached

        # 4. ИИ (при сбое note = None -> заметка сохранится как есть)
        note = await llm(
            settings,
            input_type=payload.input_type,
            content=content,
            clarifications=payload.user_clarifications,
            revision=payload.revision,
        )
        if note is None:
            logger.warning("capture %s: ИИ недоступен, сохраняем как есть", capture_id)

        response = build_response(
            capture_id=capture_id,
            revision=payload.revision,
            client=payload.client,
            input_type=payload.input_type,
            content=content,
            clarifications=payload.user_clarifications,
            note=note,
            now=clock(),
        )

        db.save(
            capture_id=capture_id,
            revision=payload.revision,
            client=response["yaml_frontmatter"]["source"],
            input_type=payload.input_type,
            content=content,
            clarification=payload.user_clarifications,
            response=response,
        )
        # Если параллельный дубль успел записаться раньше — отдаём первый ответ
        return db.get(capture_id, payload.revision) or response

    return app
