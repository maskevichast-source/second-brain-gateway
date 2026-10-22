"""Разбор карточек людей по одной: бот спрашивает, кто это, а ненужный контакт можно убрать в архив.

Работает по срезу на сервере. Карточка уходит в `3-Архив/Удалённые-контакты`, а не стирается: ноутбук
применяет служебную запись «archive» (tools/vault_sync.py), и файл можно вернуть руками.
"""
from __future__ import annotations

import json
from typing import Any

from .textutil import one_line
from .vault import as_list

DONE_SETTING = "contacts_done"
MAX_DONE = 5000


def _val(front: dict, key: str) -> str:
    value = front.get(key)
    return ", ".join(as_list(value)) if isinstance(value, list) else str(value or "").strip()


def needs_info(front: dict) -> bool:
    """Карточка без ответа на главный вопрос «кто это»."""
    if front.get("type") != "person" or _val(front, "status") == "неактуален":
        return False
    return not (_val(front, "relation") or _val(front, "relation_group") or _val(front, "how_met"))


def title_key(front: dict) -> str:
    """Что уже разобрано, помним по имени: после ответа карточка получает новый ключ в срезе."""
    return " ".join(str(front.get("title", "")).lower().replace("ё", "е").split())


def load_done(raw: str) -> set[str]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def dump_done(done: set[str]) -> str:
    return json.dumps(sorted(done)[-MAX_DONE:], ensure_ascii=False)


def build_queue(items: list[tuple[str, dict]], done: set[str]) -> list[str]:
    """Ключи (12 символов) карточек, о которых ещё нет данных. Сначала те, где хоть что-то известно (телефон, компания)."""
    rows = []
    for key, front in items:
        short = key[:12]
        if title_key(front) in done or not needs_info(front):
            continue
        rank = 0 if (_val(front, "phones") or _val(front, "company") or _val(front, "email")) else 1
        rows.append((rank, str(front.get("title", "")).lower(), short))
    rows.sort()
    return [short for _, _, short in rows]


def describe(front: dict) -> str:
    known = []
    for label, key in (("телефон", "phones"), ("компания", "company"), ("должность", "role"), ("почта", "email"),
                       ("город", "city"), ("день рождения", "birthday")):
        value = _val(front, key)
        if value:
            known.append(f"{label}: {one_line(value, 60)}")
    return "; ".join(known) if known else "в карточке только имя"


def question(front: dict, position: int, total: int) -> str:
    return (f"👤 {front.get('title', '?')}  ({position + 1} из {total})\n"
            f"Что известно: {describe(front)}\n\n"
            "Кто это и откуда вы знакомы? Ответь одним сообщением (например: «сосед по даче, помог с ремонтом»). "
            "Или нажми кнопку: пропустить, убрать контакт в архив, остановиться.")


def buttons(key: str) -> list[list[tuple[str, str]]]:
    return [[("⏭ Пропустить", f"cs:{key}"), ("🗑 Убрать в архив", f"cd:{key}")], [("⛔ Стоп", f"cq:{key}")]]


def confirm_buttons(key: str) -> list[list[tuple[str, str]]]:
    return [[("Да, убрать", f"cy:{key}"), ("Нет", f"cn:{key}")]]


def archive_payload(front: dict[str, Any]) -> dict:
    return {"action": "archive", "title": str(front.get("title", "")), "created": str(front.get("created", "")),
            "type": "person"}
