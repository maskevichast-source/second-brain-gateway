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

from . import actions as actions_module
from . import ada as ada_module
from . import ask as ask_module
from . import diary
from . import jarvis as jarvis_module
from . import monitor
from . import news as news_module
from . import owner as owner_module
from . import people_links as people_links_module
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
        "health_on": db.get_setting("push_health_on", "1") == "1",
        "rem_on": db.get_setting("push_rem_on", "1") == "1",
        "jarvis_on": db.get_setting("push_jarvis_on", "1") == "1",
        "news_on": db.get_setting("push_news_on", "1") == "1",
    }


def reminder_key(title: str, when: str) -> str:
    return hashlib.sha1(f"{title}|{when}".encode("utf-8")).hexdigest()[:12]


def _reminder_stamp(front: dict) -> str | None:
    field = "remind_at" if front.get("remind_at") else "event_at"
    raw = parse_datetime(front.get(field))
    return raw if raw and len(raw) > 10 else None


def find_reminder_note(items: list[tuple[str, dict]], key: str) -> tuple[str, dict] | None:
    """Заметка из среза (ключ строки, свойства), которой принадлежит ключ кнопки напоминания."""
    for row_key, front in items:
        if front.get("type") not in ("task", "event"):
            continue
        raw = _reminder_stamp(front)
        if raw and reminder_key(str(front.get("title", "")), raw) == key:
            return row_key, front
    return None


def due_reminders(items: list[tuple[str, dict]], db: Database, now: datetime) -> list[tuple[str, str, str, str]]:
    """(ключ, заголовок, подпись, ключ заметки в срезе) для напоминаний, срок которых наступил и о которых ещё не сообщали."""
    out = []
    for row_key, front in items:
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
            out.append((key, title, f"{label}: {title}", row_key[:12]))
    return out


def reminder_buttons(key: str, note_key: str, now: datetime) -> list:
    """Кнопки под напоминанием: главное одним нажатием. Днём предлагаем «вечером», поздно вечером «через 3 дня»."""
    later = ("🌆 Вечером, в 19:00", f"snzev:{key}") if now.hour < 17 else ("🗓 Через 3 дня", f"snz3d:{key}")
    return [[("✅ Сделано", f"ok:{key}"), ("⏰ Через час", f"snz1:{key}")],
            [later, ("🌅 Завтра в 9:00", f"snz24:{key}")],
            [("🕐 Другое время", f"it:{note_key}"), ("🗑 Отменить", f"cx:{note_key}")]]


def week_buttons(keyed_notes: list, today, limit: int = 6) -> list:
    """Кнопки под обзором недели: открыть панель (готово, отмена, перенос) для самых просроченных дел."""
    from . import ask as ask_module

    items = [i for i in ask_module.collect_items(keyed_notes, today) if i.key and i.day < today]
    items.sort(key=lambda i: i.day)
    rows = [[(f"✅ {i.title[:24]}", f"it:{i.key}")] for i in items[:limit]]
    return rows


