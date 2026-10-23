"""Связи между людьми, контактами, делами и событиями.

- Карточки вида «Имя Рабочий» узнаются как «Имя» (метка телефона не часть имени).
- Контакт с человеком (звонок, встреча, переписка) двигает last_contact в его карточке.
- Дело без проекта само привязывается к проекту, если название проекта прозвучало в деле.
- Всё считает код, без ИИ. Изменения карточек идут через обычную очередь служебных записей `set`.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import owner as owner_module
from .people import norm
from .textutil import parse_date, parse_datetime, wikilink
from .vault import as_list, parse_note

LABELS = frozenset({
    "рабочий", "рабочая", "рабочее", "домашний", "домашняя", "мобильный", "мобильная", "личный", "личная",
    "work", "home", "mobile", "cell", "office", "офис", "основной", "основная", "доп",
})
CLOSED_PROJECT = ("сделано", "отменено", "отложено", "завершён", "завершен", "закрыт")


def strip_label(name: str) -> str:
    """«Eichholtz Рабочий» -> «Eichholtz». Если после снятия метки ничего не осталось, имя не меняется."""
    words = str(name or "").split()
    while len(words) > 1 and (norm(words[-1]) in LABELS or words[-1].isdigit()):
        words.pop()
    return " ".join(words)


def _names_of(front: dict) -> set[str]:
    names = {norm(str(front.get("title", ""))), norm(str(front.get("name", "")))}
    names |= {norm(a) for a in as_list(front.get("aliases"))}
    names.discard("")
    return names


def find_card(items: list[tuple[str, dict]], name: str) -> tuple[str, dict] | None:
    """Карточка человека в срезе: точное имя или псевдоним, потом имя без метки («Имя Рабочий» -> «Имя»)."""
    wanted = norm(name)
    if not wanted:
        return None
    people = [(k, f) for k, f in items if f.get("type") == "person"]
    for key, front in people:
        if wanted in _names_of(front):
            return key, front
    base = norm(strip_label(name))
    for key, front in people:
        if base in _names_of(front) or base in {norm(strip_label(n)) for n in _names_of(front)}:
            return key, front
    return None


def _set_fields(db, row_key: str, front: dict, updates: dict, now: datetime) -> None:
    """Меняет карточку в срезе сразу, а в хранилище, когда ноутбук заберёт служебную запись."""
    db.snapshot_put([(row_key, {**front, **updates})])
    base = {"title": str(front.get("title", "")), "created": str(front.get("created", "")), "type": str(front.get("type", ""))}
    db.enqueue_service(f"set-{row_key[:12]}-{now:%Y%m%d%H%M%S}", f"set-{row_key[:12]}.json",
                       {"action": "set", **base, "fields": updates})


def mark_contact(db, row_key: str, front: dict, day: date, now: datetime) -> bool:
    """last_contact = day, если это новее записанного. True, если что-то изменилось."""
    current = parse_date(front.get("last_contact"))
    if current and date.fromisoformat(current) >= day:
        return False
    _set_fields(db, row_key, front, {"last_contact": day.isoformat()}, now)
    return True


def set_cadence(db, row_key: str, front: dict, days: int, now: datetime) -> None:
    _set_fields(db, row_key, front, {"cadence_days": days}, now)


def auto_contacts(db, files: list[dict], now: datetime) -> list[str]:
    """Контакты из только что сохранённых заметок двигают last_contact людей. Возвращает имена обновлённых карточек."""
    touched = []
    items = None
    for f in files:
        if f.get("type") != "interaction":
            continue
        note = parse_note(f["markdown"], 1)
        raw = parse_date(note.created)
        day = date.fromisoformat(raw) if raw else now.date()
        if day > now.date():
            day = now.date()
        for name in note.people:
            if owner_module.is_owner(name):
                continue
            items = items if items is not None else db.snapshot_items()
            found = find_card(items, name)
            if found and mark_contact(db, found[0], found[1], day, now):
                touched.append(str(found[1].get("title", name)))
    return touched


# ---------- проекты ----------

def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")) if len(w) >= 4}


def match_project(text: str, projects: list[str]) -> str | None:
    """Проект, название которого прозвучало в тексте: все значимые слова названия (до трёх) есть в тексте."""
    have = _stems(text)
    best, best_n = None, 0
    for title in projects:
        need = _stems(title)
        if not need or len(need) > 3:
            need = set(list(sorted(need))[:3]) if need else need
        if need and need <= have and len(need) > best_n:
            best, best_n = title, len(need)
    return best


def link_project(markdown: str, items: list[tuple[str, dict]]) -> str:
    """Дело без project получает ссылку на подходящий открытый проект. Остальные заметки не трогаем."""
    if not markdown.startswith("---"):
        return markdown
    note = parse_note(markdown, 1)
    if note.type != "task" or note.get("project"):
        return markdown
    projects = [str(f.get("title", "")) for _, f in items
                if f.get("type") == "project" and str(f.get("status", "")).lower() not in CLOSED_PROJECT]
    title = match_project(f"{note.title} {note.get('summary')}", [p for p in projects if p])
    link = wikilink(title) if title else None
    if not link:
        return markdown
    lines = markdown.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return markdown
    lines.insert(end, f'project: "{link}"')
    return "\n".join(lines)


# ---------- кнопки ----------

def person_buttons(row_key: str) -> list:
    key = row_key[:12]
    return [[("✅ Связались сегодня", f"pc:{key}"), ("⏰ Напомнить через неделю", f"pm:{key}")],
            [("➕ Добавить факт", f"pf:{key}"), ("🔕 Реже напоминать", f"pq:{key}")]]


def radar_buttons(items: list[tuple[str, dict]], names: list[str], limit: int = 5) -> list:
    """Под радаром: одним нажатием «связался» и «не напоминать» для самых просроченных."""
    rows = []
    for name in names[:limit]:
        found = find_card(items, name)
        if found:
            key = found[0][:12]
            rows.append([(f"✅ {name[:20]}", f"pc:{key}"), ("🔕", f"pq:{key}")])
    return rows


# ---------- события: «как прошло?» ----------

FOLLOWUP_AFTER = timedelta(minutes=30)
FOLLOWUP_WINDOW = timedelta(hours=24)
EVENT_DONE = ("состоялось", "отменено")


def _event_end(front: dict) -> datetime | None:
    """Когда событие закончилось: event_end, иначе начало + 1 час; без времени считаем концом 20:00."""
    end = parse_datetime(front.get("event_end"))
    start = parse_datetime(front.get("event_at"))
    raw = end or start
    if not raw:
        return None
    if len(raw) <= 10:
        return datetime.fromisoformat(raw + "T20:00")
    stamp = datetime.fromisoformat(raw)
    return stamp if end else stamp + timedelta(hours=1)


def due_followups(items: list[tuple[str, dict]], db, now: datetime) -> list[tuple[str, str, str]]:
    """(ключ строки, текст, ключ служебной отметки) для прошедших событий с людьми, о которых ещё не спрашивали."""
    out = []
    for row_key, front in items:
        if front.get("type") != "event" or str(front.get("status", "")).lower() in EVENT_DONE:
            continue
        people = [p for p in as_list(front.get("people")) if not owner_module.is_owner(p)]
        if not people:
            continue
        end = _event_end(front)
        if not end:
            continue
        end = end.replace(tzinfo=now.tzinfo)
        flag = f"evq_{row_key[:12]}"
        if db.get_setting(flag, ""):
            continue
        if end + FOLLOWUP_AFTER <= now < end + FOLLOWUP_WINDOW:
            title = str(front.get("title", ""))
            out.append((row_key, f"🗓 Как прошло: {title}\nС кем: {', '.join(people[:4])}", flag))
    return out


def followup_buttons(row_key: str) -> list:
    key = row_key[:12]
    return [[("✅ Прошло, всё ок", f"eo:{key}"), ("📝 Записать итог", f"en:{key}")],
            [("❌ Не состоялось", f"ex:{key}"), ("⏭ Не нужно", f"es:{key}")]]


def event_people(front: dict) -> list[str]:
    return [p for p in as_list(front.get("people")) if not owner_module.is_owner(p)]


def event_done(db, row_key: str, front: dict, now: datetime) -> list[str]:
    """Событие состоялось: отмечаем людей как «связались» в день события."""
    raw = parse_date(front.get("event_at"))
    day = min(date.fromisoformat(raw), now.date()) if raw else now.date()
    items = db.snapshot_items()
    touched = []
    for name in event_people(front):
        found = find_card(items, name)
        if found and mark_contact(db, found[0], found[1], day, now):
            touched.append(str(found[1].get("title", name)))
    return touched
