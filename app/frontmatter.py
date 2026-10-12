"""Сборка блока свойств (YAML) в начале заметки. Значения экранируются здесь, а не нейросетью."""
from __future__ import annotations

import json
import re

from .textutil import one_line

_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}
_TAG_OK = re.compile(r"^[\w/-]+$", re.UNICODE)


class Raw(str):
    """Значение, которое пишется без кавычек (даты и время в формате ISO)."""


def _quoted(text: object, limit: int = 500) -> str:
    return json.dumps(one_line(text, limit), ensure_ascii=False)


def _tag(tag: str) -> str:
    plain = _TAG_OK.match(tag) and tag.lower() not in _RESERVED and not tag.replace(".", "").isdigit()
    return tag if plain else _quoted(tag, 60)


def _number(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def emit(key: str, value: object) -> list[str]:
    """Строки YAML для одного ключа. Пустые значения пропускаются."""
    if value is None or value == "" or value == [] or value == ():
        return []
    if isinstance(value, Raw):
        return [f"{key}: {value}"]
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    if isinstance(value, (int, float)):
        return [f"{key}: {_number(value)}"]
    if isinstance(value, (list, tuple)):
        items = [x for x in value if x not in (None, "")]
        if not items:
            return []
        if key == "tags":
            return [f"{key}:"] + [f"  - {_tag(str(x))}" for x in items]
        return [f"{key}:"] + [f"  - {_quoted(x, 200)}" for x in items]
    return [f"{key}: {_quoted(value)}"]


def dump(items: list[tuple[str, object]]) -> str:
    """Готовый блок '---' ... '---'."""
    lines = ["---"]
    for key, value in items:
        lines.extend(emit(key, value))
    lines.append("---")
    return "\n".join(lines)
