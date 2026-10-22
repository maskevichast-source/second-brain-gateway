"""Кто здесь владелец. «Маскевич Владислав», «Владислав Маскевич» и «Влад» (если задан псевдоним) — один человек: я.

Имена хранятся в настройке owner_names (JSON-список). Первое имя берётся из Telegram (имя и фамилия аккаунта),
остальное задаётся командой /me. Сравнение не зависит от порядка слов, регистра и «ё».
Один общий список на процесс: бот одного человека, а вызовов много (сводки, радар, карточки).
"""
from __future__ import annotations

import json
import re

SETTING = "owner_names"
_NAMES: list[str] = []


def tokens(name: str) -> frozenset[str]:
    text = re.split(r"\s+[—–-]\s+|\s*\(", str(name or ""))[0]         # «Имя — профиль», «Имя (дубль)» -> «Имя»
    words = re.findall(r"[\w]+", text.lower().replace("ё", "е"), flags=re.UNICODE)
    return frozenset(words)


def set_names(names: list[str]) -> None:
    _NAMES[:] = [n for n in dict.fromkeys(" ".join(str(n).split()) for n in names) if n]


def names() -> list[str]:
    return list(_NAMES)


def is_owner(name: str) -> bool:
    mine = tokens(name)
    if not mine:
        return False
    for owner in _NAMES:
        theirs = tokens(owner)
        if theirs and mine == theirs:
            return True
    return False


def load(db) -> list[str]:
    try:
        data = json.loads(db.get_setting(SETTING, "") or "[]")
    except ValueError:
        data = []
    set_names([str(x) for x in data] if isinstance(data, list) else [])
    return names()


def save(db, new_names: list[str]) -> None:
    set_names(new_names)
    db.set_setting(SETTING, json.dumps(names(), ensure_ascii=False))


def learn_from_telegram(db, user: dict) -> bool:
    """Первый запуск: имя и фамилия аккаунта Telegram. Уже заданное не трогаем. True, если что-то записали."""
    if load(db):
        return False
    first, last = str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()
    if not (first and last):
        return False
    save(db, [f"{first} {last}"])
    return True


def describe() -> str:
    return ", ".join(_NAMES)
