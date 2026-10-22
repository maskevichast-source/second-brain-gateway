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
    """Подсказка распознавателю (Whisper принимает ~224 токена): сначала факты целиком («Компания называется
    Идея Декор»), потом имена людей. Так «Янка» не становится «янки», а название фирмы не пишется латиницей."""
    parts: list[str] = []
    total = 0
    for item in [*reversed(facts), *names]:                       # свежие факты важнее старых
        item = " ".join(str(item).split())
        if not item or item in parts:
            continue
        if total + len(item) + 2 > HINT_CHARS:
            continue
        parts.append(item)
        total += len(item) + 2
    return ". ".join(parts)


HINT_CHARS = 380


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
                  **({"prompt": hint[:HINT_CHARS + 20]} if hint else {})},
        )
        response.raise_for_status()
        text = str(response.json().get("text", "")).strip()
        return text or None
    except httpx.HTTPStatusError as exc:
        logger.warning("stt: ошибка API %s", exc.response.status_code)     # тело ответа не логируем: может содержать фрагменты запроса
        monitor.failure("stt", exc)
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("stt: не удалось расшифровать (%s)", type(exc).__name__)
        monitor.failure("stt", exc)
        return None
    finally:
        if owns:
            await client.aclose()


def bytes_configured(settings: Settings) -> bool:
    return bool(settings.stt_api_key)


async def transcribe_bytes(settings: Settings, data: bytes, filename: str = "audio.m4a", mime: str = "audio/mp4",
                           hint: str = "", http_client: httpx.AsyncClient | None = None) -> str | None:
    """Текст аудио, присланного прямо на сервер (шорткат iPhone), или None. Аудио нигде не сохраняется этой функцией."""
    if not bytes_configured(settings) or not data or len(data) > MAX_BYTES:
        return None
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=120)
    try:
        response = await client.post(
            f"{settings.stt_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.stt_api_key}"},
            files={"file": (filename or "audio.m4a", data, mime or "audio/mp4")},
            data={"model": settings.stt_model, "language": "ru", "response_format": "json",
                  **({"prompt": hint[:HINT_CHARS + 20]} if hint else {})},
        )
        response.raise_for_status()
        text = str(response.json().get("text", "")).strip()
        return text or None
    except httpx.HTTPStatusError as exc:
        logger.warning("stt: ошибка API %s", exc.response.status_code)
        monitor.failure("stt", exc)
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("stt: не удалось распознать (%s)", type(exc).__name__)
        monitor.failure("stt", exc)
        return None
    finally:
        if owns:
            await client.aclose()
