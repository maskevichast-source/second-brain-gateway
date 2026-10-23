"""Срез хранилища на сервере: короткие свойства заметок без полного текста.

Нужен, чтобы Telegram-бот отвечал на вопросы, когда ноутбук выключен. В срез попадает название, тип,
статус, даты, люди, теги и одна строка сути. Тела заметок, приватные заметки (`!`, `private: true`)
и заметки без `type` не хранятся. Стереть всё: команда /forget или DELETE /snapshot.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .textutil import one_line
from .vault import EXTRA_FIELDS, VaultNote, parse_note

KEEP = ("title", "type", "kind", "status", "area", "created", "stage", "confidence", "priority", "source_url", "promise") + EXTRA_FIELDS
LIST_KEEP = ("people", "related", "tags", "aliases", "interests", "gift_ideas", "children")
PERSON_KEEP = ("name", "relation", "relation_group", "last_contact", "preferences", "family", "how_met", "contact_goal",
               "event_end", "location", "follow_up", "orbit", "cadence_days", "partner")
MAX_ROWS = 20000


def _cut(value: Any, limit: int = 200) -> Any:
    if isinstance(value, list):
        return [one_line(str(v), 80) for v in value[:10]]
    if isinstance(value, str):
        return one_line(value, limit)
    return value


def row_from_note(note: VaultNote) -> tuple[str, dict] | None:
    if note.is_private or not note.type:
        return None
    front: dict[str, Any] = {}
    for key in KEEP + PERSON_KEEP + LIST_KEEP:
        value = note.front.get(key)
        if value not in (None, "", []):
            front[key] = _cut(value)
    summary = one_line(str(note.get("summary")), 200)
    if summary:
        front["summary"] = summary
    front["title"] = note.title
    identity = f"{note.get('capture_id')}|{note.title}|{str(note.created)[:16]}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest(), front


def rows_from_markdown(markdown: str) -> list[tuple[str, dict]]:
    row = row_from_note(parse_note(markdown, 1))
    return [row] if row else []


def to_notes(fronts: list[dict]) -> list[VaultNote]:
    return [VaultNote(index=i, front=front, body="") for i, front in enumerate(fronts, start=1)]
