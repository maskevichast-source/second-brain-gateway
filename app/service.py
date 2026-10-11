"""Сборка ответа: имя файла, YAML и Markdown формирует код, а не нейросеть."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from .llm import LLMNote
from .textutil import (
    clean_tags,
    file_suffix,
    one_line,
    quote_block,
    single_url,
    slugify,
    strip_leading_yaml,
    yaml_quote,
)

FOLDER = "0-Inbox"
DEFAULT_QUESTION = "О чём эта заметка и к какому проекту или делу она относится?"


def _clean_client(client: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "", (client or "").lower())[:20]
    return cleaned or "unknown"


def _fallback_title(content: str, link: str | None) -> str:
    if link:
        host = urlparse(link).netloc
        return f"Ссылка: {host}" if host else "Ссылка"
    for line in content.splitlines():
        if line.strip():
            return one_line(line, 60)
    return "Заметка"


def build_response(
    *,
    capture_id: str,
    revision: int,
    client: str,
    input_type: str,
    content: str,
    clarifications: str,
    note: LLMNote | None,
    now: datetime,
) -> dict:
    content = content.strip()
    clarifications = clarifications.strip()
    link = single_url(content)
    client = _clean_client(client)
    is_fallback = note is None

    # --- смысловые поля ---
    if note is not None:
        title = one_line(note.title, 100) or _fallback_title(content, link)
        note_type = note.note_type
        tags = clean_tags(note.tags)
        confidence = round(note.confidence, 2)
        ai_body = strip_leading_yaml(note.content).strip()
    else:
        title = _fallback_title(content, link)
        note_type = "resource" if link else "journal"
        tags = ["unprocessed"]
        confidence = 0.0
        ai_body = ""

    # --- уточнения (human-in-the-loop) ---
    needs_review = bool(note and note.needs_review and revision == 1 and not link)
    questions: list[str] = []
    if needs_review:
        questions = [q.strip() for q in note.review_questions if q.strip()][:2] or [DEFAULT_QUESTION]

    # --- тело заметки ---
    if link:
        body_parts = [f"**Ссылка:** {link}"]
        if ai_body:
            body_parts.append(ai_body)
        body = "\n\n".join(body_parts)
    elif is_fallback or not ai_body:
        body = content
        if clarifications:
            body += f"\n\n**Уточнение:** {clarifications}"
    else:
        original = content
        if clarifications:
            original += f"\n\nУточнение: {clarifications}"
        body = f"{ai_body}\n\n> [!quote]- Оригинал\n{quote_block(original)}"

    # --- имя файла и YAML ---
    created = now.strftime("%Y-%m-%d %H:%M")
    filename = f"{now:%Y-%m-%d}-{slugify(title)}-{file_suffix(capture_id)}.md"

    front = {
        "title": title,
        "created": created,
        "type": note_type,
        "tags": tags,
        "source": client,
        "status": "inbox",
        "confidence": confidence,
    }

    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        f'created: "{created}"',
        f'type: "{note_type}"',
    ]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    else:
        lines.append("tags: []")
    lines += [
        f"source: {yaml_quote(client)}",
        'status: "inbox"',
        f"confidence: {confidence}",
        f"capture_id: {yaml_quote(capture_id)}",
        "---",
        "",
        body,
        "",
    ]
    markdown = "\n".join(lines)

    if needs_review:
        message = "Нужно уточнение"
    elif is_fallback:
        message = "ИИ недоступен — заметка сохранена как есть"
    else:
        message = "Заметка сохранена"

    return {
        "status": "success",
        "capture_id": capture_id,
        "revision": revision,
        "needs_review": needs_review,
        "needs_review_flag": "yes" if needs_review else "no",
        "review_questions": questions,
        "review_text": "\n".join(questions),
        "fallback": is_fallback,
        "message": message,
        "filename": filename,
        "folder": FOLDER,
        "yaml_frontmatter": front,
        "markdown_body": body,
        "markdown": markdown,
    }
