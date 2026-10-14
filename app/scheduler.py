"""Фоновые сообщения от бота: вечерний дневник, утренняя сводка, обзор недели, напоминания в срок.

Работает, только если на сервере есть токен бота (TELEGRAM_BOT_TOKEN). Всё считается по срезу хранилища.
Идеи взяты из open-source «вторых мозгов» (утренний, вечерний и недельный проходы, «из прошлого»).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta

import httpx

from . import ada as ada_module
from . import ask as ask_module
from . import diary
from . import review as review_module
from . import snapshot as snapshot_module
from .config import Settings
from .db import Database
from .textutil import parse_datetime

logger = logging.getLogger(__name__)

MORNING_DEFAULT = "08:30"
WEEK_TIME = "19:00"
REMIND_WINDOW = timedelta(hours=12)       # давно просроченные напоминания не высыпаем разом после простоя
DONE_STATES = frozenset({"сделано", "отменено", "отложено", "состоялось"})


def get_diary_config(db: Database) -> tuple[bool, str]:
    on = db.get_setting("diary_on", "1") == "1"
    at = db.get_setting("diary_time", "") or diary.DEFAULT_TIME
    return on, at


def push_config(db: Database) -> dict:
    return {
        "morning_on": db.get_setting("push_morning_on", "1") == "1",
        "morning_time": db.get_setting("push_morning_time", "") or MORNING_DEFAULT,
        "week_on": db.get_setting("push_week_on", "1") == "1",
        "rem_on": db.get_setting("push_rem_on", "1") == "1",
    }


def reminder_key(title: str, when: str) -> str:
    return hashlib.sha1(f"{title}|{when}".encode("utf-8")).hexdigest()[:12]


def due_reminders(items: list[tuple[str, dict]], db: Database, now: datetime) -> list[tuple[str, str, str]]:
    """(ключ, заголовок, подпись) для напоминаний, срок которых наступил и о которых ещё не сообщали."""
    out = []
    for _, front in items:
        if str(front.get("status", "")) in DONE_STATES or front.get("type") not in ("task", "event"):
            continue
        field, lead = ("remind_at", timedelta(0)) if front.get("remind_at") else ("event_at", timedelta(minutes=30))
        raw = parse_datetime(front.get(field))
        if not raw or len(raw) <= 10:                       # только дата без времени: это забота утренней сводки
            continue
        due = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo) - lead
        title = str(front.get("title", ""))
        key = reminder_key(title, raw)
        state = db.get_setting(f"rem_{key}", "")
        if state in ("done", "sent"):
            continue
        if state:                                           # отложено: ждём указанное время
            due = datetime.fromisoformat(state).replace(tzinfo=now.tzinfo)
        if due <= now < due + REMIND_WINDOW:
            label = "⏰ Скоро событие" if field == "event_at" else "⏰ Напоминание"
            out.append((key, title, f"{label}: {title}"))
    return out


async def tick(db: Database, settings: Settings, now: datetime, send) -> int:
    """Одна проверка. Возвращает число отправленных сообщений."""
    sent = 0
    chats = list(settings.telegram_allowed_ids)

    async def broadcast(text: str, buttons=None) -> bool:
        ok = True
        for chat_id in chats:
            ok = (await (send(chat_id, text, buttons) if buttons else send(chat_id, text))) and ok
        return ok

    items = db.snapshot_items()
    fronts = [front for _, front in items]
    today = now.date()

    # вечерний дневник
    on, at = get_diary_config(db)
    if diary.should_remind(now, on=on, at=at, last_sent=db.get_setting("diary_last_sent", ""),
                           entry_today=diary.has_entry(fronts, diary.entry_date(now))):
        if await broadcast(diary.REMINDER):
            db.set_setting("diary_last_sent", today.isoformat())
        sent += len(chats)

    cfg = push_config(db)
    notes = snapshot_module.to_notes(fronts) if fronts else []

    # напоминания в срок (с кнопками отложить)
    if cfg["rem_on"] and notes:
        for key, title, text in due_reminders(items, db, now):
            buttons = [[("Через час", f"snz1:{key}"), ("Завтра утром", f"snz24:{key}")], [("Ок", f"ok:{key}")]]
            if await broadcast(text, buttons):
                db.set_setting(f"rem_{key}", "sent")
            sent += len(chats)

    # утренняя сводка
    if cfg["morning_on"] and notes and now.hour < 12 and db.get_setting("push_morning_last", "") != today.isoformat():
        hh, mm = (int(x) for x in cfg["morning_time"].split(":"))
        if (now.hour, now.minute) >= (hh, mm):
            text = ask_module.build_digest(notes, now)
            back = review_module.format_resurface(review_module.pick_resurface(notes, today), today)
            if "Срочного нет" in text and not back:
                db.set_setting("push_morning_last", today.isoformat())
            else:
                if back:
                    text += "\n\n" + back
                if await broadcast("☀️ Доброе утро.\n\n" + text):
                    db.set_setting("push_morning_last", today.isoformat())
                sent += len(chats)

    # обзор недели по воскресеньям вечером
    week_id = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]}"
    if (cfg["week_on"] and notes and today.weekday() == 6 and now.hour >= int(WEEK_TIME[:2])
            and db.get_setting("push_week_last", "") != week_id):
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        if await broadcast(review_module.build_week_review(notes, now, ada_text)):
            db.set_setting("push_week_last", week_id)
        sent += len(chats)
    return sent


def telegram_sender(settings: Settings):
    async def send(chat_id: int, text: str, buttons=None) -> bool:
        body = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", json=body)
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
