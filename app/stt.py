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


def build_hint(names: list[str], facts: list[str]) -> str:
    """Подсказка распознавателю: имена людей и клички из фактов, чтобы «Янка» не превращалась в «янки»."""
    words: list[str] = []
    for item in [*names, *facts]:
        for word in item.replace(",", " ").replace(".", " ").split():
            if word[:1].isupper() and len(word) > 2 and word not in words:
                words.append(word)
    return ("Имена: " + ", ".join(words[:40])) if words else ""


def configured(settings: Settings) -> bool:
    return bool(settings.stt_api_key and settings.telegram_bot_token)


async def transcribe(settings: Settings, file_id: str, http_client: httpx.AsyncClient | None = None,
                     hint: str = "") -> str | None:
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

        # Telegram отдаёт voice/file_X.oga, но Groq требует расширение .ogg
        filename = path.rsplit("/", 1)[-1] or "voice.ogg"
        if filename.endswith(".oga"):
            filename = filename[:-4] + ".ogg"

        response = await client.post(
            f"{settings.stt_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.stt_api_key}"},
            files={"file": (filename, audio.content, "audio/ogg")},
            data={"model": settings.stt_model, "language": "ru", "response_format": "json",
                  **({"prompt": hint[:400]} if hint else {})},
        )
        response.raise_for_status()
        text = str(response.json().get("text", "")).strip()
        return text or None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "stt: ошибка API %s: %s",
            exc.response.status_code,
            exc.response.text[:300]
        )
        monitor.failure("stt", exc)
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("stt: не удалось расшифровать (%s)", type(exc).__name__)
        monitor.failure("stt", exc)
        return None
    finally:
        if owns:
            await client.aclose()
