"""Маленькие чистые функции: имена файлов, теги, даты, ссылки-вики, цитаты.

Всё, что попадает в имя файла и в блок свойств заметки, формируется здесь, в обычном коде,
а не нейросеть: так модель не может сломать имя файла или YAML.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_URL_FIND_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_LEADING_YAML_RE = re.compile(r"\A\s*---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_PRIVATE_RE = re.compile(r"^\s*(?:!+|секрет\s*[:;,.\-—])\s*", re.IGNORECASE)
_WIKI_BAD_RE = re.compile(r"[\[\]|#^\\/:*?\"<>]")


def is_valid_capture_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


def single_url(text: str) -> str | None:
    """Вернёт ссылку, если весь текст — это одна ссылка, иначе None."""
    candidate = text.strip()
    return candidate if _URL_RE.match(candidate) else None


def find_urls(text: str, limit: int = 3) -> list[str]:
    found: list[str] = []
    for match in _URL_FIND_RE.findall(text or ""):
        url = match.rstrip(".,;:!?»")
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def split_private(text: str) -> tuple[bool, str]:
    """Режим «без ИИ»: запись начинается с «!» или «секрет:». Возвращает (приватно, текст без префикса)."""
    match = _PRIVATE_RE.match(text or "")
    if not match:
        return False, text
    return True, text[match.end():].strip()


def file_suffix(seed: str) -> str:
    """Короткий стабильный хвост для имени файла — защита от одинаковых имён."""
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]


def one_line(text: str, limit: int = 100) -> str:
    collapsed = re.sub(r"\s+", " ", str(text) if text is not None else "").strip()
    return collapsed[:limit].rstrip()


def trim_words(text: str, limit: int) -> str:
    """Обрезает по границе слова, если текст длиннее лимита."""
    text = one_line(text, 10_000)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,;:-—") + "…"


def slugify(title: str, max_len: int = 50) -> str:
    slug = (title or "").lower()
    slug = re.sub(r"[^\w]+", "-", slug, flags=re.UNICODE).replace("_", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "zametka"


def clean_tags(tags: list[str], limit: int = 8) -> list[str]:
    result: list[str] = []
    for raw in tags:
        tag = str(raw).strip().lstrip("#").lower()
        tag = re.sub(r"\s+", "-", tag)
        tag = re.sub(r"[^\w/-]", "", tag, flags=re.UNICODE)
        tag = tag.strip("-/")[:30]
        if not tag or tag.isdigit() or tag in result:
            continue
        result.append(tag)
        if len(result) >= limit:
            break
    return result


def strip_leading_yaml(body: str) -> str:
    """Если модель всё же вставила свой блок --- ... --- в начало, убираем его."""
    return _LEADING_YAML_RE.sub("", body or "", count=1)


def quote_block(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def wikilink(name: object) -> str | None:
    """'Серик', '[[Серик]]', ' Серик  Иванов ' -> '[[Серик Иванов]]'. Пусто -> None."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    text = text.removeprefix("[[").removesuffix("]]")
    text = _WIKI_BAD_RE.sub("", text).strip(" .,;")[:60].strip()
    return f"[[{text}]]" if text else None


def wikilinks(names: object, limit: int = 12) -> list[str]:
    if not isinstance(names, (list, tuple)):
        names = [names] if names else []
    result: list[str] = []
    for name in names:
        link = wikilink(name)
        if link and link not in result:
            result.append(link)
        if len(result) >= limit:
            break
    return result


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")


def parse_date(value: object) -> str | None:
    """Принимает 2026-10-14 или 2026-10-14T18:00, возвращает только дату или None."""
    match = _DATE_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
    except ValueError:
        return None


def parse_datetime(value: object) -> str | None:
    """Возвращает 'YYYY-MM-DD' или 'YYYY-MM-DDTHH:mm', если время было указано."""
    match = _DATE_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        day = date(int(match[1]), int(match[2]), int(match[3]))
        if match[4] is None:
            return day.isoformat()
        datetime(day.year, day.month, day.day, int(match[4]), int(match[5]))
    except ValueError:
        return None
    return f"{day.isoformat()}T{match[4]}:{match[5]}"


def parse_month_day(value: object) -> str | None:
    """'03-05' или '1990-03-05' -> строка как есть (после проверки), иначе None."""
    text = str(value or "").strip()
    full = parse_date(text) if len(text) >= 10 else None
    if full:
        return full
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if not match:
        return None
    month, day = int(match[1]), int(match[2])
    try:
        date(2000, month, day)  # 2000 — високосный, 29 февраля допустимо
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}"
