"""Фоновые напоминания в Telegram (нужен токен бота TELEGRAM_BOT_TOKEN)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from . import diary
from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)


def get_diary_config(db: Database) -> tuple[bool, str]:
    on = db.get_setting("diary_on", "1") == "1"
    at = db.get_setting("diary_time", "") or diary.DEFAULT_TIME
    return on, at


async def tick(db: Database, settings: Settings, now: datetime, send) -> int:
    """Одна проверка. Возвращает число отправленных напоминаний."""
    on, at = get_diary_config(db)
    day = diary.entry_date(now)
    sent = 0
    if diary.should_remind(now, on=on, at=at, last_sent=db.get_setting("diary_last_sent", ""),
                           entry_today=diary.has_entry(db.snapshot_fronts(), day)):
        ok = True
        for chat_id in settings.telegram_allowed_ids:
            ok = await send(chat_id, diary.REMINDER) and ok
            sent += 1
        if ok:
            db.set_setting("diary_last_sent", now.date().isoformat())
    return sent


def telegram_sender(settings: Settings):
    async def send(chat_id: int, text: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                                      json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
            return r.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("scheduler: Telegram недоступен (%s)", type(exc).__name__)
            return False
    return send


async def run_loop(db: Database, settings: Settings, clock, send, interval: float = 60.0) -> None:
    while True:
        try:
            await tick(db, settings, clock(), send)
        except Exception as exc:  # noqa: BLE001 — цикл не должен падать
            logger.error("scheduler: ошибка (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
