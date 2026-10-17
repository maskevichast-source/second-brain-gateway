"""Файлы из Telegram: скачивание, текст из PDF и (по желанию) распознавание текста на картинках.

Файлы нигде не сохраняются: байты живут только во время обработки сообщения. PDF читается локально
библиотекой pypdf (без сети). Распознавание картинок через vision-модель выключено, пока не задана
переменная VISION_MODEL (и ключ: VISION_API_KEY или общий STT_API_KEY); без неё или при любом сбое запись сохраняется как есть с честной пометкой.
"""
from __future__ import annotations

import base64
import io
import logging
import re

import httpx

from . import monitor
from .config import Settings

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024          # лимит скачивания у Telegram Bot API
MAX_PDF_CHARS = 15000                 # сколько символов из PDF идёт в запись
MAX_PDF_PAGES = 40
MAX_IMAGE_BYTES = 3 * 1024 * 1024     # base64 у Groq не больше 4 МБ
NO_TEXT = "НЕТ"
VISION_PROMPT = ("Перепиши весь читаемый текст с картинки как есть, без пояснений. "
                 f"Если текста нет, ответь одним словом: {NO_TEXT}.")


async def fetch_file(settings: Settings, file_id: str, http_client: httpx.AsyncClient | None = None) -> bytes | None:
    """Байты файла Telegram или None. Токен бота в логи не попадает."""
    if not (settings.telegram_bot_token and file_id):
        return None
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=60)
    try:
        base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        info = await client.get(f"{base}/getFile", params={"file_id": file_id})
        info.raise_for_status()
        result = info.json()["result"]
        if int(result.get("file_size") or 0) > MAX_BYTES:
            return None
        blob = await client.get(f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{result['file_path']}")
        blob.raise_for_status()
        return blob.content if len(blob.content) <= MAX_BYTES else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("files: не удалось скачать (%s)", type(exc).__name__)
        monitor.failure("telegram", exc)
        return None
    finally:
        if owns:
            await client.aclose()


def extract_pdf_text(data: bytes, max_pages: int = MAX_PDF_PAGES) -> str:
    """Текст PDF с текстовым слоем. Пустая строка: скан, пароль, битый файл или нет pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return ""
        parts: list[str] = []
        total = 0
        for page in reader.pages[:max_pages]:
            chunk = page.extract_text() or ""
            parts.append(chunk)
            total += len(chunk)
            if total > MAX_PDF_CHARS * 4:
                break
    except Exception as exc:  # noqa: BLE001 — pypdf бросает много разных ошибок на битых файлах
        logger.info("files: PDF не прочитан (%s)", type(exc).__name__)
        return ""
    text = "\n".join(parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _vision_key(settings: Settings) -> str:
    return settings.vision_api_key or settings.stt_api_key


def _vision_base(settings: Settings) -> str:
    return settings.vision_base_url or settings.stt_base_url


def vision_enabled(settings: Settings) -> bool:
    return bool(settings.vision_model and _vision_key(settings))


async def describe_image(settings: Settings, data: bytes, mime: str = "image/jpeg",
                         http_client: httpx.AsyncClient | None = None) -> str | None:
    """Текст с картинки через OpenAI-совместимый /chat/completions (Groq vision) или None."""
    if not vision_enabled(settings) or not data or len(data) > MAX_IMAGE_BYTES:
        return None
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=60)
    try:
        url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode('ascii')}"
        base = _vision_base(settings)
        body = {"model": settings.vision_model, "temperature": 0, "max_tokens": 1500,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": url}}]}]}
        if "api.openai.com" in base:
            # новые модели OpenAI не принимают max_tokens и temperature; max_completion_tokens понимают и старые
            body.pop("max_tokens"), body.pop("temperature")
            body["max_completion_tokens"] = 4000
        elif "generativelanguage.googleapis.com" in base:
            # Gemini «думает» перед ответом и тратит на это лимит токенов: думаем минимально, лимит побольше
            body["reasoning_effort"] = "low"
            body["max_tokens"] = 4000
        response = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {_vision_key(settings)}"},
            json=body,
        )
        response.raise_for_status()
        text = str(response.json()["choices"][0]["message"]["content"] or "").strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("vision: не удалось распознать (%s)", type(exc).__name__)
        monitor.failure("vision", exc)
        return None
    finally:
        if owns:
            await client.aclose()
    if not text or text.strip(". ").upper() == NO_TEXT:
        return None
    return text[:6000]