async def tick(db: Database, settings: Settings, now: datetime, send) -> int:
    """Одна проверка. Возвращает число отправленных сообщений."""
    sent = 0
    chats = list(settings.telegram_allowed_ids)

    async def broadcast(text: str, buttons=None) -> bool:
        ok = True
        for chat_id in chats:
            ok = (await (send(chat_id, text, buttons) if buttons else send(chat_id, text))) and ok
        return ok

    owner_module.load(db)
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
        for key, title, text, note_key in due_reminders(items, db, now):
            buttons = reminder_buttons(key, note_key, now)
            if await broadcast(text, buttons):
                db.set_setting(f"rem_{key}", "sent")
            sent += len(chats)

    # после события с людьми: «как прошло?» с кнопками (один раз)
    if cfg["rem_on"] and notes and not jarvis_module.is_quiet(now):
        for row_key, text, flag in people_links_module.due_followups(items, db, now)[:2]:
            if await broadcast(text, people_links_module.followup_buttons(row_key)):
                db.set_setting(flag, "1")
            sent += len(chats)

    # Джарвис: крупные покупки и доходы из таблицы трат «Ады»
    if (cfg["jarvis_on"] and ada_module.configured(settings) and not jarvis_module.is_quiet(now)
            and jarvis_module.should_run(db, now)):
        jarvis_module.stamp_run(db, now)
        data = await ada_module.fetch_raw(settings)
        for aid, text, btns in jarvis_module.due_alerts(data, db, now)[:jarvis_module.MAX_PER_RUN]:
            if await broadcast(text, btns):
                jarvis_module.after_sent(db, aid, now)
            sent += len(chats)

    # важные новости про сервисы (сбои, утечки, смена ключей); про безопасность пишем и ночью
    if cfg["news_on"] and settings.news_feeds and db.get_setting("news_last_run", "") != now.strftime("%Y-%m-%d %H") + (":0" if now.minute < 30 else ":3"):
        db.set_setting("news_last_run", now.strftime("%Y-%m-%d %H") + (":0" if now.minute < 30 else ":3"))
        for item in (await news_module.fetch_new(db, settings.news_feeds, now))[:3]:
            if item.critical or not jarvis_module.is_quiet(now):
                await broadcast(news_module.format_item(item))
                sent += len(chats)

    # утренняя сводка
    if cfg["morning_on"] and notes and now.hour < 12 and db.get_setting("push_morning_last", "") != today.isoformat():
        hh, mm = (int(x) for x in cfg["morning_time"].split(":"))
        if (now.hour, now.minute) >= (hh, mm):
            loans_line = await ada_module.fetch_loans_brief(settings) if ada_module.configured(settings) else ""
            text, digest_buttons = actions_module.render_day(db, now, ada_brief=loans_line)
            back = review_module.format_resurface(review_module.pick_resurface(notes, today), today)
            if "Срочного нет" in text and not back and not loans_line:
                db.set_setting("push_morning_last", today.isoformat())
            else:
                if back:
                    text += "\n\n" + back
                if await broadcast("☀️ Доброе утро.\n\n" + text, digest_buttons or None):
                    db.set_setting("push_morning_last", today.isoformat())
                sent += len(chats)

    # обзор недели по воскресеньям вечером
    week_id = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]}"
    if (cfg["week_on"] and notes and today.weekday() == 6 and now.hour >= int(WEEK_TIME[:2])
            and db.get_setting("push_week_last", "") != week_id):
        ada_text = await ada_module.fetch_summary(settings) if ada_module.configured(settings) else ""
        keyed = actions_module.keyed_notes(db)
        buttons = week_buttons(keyed, today)
        if await broadcast(review_module.build_week_review(keyed, now, ada_text), buttons or None):
            db.set_setting("push_week_last", week_id)
        sent += len(chats)

    # проверка хранилища по понедельникам: сообщение только если есть замечания
    if (cfg["health_on"] and notes and today.weekday() == 0 and now.hour >= 10
            and db.get_setting("push_health_last", "") != week_id):
        score, issues = review_module.build_health(notes, today)
        if not issues or await broadcast("🩺 " + review_module.format_health(notes, today)):
            db.set_setting("push_health_last", week_id)
        sent += len(chats) if issues else 0
    return sent


def telegram_sender(settings: Settings):
    async def send(chat_id: int, text: str, buttons=None) -> bool:
        body = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", json=body)
            if r.status_code != 200:
                monitor.failure("telegram", RuntimeError(f"HTTP {r.status_code}"))
            return r.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("scheduler: Telegram недоступен (%s)", type(exc).__name__)
            monitor.failure("telegram", exc)
            return False
    return send


def telegram_document_sender(settings: Settings):
    """Отправка файла в личку (для /backup). Токен бота нужен только здесь, в логи он не пишется."""
    async def send_document(chat_id: int, filename: str, data: bytes, caption: str = "") -> bool:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument",
                                      data={"chat_id": str(chat_id), "caption": caption[:900]},
                                      files={"document": (filename, data, "application/json")})
            if r.status_code != 200:
                monitor.failure("telegram", RuntimeError(f"HTTP {r.status_code}"))
            return r.status_code == 200
        except httpx.HTTPError as exc:
            monitor.failure("telegram", exc)
            return False
    return send_document


async def run_loop(db: Database, settings: Settings, clock, send, interval: float = 60.0) -> None:
    while True:
        try:
            await tick(db, settings, clock(), send)
        except Exception as exc:  # noqa: BLE001 — цикл не должен падать
            logger.error("scheduler: ошибка (%s)", type(exc).__name__)
            monitor.failure("scheduler", exc)
        try:
            await monitor.MONITOR.flush()
        except Exception as exc:  # noqa: BLE001
            logger.error("scheduler: ошибка монитора (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
