"""Напоминания и события для iPhone: сервис только подсказывает, шорткат создаёт их сам."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .textutil import one_line, parse_datetime
from .vault import parse_note

DONE = frozenset({"сделано", "отменено", "отложено", "достигнута", "отброшена", "готово", "отказался",
                  "состоялось", "завершена", "отменена", "пересмотрено", "расторгнут"})

# (тип, поле, вид, смещение в днях, подпись)
RULES = (
    ("task", "remind_at", "reminder", 0, ""),
    ("task", "due", "reminder", 0, ""),
    ("event", "event_at", "event", 0, ""),
    ("trip", "date_from", "event", 0, "Поездка: "),
    ("interaction", "follow_up_due", "reminder", 0, "Связаться: "),
    ("home", "next_due", "reminder", 0, "По дому: "),
    ("document", "valid_until", "reminder", -14, "Скоро истекает: "),
    ("home", "warranty_until", "reminder", -14, "Кончается гарантия: "),
    ("decision", "review_at", "reminder", 0, "Пересмотреть решение: "),
    ("project", "deadline", "reminder", -1, "Срок проекта: "),
    ("case", "deadline", "reminder", -1, "Срок по делу: "),
    ("product", "deadline", "reminder", -1, "Купить к сроку: "),
    ("goal", "deadline", "reminder", -3, "Срок цели: "),
)


def _split(stamp: str) -> tuple[date, str]:
    day = date.fromisoformat(stamp[:10])
    time = stamp[11:16] if len(stamp) >= 16 else ""
    return day, time


def extract(markdown: str, filename: str, today: date) -> list[dict]:
    note = parse_note(markdown, 1)
    if note.is_private:
        return []
    status = str(note.get("status"))
    if status in DONE:
        return []
    items, seen = [], set()
    for type_id, field, kind, offset, prefix in RULES:
        if note.type != type_id:
            continue
        stamp = parse_datetime(note.get(field))
        if not stamp:
            continue
        day, time = _split(stamp)
        if offset:
            day, time = day + timedelta(days=offset), ""
        if day < today:
            continue
        key = (kind, day, time)
        if type_id == "task" and field == "due" and any(k[0] == "reminder" for k in seen) and note.get("remind_at"):
            continue                                    # напоминание по времени уже есть, срок не дублируем
        if key in seen:
            continue
        seen.add(key)
        item = {
            "title": one_line(prefix + note.title, 120),
            "kind": kind,
            "date": day.isoformat(),
            "time": time,
            "note": one_line(str(note.get("summary")), 200),
            "file": filename,
        }
        # готовые строки дат для шортката: «ГГГГ-ММ-ДД ЧЧ:ММ» (без времени — 09:00)
        start_dt = datetime.fromisoformat(f"{day.isoformat()}T{time or '09:00'}")
        item["start"] = start_dt.strftime("%Y-%m-%d %H:%M")
        if kind == "event":
            end = parse_datetime(note.get("event_end"))
            end_dt = datetime.fromisoformat(end) if end and len(end) >= 16 else start_dt + timedelta(hours=1)
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(hours=1)
            item["end"] = end_dt.strftime("%Y-%m-%d %H:%M")
        else:
            item["end"] = ""
        items.append(item)
    return items


def for_files(files: list[dict], now: datetime) -> list[dict]:
    """Дописывает каждому файлу `reminders` и возвращает общий список."""
    today = now.date()
    everything: list[dict] = []
    for f in files:
        items = extract(f["markdown"], f["filename"], today)
        f["reminders"] = items
        everything.extend(items)
    return everything
