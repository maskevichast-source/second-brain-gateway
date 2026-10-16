"""Исходники: оригиналы того, что человек прислал боту (голос, PDF, фото, скриншоты, файлы).

Сервер не хранит файлы. В очередь кладётся только «квитанция»: идентификатор файла в Telegram, имя и папка.
Когда ноутбук выгружает очередь, он просит сервер отдать байты (`GET /file/{id}`), сервер скачивает файл
из Telegram и сразу передаёт его дальше. Скрипт кладёт файл в `7-Исходники/ГОД-МЕСЯЦ`, а заметки
ссылаются на него: свойство `source_files` и блок «Исходники» с `![[имя файла]]`.
Исходный текст (сообщения, ссылки, расшифровка голоса) и так дословно лежит в блоке «Оригинал» самой заметки.
"""
from __future__ import annotations

import contextvars
import os

from .frontmatter import emit
from .textutil import file_suffix, slugify

ROOT = "7-Исходники"
MAX_BYTES = 20 * 1024 * 1024            # больше Telegram не отдаёт ботам
PDF = "application/pdf"

# Источники текущего сообщения. Устанавливается в обработчике Telegram, читается при сохранении заметок.
CURRENT: contextvars.ContextVar[list] = contextvars.ContextVar("sb_sources", default=[])

_EXT_BY_MIME = {
    "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "text/plain": ".txt",
}
_EMBED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".ogg", ".mp3", ".m4a", ".wav", ".webm",
              ".mp4", ".mov", ".svg", ".bmp"}


def _item(kind: str, node: dict, default_name: str, default_mime: str) -> dict | None:
    file_id = str(node.get("file_id") or "")
    if not file_id:
        return None
    return {"file_id": file_id, "kind": kind, "name": str(node.get("file_name") or default_name),
            "mime": str(node.get("mime_type") or default_mime), "size": int(node.get("file_size") or 0)}


def collect(message: dict) -> tuple[list[dict], list[str]]:
    """Вложения сообщения -> (то, что можно сохранить, названия слишком больших файлов)."""
    found: list[dict] = []
    photos = message.get("photo") or []
    if photos:
        found.append(_item("фото", photos[-1], "photo.jpg", "image/jpeg"))
    doc = message.get("document")
    if isinstance(doc, dict):
        mime = str(doc.get("mime_type") or "")
        name = str(doc.get("file_name") or "")
        kind = ("pdf" if mime == PDF or name.lower().endswith(".pdf")
                else "скрин" if mime.startswith("image/") else "файл")
        found.append(_item(kind, doc, "file", mime or "application/octet-stream"))
    for key, kind, name, mime in (("voice", "голос", "voice.ogg", "audio/ogg"), ("audio", "аудио", "audio.mp3", "audio/mpeg"),
                                  ("video_note", "кружок", "video-note.mp4", "video/mp4"),
                                  ("video", "видео", "video.mp4", "video/mp4")):
        node = message.get(key)
        if isinstance(node, dict):
            found.append(_item(kind, node, name, mime))
    ok, too_big = [], []
    for item in found:
        if not item:
            continue
        if item["size"] > MAX_BYTES:
            too_big.append(item["name"])
        else:
            ok.append(item)
    return ok, too_big


def _extension(item: dict) -> str:
    ext = os.path.splitext(item["name"])[1].lower()
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext
    return _EXT_BY_MIME.get(item["mime"], ".bin")


def plan(sources: list[dict], capture_id: str, date: str) -> list[dict]:
    """Имена и папка для каждого исходника. Имя стабильно: повторная обработка даёт то же имя."""
    month = date[:7] if len(date) >= 7 else "без-даты"
    result = []
    for number, item in enumerate(sources, start=1):
        stem = slugify(os.path.splitext(item["name"])[0], 40) if item["kind"] in ("pdf", "файл", "аудио") else item["kind"]
        filename = f"{date}-{stem}-{file_suffix(f'{capture_id}-{number}')}{_extension(item)}"
        result.append({**item, "filename": filename, "folder": f"{ROOT}/{month}"})
    return result


def embed(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return f"![[{filename}]]" if ext in _EMBED_EXT else f"[[{filename}]]"


def attach(markdown: str, planned: list[dict]) -> str:
    """Добавляет в заметку свойство source_files и блок «Исходники» в конце. Идемпотентно."""
    if not planned or "source_files:" in markdown:
        return markdown
    lines = markdown.split("\n")
    if not lines or lines[0].strip() != "---":
        return markdown
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            prop = emit("source_files", [f"[[{p['filename']}]]" for p in planned])
            lines[i:i] = prop
            break
    else:
        return markdown
    text = "\n".join(lines).rstrip("\n")
    block = "## Исходники\n\n" + "\n".join(f"- {embed(p['filename'])}" for p in planned)
    return f"{text}\n\n{block}\n"
