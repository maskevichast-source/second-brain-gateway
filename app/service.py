"""Сборка ответа: проверка того, что вернул ИИ, имена файлов, блок свойств и Markdown.

Всё, что попадает в файл, формирует этот код. ИИ предлагает только смысловые поля.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from . import schema
from .frontmatter import Raw, dump
from .links import LinkInfo
from .llm import LLMNote, LLMResult
from .schema import Field
from .textutil import (
    clean_tags,
    file_suffix,
    one_line,
    parse_date,
    parse_datetime,
    parse_month_day,
    quote_block,
    single_url,
    slugify,
    strip_leading_yaml,
    trim_words,
    wikilink,
    wikilinks,
)

FOLDER = "0-Inbox"
DEFAULT_QUESTION = "О чём эта заметка и к какому делу или человеку она относится?"
ORIGINAL_MAX_REPEAT = 600  # при нескольких заметках оригинал повторяется в каждой, только если он короткий


# ---------- приведение значений полей ----------

def _first(value):
    return value[0] if isinstance(value, (list, tuple)) and value else value


def _coerce(spec: Field, value):
    """Приводит значение от ИИ к нужному типу. None, если значение неподходящее или пустое."""
    if value is None or value == "" or value == [] or isinstance(value, dict):
        return None
    kind = spec.kind
    if kind == "str":
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v)
        text = one_line(value, 500)
        return text or None
    if kind == "list":
        if isinstance(value, str):
            value = [p for p in value.replace("\n", ",").replace(";", ",").split(",")]
        if not isinstance(value, (list, tuple)):
            return None
        items = [one_line(v, 200) for v in value if v not in (None, "")]
        items = [i for i in items if i][:15]
        return items or None
    if kind == "link":
        return wikilink(_first(value))
    if kind == "enum":
        text = one_line(_first(value), 60).lower()
        return text if text in spec.values else None
    if kind in ("int", "num"):
        try:
            number = float(str(_first(value)).replace(",", ".").replace(" ", ""))
        except ValueError:
            return None
        if spec.lo is not None and number < spec.lo:
            return None
        if spec.hi is not None and number > spec.hi:
            return None
        if kind == "int":
            return int(number)
        return int(number) if number.is_integer() else number
    if kind == "date":
        parsed = parse_date(_first(value))
        return Raw(parsed) if parsed else None
    if kind == "datetime":
        parsed = parse_datetime(_first(value))
        return Raw(parsed) if parsed else None
    if kind == "md":
        parsed = parse_month_day(_first(value))
        if not parsed:
            return None
        return Raw(parsed) if len(parsed) == 10 else parsed
    return None


# ---------- заметка ----------

@dataclass
class Note:
    type: str
    title: str
    items: list[tuple[str, object]]
    body: str
    original_block: str = ""

    def markdown(self) -> str:
        parts = [dump(self.items), "", self.body.strip()]
        if self.original_block:
            parts += ["", self.original_block]
        parts.append("")
        return "\n".join(parts)


def _client_name(client: str) -> str:
    cleaned = "".join(ch for ch in (client or "").lower() if ch.isalnum() or ch in "_-")[:20]
    return cleaned or "unknown"


def normalize_note(
    raw: LLMNote,
    *,
    content_urls: list[str],
    links: dict[str, LinkInfo],
    client: str,
    capture_id: str,
    now: datetime,
    fallback_body: str,
    private: bool = False,
) -> Note:
    note_type = raw.type if raw.type in schema.TYPES else schema.DEFAULT_TYPE
    spec = schema.TYPES[note_type]

    kind = None
    if spec.kinds:
        if raw.kind in spec.kinds:
            kind = raw.kind
        elif "другое" in spec.kinds:
            kind = "другое"
    status = None
    if spec.statuses:
        status = raw.status if raw.status in spec.statuses else spec.statuses[0]
    area = raw.area if raw.area in schema.AREAS else schema.DEFAULT_AREA

    body = strip_leading_yaml(raw.body).strip()
    title = trim_words(raw.title or body or fallback_body, 80) or "Заметка"
    summary = trim_words(raw.summary or title, 140)

    # особые поля типа: только известные, приведённые к типу
    fields: dict[str, object] = {}
    for name, field_spec in spec.fields.items():
        coerced = _coerce(field_spec, raw.fields.get(name))
        if coerced is not None:
            fields[name] = coerced

    # ссылка: только та, что реально была в записи
    source_url = raw.source_url if raw.source_url in content_urls else None
    info = links.get(source_url) if source_url else None
    if info:
        if note_type == "media":
            if info.channel and "creator" not in fields:
                fields["creator"] = info.channel
            if info.is_youtube and "platform" not in fields:
                fields["platform"] = "YouTube"
        if note_type == "bookmark":
            if info.domain and "domain" not in fields:
                fields["domain"] = info.domain
            if info.title and "page_title" not in fields:
                fields["page_title"] = info.title

    ai_filled = [f for f in raw.ai_filled if f in schema.AI_FILLABLE and f in fields]
    priority = raw.priority if raw.priority in schema.PRIORITIES else None

    head = {
        "schema": schema.SCHEMA_VERSION,
        "title": title,
        "summary": summary,
        "created": Raw(now.strftime("%Y-%m-%dT%H:%M")),
        "type": note_type,
        "kind": kind,
        "status": status,
        "stage": "inbox",
        "area": area,
    }
    tail = {
        "priority": priority,
        "tags": clean_tags(raw.tags),
        "people": wikilinks(raw.people),
        "related": wikilinks(raw.related),
        "source": _client_name(client),
        "source_url": source_url,
        "private": True if private else None,
        "ai_filled": ai_filled,
        "capture_id": capture_id,
        "confidence": round(raw.confidence, 2),
    }
    items = list(head.items()) + list(fields.items()) + list(tail.items())

    if source_url and source_url not in body:
        body = f"**Ссылка:** {source_url}" + (f"\n\n{body}" if body else "")
    if not body:
        body = fallback_body
    return Note(type=note_type, title=title, items=items, body=body)


# ---------- запасные заметки, когда ИИ недоступен ----------

def _fallback_notes(content: str, links: list[LinkInfo]) -> list[LLMNote]:
    link = single_url(content)
    info = links[0] if link and links and links[0].url == link else None
    if link and info and info.is_youtube and info.title:
        return [LLMNote(
            type="media", kind="видео", status="хочу", area="отдых", title=info.title,
            summary=f"Видео на YouTube: {info.title}", tags=[], source_url=link,
            fields={"creator": info.channel, "platform": "YouTube"}, confidence=0.5,
        )]
    if link:
        domain = urlparse(link).hostname or ""
        title = info.title if info and info.title else f"Ссылка: {domain.removeprefix('www.')}"
        body = info.description if info and info.description else ""
        return [LLMNote(
            type="bookmark", status="не-разобрано", title=title, summary=title, tags=["unprocessed"],
            source_url=link, body=body, confidence=0.0,
        )]
    first = next((line for line in content.splitlines() if line.strip()), "Заметка")
    return [LLMNote(
        type="note", title=trim_words(first, 60), summary=trim_words(first, 140),
        tags=["unprocessed"], body=content, confidence=0.0,
    )]


# ---------- ответ ----------

def _original_block(content: str, clarifications: str) -> str:
    text = content.strip()
    if clarifications.strip():
        text += f"\n\nУточнение: {clarifications.strip()}"
    return "> [!quote]- Оригинал\n" + quote_block(text)


def _assemble(notes: list[Note], capture_id: str, now: datetime) -> list[dict]:
    files = []
    for index, note in enumerate(notes):
        seed = capture_id if len(notes) == 1 else f"{capture_id}-{index + 1}"
        filename = f"{now:%Y-%m-%d}-{slugify(note.title)}-{file_suffix(seed)}.md"
        files.append({
            "filename": filename,
            "folder": FOLDER,
            "type": note.type,
            "title": note.title,
            "markdown": note.markdown(),
        })
    return files


def _message(files: list[dict], *, needs_review: bool, fallback: bool, private: bool) -> str:
    if needs_review:
        return "Нужно уточнение"
    if private:
        return "Сохранено без ИИ"
    if fallback:
        return "ИИ недоступен — заметка сохранена как есть"
    if len(files) == 1:
        return f"{schema.TYPES[files[0]['type']].name}: {files[0]['title']}"
    return f"Сохранено заметок: {len(files)}"


def build_response(
    *,
    capture_id: str,
    revision: int,
    client: str,
    content: str,
    clarifications: str,
    result: LLMResult | None,
    links: list[LinkInfo],
    content_urls: list[str],
    now: datetime,
    max_notes: int = 10,
) -> dict:
    content = content.strip()
    clarifications = clarifications.strip()
    link_map = {info.url: info for info in links}
    fallback = result is None
    raw_notes = _fallback_notes(content, links) if fallback else list(result.notes)

    overflow = len(raw_notes) > max_notes
    raw_notes = raw_notes[:max_notes]
    single = len(raw_notes) == 1 and not overflow
    # единственная ссылка в записи принадлежит единственной заметке
    if single and len(content_urls) == 1 and not raw_notes[0].source_url:
        raw_notes[0] = raw_notes[0].model_copy(update={"source_url": content_urls[0]})

    notes: list[Note] = []
    show_original_everywhere = single or len(content) <= ORIGINAL_MAX_REPEAT
    for index, raw in enumerate(raw_notes):
        note = normalize_note(
            raw,
            content_urls=content_urls,
            links=link_map,
            client=client,
            capture_id=capture_id if single else f"{capture_id}-{index + 1}",
            now=now,
            fallback_body=content if single else "",
        )
        if show_original_everywhere or index == 0:
            note.original_block = _original_block(content, clarifications)
        notes.append(note)

    if overflow:
        rest = normalize_note(
            LLMNote(type="note", title="Остаток записи", summary="Запись длиннее лимита заметок",
                    tags=["не-разобрано"], body=content, confidence=0.0),
            content_urls=content_urls, links=link_map, client=client,
            capture_id=f"{capture_id}-rest", now=now, fallback_body=content,
        )
        notes.append(rest)

    # вопросы: только для первой ревизии и не для одиночной ссылки
    needs_review = bool(result and result.needs_review and revision == 1 and not single_url(content))
    questions: list[str] = []
    if needs_review:
        questions = [q.strip() for q in result.review_questions if q.strip()][:2] or [DEFAULT_QUESTION]

    files = _assemble(notes, capture_id, now)
    first = files[0]
    return {
        "status": "success",
        "schema": schema.SCHEMA_VERSION,
        "capture_id": capture_id,
        "revision": revision,
        "needs_review": needs_review,
        "needs_review_flag": "yes" if needs_review else "no",
        "review_questions": questions,
        "review_text": "\n".join(questions),
        "fallback": fallback,
        "private": False,
        "count": len(files),
        "message": _message(files, needs_review=needs_review, fallback=fallback, private=False),
        "files": files,
        # первый файл дублируется на верхнем уровне: старый шорткат продолжает работать
        "filename": first["filename"],
        "folder": first["folder"],
        "markdown": first["markdown"],
    }


def build_private_response(*, capture_id: str, client: str, text: str, now: datetime) -> dict:
    """Режим «без ИИ»: текст не покидает сервер и нигде не сохраняется."""
    first_line = next((line for line in text.splitlines() if line.strip()), "Секретная заметка")
    raw = LLMNote(type="note", title=trim_words(first_line, 60), tags=[], body=text, confidence=1.0)
    note = normalize_note(
        raw, content_urls=[], links={}, client=client, capture_id=capture_id,
        now=now, fallback_body=text, private=True,
    )
    files = _assemble([note], capture_id, now)
    return {
        "status": "success",
        "schema": schema.SCHEMA_VERSION,
        "capture_id": capture_id,
        "revision": 1,
        "needs_review": False,
        "needs_review_flag": "no",
        "review_questions": [],
        "review_text": "",
        "fallback": False,
        "private": True,
        "count": 1,
        "message": "Сохранено без ИИ",
        "files": files,
        "filename": files[0]["filename"],
        "folder": files[0]["folder"],
        "markdown": files[0]["markdown"],
    }
