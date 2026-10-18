"""Дела из сводки: закрыть, отменить, отложить. Работает по срезу на сервере.

Срез обновляется сразу, а в хранилище изменение попадает служебной записью: её применяет ноутбук
(tools/vault_sync.py). Пока ноутбук выключен, сводки уже не показывают закрытое.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from . import ask
from .db import Database
from .snapshot import to_notes
from .textutil import parse_date

CLOSED = frozenset({"сделано", "отменено", "готово", "состоялось", "отказался", "отменена", "завершена"})
DATE_FIELDS = ("due", "remind_at", "next_due", "event_at", "deadline")
LIST_MAX_AGE = timedelta(days=2)


def keyed_notes(db: Database) -> list:
    items = db.snapshot_items()
    return to_notes([{**front, "_key": key[:12]} for key, front in items])


def render_day(db: Database, now: datetime, ada_brief: str = "") -> tuple[str, list]:
    """Сводка с номерами и кнопками. Номера запоминаются: «сделано 2» относится к последнему показанному списку."""
    notes = keyed_notes(db)
    text = ask.build_digest(notes, now, ada_brief=ada_brief, numbered=True)
    acts = ask.digest_actions(notes, now.date())
    db.set_setting("last_list", json.dumps({"at": now.replace(tzinfo=None).isoformat(timespec="minutes"),
                                            "items": [[item.key, item.title] for _, item in acts]}, ensure_ascii=False))
    buttons = [[(f"✅ {n}", f"dn:{item.key}"), (f"🗑 {n}", f"cx:{item.key}"), (f"⏭ {n}", f"sz:{item.key}")]
               for n, item in acts]
    return text, buttons


def _find(db: Database, key: str) -> tuple[str, dict] | None:
    for row_key, front in db.snapshot_items():
        if row_key[:12] == key:
            return row_key, front
    return None


def _with_date(raw: Any, new_day: date) -> str:
    """Меняет дату, сохраняя время («2026-10-17T21:00» -> «2026-10-19T21:00»)."""
    text = str(raw or "")
    return new_day.isoformat() + (text[10:] if len(text) > 10 else "")


def _roll(front: dict, field: str, today: date, steps_from_today: bool) -> date | None:
    raw = parse_date(front.get(field))
    if not raw:
        return None
    current = date.fromisoformat(raw)
    return max(current, today) + timedelta(days=1) if steps_from_today else current


def apply(db: Database, key: str, action: str, now: datetime) -> tuple[str, str]:
    """action: done | cancel | snooze. Возвращает (код, заголовок дела): ok, already, missing, unsupported."""
    found = _find(db, key)
    if not found:
        return "missing", ""
    row_key, front = found
    title, kind = str(front.get("title", "")), str(front.get("type", ""))
    if str(front.get("status", "")).strip().lower() in CLOSED:
        return "already", title
    today = now.date()
    fields_of_type = [f for (t, f) in ask.ACTIONABLE if t == kind]
    if not fields_of_type:
        return "unsupported", title
    base = {"title": title, "created": str(front.get("created", "")), "type": kind}
    period = ask.repeat_days(front.get("repeat") or front.get("schedule")) if kind in ("task", "home") else None

    if action == "snooze" or (action == "done" and period):
        updates = {}
        for field in DATE_FIELDS:
            if not front.get(field) or field not in fields_of_type + ["due", "remind_at"]:
                continue
            raw = parse_date(front.get(field))
            if not raw:
                continue
            current = date.fromisoformat(raw)
            if action == "snooze":
                new_day = max(current, today) + timedelta(days=1)
            else:                                           # повторяющееся: «сделано» двигает на следующий раз
                shown = ask.roll_forward(current, period, today)
                new_day = shown + timedelta(days=period) if shown <= today else shown
            updates[field] = _with_date(front.get(field), new_day)
        if not updates:
            return "unsupported", title
        db.snapshot_put([(row_key, {**front, **updates})])
        db.enqueue_service(f"set-{key}-{now:%Y%m%d%H%M%S}", f"set-{key}.json", {"action": "set", **base, "fields": updates})
        return "ok", title

    status = "отменено" if action == "cancel" else next(v for (t, _), v in ask.ACTIONABLE.items() if t == kind)
    db.snapshot_put([(row_key, {**front, "status": status})])
    db.enqueue_service(f"close-{key}-{now:%Y%m%d%H%M%S}", f"close-{key}.json", {"action": "close", **base, "status": status})
    return "ok", title


TOAST = {"done": "✅ Готово", "cancel": "🗑 Отменено", "snooze": "⏭ Перенёс на завтра"}
_DONE = re.compile(r"^(сделал[аои]?|сделано|готово|выполнил[аои]?|выполнено|закрой|закрыть|отметь(?: сделанным)?|done)[\s:,.\-]+(.+)$", re.I)
_CANCEL = re.compile(r"^(отмени(?:ть)?|отмена|удали(?:ть)?|убери|неактуально|не нужно|не надо)[\s:,.\-]+(.+)$", re.I)
_SNOOZE = re.compile(r"^(отложи(?:ть)?|перенеси(?:ть)?|на завтра)[\s:,.\-]+(.+)$", re.I)


def parse_phrase(text: str) -> tuple[str, str] | None:
    """«сделано 1 и 3», «отмена купить молоко», «отложи 2» -> (действие, остаток фразы)."""
    t = text.strip()
    if len(t) > 90 or t.startswith("/"):
        return None
    for action, pattern in (("done", _DONE), ("cancel", _CANCEL), ("snooze", _SNOOZE)):
        m = pattern.match(t)
        if m:
            return action, m.group(2).strip()
    return None


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")) if len(w) > 2}


def resolve(db: Database, rest: str, now: datetime) -> tuple[list[tuple[str, str]], str]:
    """Какие дела имел в виду человек: по номерам из последнего списка или по названию. -> ([(ключ, название)], ошибка)."""
    if re.fullmatch(r"[\d\s,;и.]+", rest):
        raw = db.get_setting("last_list", "")
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        items = data.get("items") or []
        if not items:
            return [], "Сначала открой список: /tasks"
        try:
            age = now.replace(tzinfo=None) - datetime.fromisoformat(data.get("at", ""))
        except ValueError:
            age = timedelta(0)
        if age > LIST_MAX_AGE:
            return [], "Список давно не обновлялся, номера могли сдвинуться. Открой /tasks и повтори."
        picked, bad = [], []
        for n in dict.fromkeys(int(x) for x in re.findall(r"\d+", rest)):
            if 1 <= n <= len(items):
                picked.append((items[n - 1][0], items[n - 1][1]))
            else:
                bad.append(n)
        if bad:
            return [], f"Нет дела с номером {', '.join(map(str, bad))}. Открой /tasks."
        return picked, ""
    wanted = _stems(rest)
    if not wanted:
        return [], ""
    scored = []
    for row_key, front in db.snapshot_items():
        if front.get("type") not in ("task", "event") or str(front.get("status", "")).lower() in CLOSED | {"отложено"}:
            continue
        have = _stems(str(front.get("title", "")))
        if have and len(wanted & have) / len(wanted) >= 0.6:
            scored.append((len(wanted & have) / len(wanted | have), row_key[:12], str(front.get("title", ""))))
    scored.sort(reverse=True)
    if not scored:
        return [], ""                                  # не похоже на дело: пусть сообщение пойдёт как обычная запись
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        names = "; ".join(t for _, _, t in scored[:4])
        return [], f"Подходит несколько дел: {names}. Уточни или открой /tasks и пользуйся номерами."
    return [(scored[0][1], scored[0][2])], ""
