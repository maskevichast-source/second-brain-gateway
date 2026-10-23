"""Одинаковые события и дела не заводятся второй раз.

Проверка консервативная, чтобы ничего не потерять: событие считается повтором, только если в срезе уже есть открытое
событие с таким же названием (допускаются мелкие отличия) в тот же день; дело, только если совпадают название и срок
(дата или время). Заметки с вложениями (оригинал уходит в 7-Исходники) и приватные не отбрасываются никогда.
Люди тут не трогаются: их дубли убирает 2_tidy_people.bat после переноса данных в карточку.
"""
from __future__ import annotations

import difflib
import re

from .textutil import parse_date, parse_datetime
from .snapshot import rows_from_markdown
from .vault import parse_note

DONE = ("состоялось", "отменено", "сделано", "отложено")


def _norm(title: str) -> str:
    return re.sub(r"[^a-zа-яё0-9 ]", "", re.sub(r"\s+", " ", str(title).lower().replace("ё", "е"))).strip()


def _same_title(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9)


def _is_repeat(note, items: list[tuple[str, dict]]) -> str | None:
    if note.type == "event":
        day = parse_date(note.get("event_at"))
        if not day:
            return None
        for _, front in items:
            if (front.get("type") == "event" and str(front.get("status", "")).lower() not in DONE
                    and parse_date(front.get("event_at")) == day and _same_title(note.title, str(front.get("title", "")))):
                return str(front.get("title", ""))
    elif note.type == "task":
        when = parse_datetime(note.get("due")) or parse_datetime(note.get("remind_at"))
        if not when:
            return None
        for _, front in items:
            if front.get("type") != "task" or str(front.get("status", "")).lower() in DONE:
                continue
            other = parse_datetime(front.get("due")) or parse_datetime(front.get("remind_at"))
            if other == when and _same_title(note.title, str(front.get("title", ""))):
                return str(front.get("title", ""))
    return None


def drop_repeats(files: list[dict], items: list[tuple[str, dict]], has_sources: bool) -> tuple[list[dict], list[str]]:
    """(что сохранять, названия отброшенных повторов). Повторы внутри одной пачки тоже отбрасываются."""
    if has_sources:
        return files, []
    own = {key for f in files for key, _ in rows_from_markdown(f["markdown"])}      # сервер уже положил эти заметки в срез
    items = [(k, v) for k, v in items if k not in own]
    kept, dropped = [], []
    seen: list[tuple[str, dict]] = []
    for f in files:
        note = parse_note(f["markdown"], 1)
        if note.is_private:
            kept.append(f)
            continue
        title = _is_repeat(note, items + seen)
        if title:
            dropped.append(title)
            continue
        kept.append(f)
        seen.append((f.get("filename", ""), dict(note.front)))
    return kept, dropped
