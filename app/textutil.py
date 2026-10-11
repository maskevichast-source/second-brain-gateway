"""Маленькие чистые функции: имена файлов, теги, YAML-строки, цитаты.

Всё, что попадает в имя файла и в заголовок заметки, формируется здесь, в обычном коде,
а не нейросетью: так модель не может сломать имя файла или YAML.
"""
from __future__ import annotations

import hashlib
import json
import re

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_LEADING_YAML_RE = re.compile(r"\A\s*---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def is_valid_capture_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


def single_url(text: str) -> str | None:
    """Вернёт ссылку, если весь текст — это одна ссылка, иначе None."""
    candidate = text.strip()
    return candidate if _URL_RE.match(candidate) else None


def file_suffix(capture_id: str) -> str:
    """Короткий стабильный хвост для имени файла — защита от одинаковых имён."""
    return hashlib.sha1(capture_id.encode("utf-8")).hexdigest()[:6]


def one_line(text: str, limit: int = 100) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed[:limit].rstrip()


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


def yaml_quote(value: str) -> str:
    """Строка в двойных кавычках. JSON-строка — это корректный скаляр YAML."""
    return json.dumps(one_line(value, 200), ensure_ascii=False)


def strip_leading_yaml(body: str) -> str:
    """Если модель всё же вставила свой блок --- ... --- в начало, убираем его."""
    return _LEADING_YAML_RE.sub("", body or "", count=1)


def quote_block(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
