"""Telegram-вход: бот принимает сообщения и кладёт готовые заметки в очередь.

Сервер не может писать в iCloud, поэтому заметки ждут в очереди (`outbox`), пока их не заберёт
ноутбук (tools/vault_sync.py) или телефон (шорткат). После подтверждения (`/ack`) они удаляются.
Ответы боту отдаются прямо в ответе на webhook, поэтому токену бота на сервере делать нечего.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

HELP = (
    "Пришли мысль, ссылку или список дел, и я превращу это в заметку для Obsidian.\n\n"
    "• Начни сообщение с «!» или «секрет:», чтобы текст не уходил в ИИ.\n"
    "• Если я переспрошу, ответь одним сообщением или напиши /skip, чтобы сохранить как есть.\n"
    "• /status — сколько заметок ждут выгрузки в хранилище.\n\n"
    "Голос и фото пока не принимаю: сначала преврати их в текст (на iPhone есть диктовка с клавиатуры)."
)

Processor = Callable[..., Awaitable[dict]]


def _reply(chat_id: int, text: str) -> dict:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}


def _summary(response: dict) -> str:
    files = response.get("files", [])
    if response.get("private"):
        head = "🔒 Сохранил без ИИ."
    elif response.get("fallback"):
        head = "⚠️ ИИ недоступен, сохранил как есть."
    else:
        head = f"✅ {response.get('message', 'Записал')}"
    if len(files) > 1:
        head = f"✅ Заметок: {len(files)}"
        head += "\n" + "\n".join(f"• {f['title']}" for f in files[:10])
    return head + "\nВ хранилище появится, когда ноутбук или телефон заберёт очередь."


async def handle_update(
    update: dict, *, settings: Settings, db: Database, process: Processor,
) -> dict | None:
    """Возвращает ответ для Telegram (метод sendMessage) или None, если отвечать не нужно."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    user_id = (message.get("from") or {}).get("id")
    if not isinstance(chat_id, int) or not isinstance(user_id, int):
        return None

    allowed = settings.telegram_allowed_ids
    if not allowed:
        return _reply(chat_id, f"Бот ещё не настроен. Твой Telegram ID: {user_id}. "
                               "Впиши его в переменную TELEGRAM_ALLOWED_USER_IDS на Railway.")
    if user_id not in allowed:
        return None

    update_id = update.get("update_id")
    dedupe_key = f"tgu-{update_id}" if isinstance(update_id, int) else None
    if dedupe_key:
        cached = db.get(dedupe_key, 1)
        if cached:
            return cached

    reply = await _answer(message, chat_id, update_id, settings=settings, db=db, process=process)
    if dedupe_key and reply:
        db.save(capture_id=dedupe_key, revision=1, response=reply)
    return reply


async def _answer(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database, process: Processor) -> dict:
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return _reply(chat_id, "Пока принимаю только текст и ссылки. " + HELP.split("\n\n")[-1])

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command in ("/start", "/help"):
        return _reply(chat_id, HELP)
    if command == "/status":
        return _reply(chat_id, f"Ждут выгрузки в хранилище: {db.pending_count()}")

    waiting = db.get_waiting(chat_id)
    if command == "/skip":
        if not waiting:
            return _reply(chat_id, "Сейчас нечего пропускать.")
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], "", 2
    elif command:
        return _reply(chat_id, "Не знаю такой команды. " + HELP.split("\n\n")[0])
    elif waiting:
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], text, 2
    else:
        capture_id, content, clarifications, revision = f"tg-{update_id}", text, "", 1

    if len(content) > settings.max_content_chars or len(clarifications) > settings.max_content_chars:
        return _reply(chat_id, f"Слишком длинно: максимум {settings.max_content_chars} символов.")

    response = await process(
        capture_id=capture_id, client="telegram", content=content,
        revision=revision, clarifications=clarifications,
    )
    if revision == 2:
        db.clear_waiting(chat_id)
    if response.get("needs_review") and revision == 1:
        db.set_waiting(chat_id, capture_id, content)
        questions = "\n".join(f"• {q}" for q in response.get("review_questions", []))
        return _reply(chat_id, f"Нужно уточнение:\n{questions}\n\nОтветь одним сообщением или /skip.")
    db.enqueue(capture_id, response["files"])
    return _reply(chat_id, _summary(response))
