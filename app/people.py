"""Известные люди: сопоставление имён и поиск новых.

Сервер знает только имена, роли и псевдонимы (без телефонов и заметок).
"""
from __future__ import annotations

import re

# Слова, которые ИИ иногда кладёт в people, но это не имена.
GENERIC = frozenset({
    "жена", "муж", "мама", "папа", "мать", "отец", "брат", "сестра", "сын", "дочь", "тёща", "теща", "тесть",
    "свекровь", "свёкор", "друг", "подруга", "коллега", "начальник", "босс", "врач", "доктор", "сосед",
    "соседка", "клиент", "подрядчик", "мастер", "курьер", "я", "мы", "он", "она",
})


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).lower().replace("ё", "е")).strip()


def build_index(known: list[dict]) -> dict[str, str]:
    """Нормализованное имя или псевдоним -> каноническое имя."""
    index: dict[str, str] = {}
    for person in known:
        canon = person["name"]
        for variant in [canon, *person.get("aliases", [])]:
            index.setdefault(norm(variant), canon)
        first = canon.split()[0] if canon.split() else canon
        index.setdefault(norm(first), canon) if len(canon.split()) > 1 else None
    return index


def canonical(name: str, index: dict[str, str]) -> str:
    return index.get(norm(name), name.strip())


def is_new(name: str, index: dict[str, str]) -> bool:
    name = name.strip()
    if not name or not name[0].isupper() or norm(name) in GENERIC:
        return False
    return norm(name) not in index


def describe_for_llm(known: list[dict], limit: int = 200) -> str:
    rows = []
    for person in known[:limit]:
        extra = []
        if person.get("role"):
            extra.append(person["role"])
        if person.get("aliases"):
            extra.append("также: " + ", ".join(person["aliases"][:4]))
        rows.append(f"- {person['name']}" + (f" ({'; '.join(extra)})" if extra else ""))
    return "\n".join(rows)
