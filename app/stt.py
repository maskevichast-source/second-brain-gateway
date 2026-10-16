"""Расшифровка голосовых сообщений Telegram.

Бот скачивает голосовое по токену бота и отправляет в сервис распознавания речи, совместимый с OpenAI
(по умолчанию Groq, модель whisper-large-v3-turbo; подходит и сам OpenAI). Аудио нигде не сохраняется.
"""
from __future__ import annotations

import logging

import httpx

from . import monitor
from .config import Settings

logger = logging.getLogger(__name__)

MAX_SECONDS = 300
MAX_BYTES = 20 * 1024 * 1024          # лимит скачивания файлов у Telegram Bot API


def configured(settings: Settings) -> bool:
    return bool(settings.stt_api_key and settings.telegram_bot_token)


async def transcribe(settings: Settings, file_id: str, http_client: httpx.AsyncClient | None = None) -> str | None:
    """Текст голосового сообщения или None, если не получилось."""
    if not configured(settings):
        return None
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=60)
    try:
        base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        info = await client.get(f"{base}/getFile", params={"file_id": file_id})
        info.raise_for_status()
        path = info.json()["result"]["file_path"]
        audio = await client.get(f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{path}")
        audio.raise_for_status()
        if len(audio.content) > MAX_BYTES:
            return None
        response = await client.post(
            f"{settings.stt_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.stt_api_key}"},
            files={"file": (path.rsplit("/", 1)[-1] or "voice.ogg", audio.content)},
            data={"model": settings.stt_model, "language": "ru", "response_format": "json"},
        )
        response.raise_for_status()
        text = str(response.json().get("text", "")).strip()
        return text or None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("stt: не удалось расшифровать (%s)", type(exc).__name__)
        monitor.failure("stt", exc)
        return None
    finally:
        if owns:
            await client.aclose()
