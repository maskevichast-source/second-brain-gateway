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
    """Нормализованное имя или псевдоним -> каноническое имя.

    Имя без фамилии («Ирина») считается псевдонимом, только если оно одно на всех известных людей. Если Ирин две,
    просто «Ирина» не привязывается ни к одной из них (значение пустое): человек известен, но кто именно, неизвестно."""
    index: dict[str, str] = {}
    for person in known:
        canon = person["name"]
        for variant in [canon, *person.get("aliases", [])]:
            index.setdefault(norm(variant), canon)
    firsts: dict[str, set[str]] = {}
    for person in known:
        canon = person["name"]
        if len(canon.split()) > 1:
            firsts.setdefault(norm(canon.split()[0]), set()).add(canon)
    for first, owners in firsts.items():
        if first in index:                       # так же зовут человека с одним именем или чей-то псевдоним
            continue
        index[first] = next(iter(owners)) if len(owners) == 1 else ""
    return index


def canonical(name: str, index: dict[str, str]) -> str:
    return index.get(norm(name)) or name.strip()


def _stem(word: str) -> str:
    return norm(word)[:4]


def mentioned(name: str, text: str) -> bool:
    """Звучит ли название (проект, место, вещь) в тексте: общий корень слова или очень похожее слово.
    Защита от выдуманных связей: «Идея Декор» не должна появляться в записи про человека из другой компании."""
    import difflib
    words = re.findall(r"[a-zа-яё0-9]+", norm(text))
    stems = {w[:4] for w in words}
    for token in re.findall(r"[a-zа-яё0-9]+", norm(name)):
        if len(token) < 3:
            continue
        if token[:4] in stems or any(difflib.SequenceMatcher(None, token, w).ratio() >= 0.75 for w in words if abs(len(w) - len(token)) <= 3):
            return True
    return False


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
