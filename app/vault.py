"""Чтение заметок хранилища, которые шорткат присылает вместе с вопросом.

Тексты заметок на сервере не сохраняются: они живут только в памяти на время одного запроса.
Заметки с `private: true` отбрасываются сразу после разбора и до ИИ не доходят.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

from .textutil import one_line, parse_date

SEPARATOR = "=====NOTE====="
_FRONT_START = re.compile(r"(?m)^---[ \t]*\n(?=schema:)")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# Поля, которые стоит показывать ИИ в сжатом виде (кроме общих).
EXTRA_FIELDS = (
    "due", "remind_at", "event_at", "next_due", "deadline", "valid_until", "warranty_until",
    "review_at", "follow_up_due", "date_from", "date_to", "finished_at", "birthday",
    "recommended_by", "creator", "year", "platform", "city", "address", "price",
    "company", "role", "phones", "email", "for_whom", "destination", "counterparty",
    "topic", "agreements", "next_step", "next_action", "repeat", "schedule",
)


@dataclass
class VaultNote:
    index: int
    front: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    def get(self, key: str, default: Any = "") -> Any:
        value = self.front.get(key, default)
        return default if value is None else value

    @property
    def type(self) -> str:
        return str(self.get("type"))

    @property
    def title(self) -> str:
        title = str(self.get("title")).strip()
        if title:
            return title
        first = next((line.strip("# ").strip() for line in self.body.splitlines() if line.strip()), "")
        return one_line(first, 80) or f"Заметка {self.index}"

    @property
    def created(self) -> str:
        return str(self.get("created"))

    @property
    def is_private(self) -> bool:
        return str(self.get("private")).lower() in ("true", "yes", "да", "1")

    @property
    def people(self) -> list[str]:
        return as_list(self.get("people"))


def as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    return [unlink(str(item)) for item in items if item not in (None, "")]


def unlink(text: str) -> str:
    return _WIKILINK.sub(r"\1", text).strip()


def _plain(value: Any) -> Any:
    """Приводит значения YAML к строкам: даты -> ISO, списки -> списки строк."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def split_corpus(corpus: str) -> list[str]:
    """Делит присланный текст на заметки: по маркеру или, если его нет, по началу свойств."""
    text = corpus.replace("\r\n", "\n").strip()
    if not text:
        return []
    if SEPARATOR in text:
        chunks = text.split(SEPARATOR)
    else:
        starts = [m.start() for m in _FRONT_START.finditer(text)]
        if not starts:
            return [text]
        starts.append(len(text))
        chunks = [text[a:b] for a, b in zip(starts, starts[1:])]
        if starts[0] > 0 and text[: starts[0]].strip():
            chunks.insert(0, text[: starts[0]])
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def parse_note(raw: str, index: int) -> VaultNote:
    front: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        lines = raw.split("\n")
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            block = "\n".join(lines[1:end])
            body = "\n".join(lines[end + 1:])
            try:
                loaded = yaml.load(block, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))  # noqa: S506
            except yaml.YAMLError:
                loaded = None
            if isinstance(loaded, dict):
                front = {str(k): _plain(v) for k, v in loaded.items()}
    return VaultNote(index=index, front=front, body=body.strip())


def load_notes(corpus: str) -> tuple[list[VaultNote], int]:
    """Возвращает (заметки без приватных, число отброшенных приватных).

    Файлы без свойства `type` (стартовые страницы, дашборды, шаблоны) заметками не считаются.
    """
    notes: list[VaultNote] = []
    private = 0
    for i, chunk in enumerate(split_corpus(corpus), start=1):
        note = parse_note(chunk, i)
        if note.is_private:
            private += 1
            continue
        if not note.type:
            continue
        notes.append(note)
    return notes, private


def strip_original(body: str) -> str:
    """Блок «Оригинал» повторяет текст заметки; ИИ он не нужен."""
    return re.split(r"\n> \[!quote\]", "\n" + body, maxsplit=1)[0].strip()


def compact_line(note: VaultNote, body_chars: int = 160) -> str:
    kind = f"{note.type}/{note.get('kind')}" if note.get("kind") else note.type
    parts = [f"[{note.index}] {kind or 'заметка'}"]
    for label, key in (("статус", "status"), ("сфера", "area"), ("записано", "created")):
        value = str(note.get(key)).strip()
        if value:
            parts.append(f"{label}: {value[:16] if key == 'created' else value}")
    parts.append(f"«{note.title}»")
    summary = one_line(str(note.get("summary")), 140)
    if summary and summary != note.title:
        parts.append(summary)
    if note.people:
        parts.append("люди: " + ", ".join(note.people[:6]))
    tags = as_list(note.get("tags"))
    if tags:
        parts.append("теги: " + ", ".join(tags[:6]))
    extras = []
    for key in EXTRA_FIELDS:
        value = note.get(key)
        if value in ("", None, []):
            continue
        shown = ", ".join(as_list(value)) if isinstance(value, list) else unlink(str(value))
        extras.append(f"{key}={one_line(shown, 60)}")
    if extras:
        parts.append("; ".join(extras))
    body = one_line(strip_original(note.body), body_chars)
    if body and body != summary:
        parts.append("текст: " + body)
    return " | ".join(parts)


# ---------- грубый поиск по словам (запасной вариант и предотбор) ----------

_STOP = {
    "что", "как", "где", "когда", "какие", "какой", "какая", "какое", "кто", "кому", "про", "для", "это",
    "мне", "меня", "мой", "моя", "мои", "моё", "есть", "был", "была", "были", "было", "надо", "нужно",
    "все", "всё", "там", "тут", "или", "при", "над", "под", "без", "уже", "ещё", "еще", "дай", "покажи",
    "расскажи", "напомни", "найди", "хотел", "хотела", "планировал", "планировала", "записывал",
}


def keywords(question: str) -> list[str]:
    words = re.findall(r"[\w-]+", question.lower(), flags=re.UNICODE)
    keys = []
    for word in words:
        if len(word) < 3 or word in _STOP:
            continue
        keys.append(word[: max(4, len(word) - 2)])
    return list(dict.fromkeys(keys))


def score(note: VaultNote, keys: list[str], line: str | None = None) -> int:
    haystack = (line or compact_line(note, 400)).lower()
    return sum(3 for key in keys if key in haystack)
