"""Недельный обзор и брифинг перед встречей. Всё считает код по свойствам заметок, без ИИ."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from .ask import build_digest, collect_items, fmt_date
from .schema import TYPES
from .textutil import one_line, parse_date
from .vault import VaultNote, as_list, strip_original

STALE_CONTACT_DAYS = 30
DOC_AHEAD_DAYS = 60


def _created(note: VaultNote) -> date | None:
    raw = parse_date(note.created)
    return date.fromisoformat(raw) if raw else None


def build_week_review(notes: list[VaultNote], now: datetime, ada_text: str = "") -> str:
    today = now.date()
    week_ago = today - timedelta(days=7)
    out = [f"Обзор недели на {fmt_date(today)}"]

    fresh = [n for n in notes if (_created(n) or date.min) > week_ago]
    if fresh:
        counts = Counter(n.type for n in fresh)
        parts = [f"{TYPES[t].name.lower() if t in TYPES else t} — {c}" for t, c in counts.most_common(6)]
        out += ["", f"Записано за неделю: {len(fresh)} ({', '.join(parts)})."]
    else:
        out += ["", "За неделю новых записей нет."]

    items = collect_items(notes, today)
    overdue = [i for i in items if i.day < today]
    if overdue:
        out += ["", "Просрочено:"] + [
            f"• {i.title} [{i.label}], на {(today - i.day).days} дн." for i in overdue[:10]]
    nxt = [i for i in items if today <= i.day <= today + timedelta(days=7)]
    if nxt:
        out += ["", "Впереди на неделе:"] + [f"• {fmt_date(i.day)}: {i.title} [{i.label}]" for i in nxt[:12]]

    waiting = [n for n in notes if n.type == "task" and str(n.get("status")) == "ждёт"]
    if waiting:
        out += ["", "Жду ответа:"] + [
            f"• {n.title}" + (f" ({', '.join(n.people[:3])})" if n.people else "") for n in waiting[:8]]

    stale = []
    for n in notes:
        if n.type != "person" or str(n.get("status")) == "неактуален":
            continue
        raw = parse_date(n.get("last_contact"))
        if raw and (today - date.fromisoformat(raw)).days > STALE_CONTACT_DAYS:
            stale.append((raw, n.title, str(n.get("relation"))))
    if stale:
        stale.sort()
        out += ["", f"Давно не общались (больше {STALE_CONTACT_DAYS} дней):"] + [
            f"• {t}" + (f" ({r})" if r else "") + f" — последний контакт {fmt_date(date.fromisoformat(d))}"
            for d, t, r in stale[:8]]

    decisions = [n for n in notes if n.type == "decision" and str(n.get("status")) in ("пересмотреть",)]
    if decisions:
        out += ["", "Решения, которые обещал пересмотреть:"] + [f"• {n.title}" for n in decisions[:6]]

    docs = []
    for n in notes:
        if n.type == "document" and str(n.get("status")) not in ("расторгнут", "истёк"):
            raw = parse_date(n.get("valid_until"))
            if raw and today <= date.fromisoformat(raw) <= today + timedelta(days=DOC_AHEAD_DAYS):
                docs.append((raw, n.title))
    if docs:
        docs.sort()
        out += ["", f"Документы с истекающим сроком (ближайшие {DOC_AHEAD_DAYS} дней):"] + [
            f"• {t} — до {fmt_date(date.fromisoformat(d))}" for d, t in docs[:6]]

    backlog = Counter(n.type for n in notes if str(n.get("status")) == "хочу" and n.type in ("media", "place", "product"))
    if backlog:
        out += ["", "Список «хочу»: " + ", ".join(
            f"{TYPES[t].name.lower()} — {c}" for t, c in backlog.most_common())]
    inbox = [n for n in notes if str(n.get("stage")) == "inbox"]
    old_bookmarks = [n for n in notes if n.type == "bookmark" and str(n.get("status")) != "разобрано"
                     and (_created(n) or today) <= today - timedelta(days=14)]
    if inbox or old_bookmarks:
        out += ["", f"Разобрать: во входящих {len(inbox)}, неразобранных закладок старше 2 недель {len(old_bookmarks)}."]
    if ada_text:
        out += ["", ada_text]
    if len(out) <= 3 and not fresh:
        out += ["", "Срочного нет."]
    return "\n".join(out)


def build_brief(notes: list[VaultNote], person: str, now: datetime, ada_text: str = "") -> str:
    """Что нужно знать перед встречей или звонком с человеком."""
    today = now.date()
    key = person.strip().lower()
    if not key:
        return "Назовите, с кем встреча."
    card = next((n for n in notes if n.type == "person" and (
        n.title.lower() == key or str(n.get("name")).lower() == key
        or key in [a.lower() for a in as_list(n.get("aliases"))])), None)
    if card is None:
        card = next((n for n in notes if n.type == "person" and key in n.title.lower()), None)
    name = card.title if card else person.strip()
    low = name.lower()
    related = [n for n in notes if n is not card and any(low == p.lower() for p in n.people)]
    if not card and not related:
        return f"Про «{person.strip()}» в заметках ничего нет."

    out = [f"Брифинг: {name}"]
    if card:
        facts = []
        for label, field in (("кто", "relation"), ("компания", "company"), ("роль", "role"), ("город", "city"),
                             ("телефон", "phones"), ("почта", "email"), ("познакомились", "how_met"),
                             ("интересы", "interests"), ("предпочтения", "preferences"), ("семья", "family")):
            value = card.get(field)
            shown = ", ".join(as_list(value)) if isinstance(value, list) else str(value).strip()
            if shown:
                facts.append(f"{label}: {one_line(shown, 80)}")
        if facts:
            out += ["", "О человеке:"] + [f"• {f}" for f in facts]
        raw = parse_date(card.get("last_contact"))
        if raw:
            out.append(f"• последний контакт: {fmt_date(date.fromisoformat(raw))} ({(today - date.fromisoformat(raw)).days} дн. назад)")
        bday = str(card.get("birthday")).strip()
        if bday:
            out.append(f"• день рождения: {bday}")
        if card.get("contact_goal"):
            out.append(f"• зачем общаюсь: {one_line(str(card.get('contact_goal')), 100)}")
        gifts = as_list(card.get("gift_ideas"))
        if gifts:
            out.append("• идеи подарков: " + ", ".join(gifts[:5]))

    contacts = sorted((n for n in related if n.type == "interaction"), key=lambda n: n.created, reverse=True)
    if contacts:
        out += ["", "Последние контакты:"]
        for n in contacts[:4]:
            tail = ""
            if n.get("agreements"):
                tail = f" — договорённости: {one_line(str(n.get('agreements')), 100)}"
            elif n.get("topic"):
                tail = f" — {one_line(str(n.get('topic')), 80)}"
            out.append(f"• {n.created[:10]}: {n.title}{tail}")

    tasks = [n for n in related if n.type == "task" and str(n.get("status")) not in ("сделано", "отменено", "отложено")]
    if tasks:
        out += ["", "Открытые дела с этим человеком:"]
        for n in tasks[:8]:
            due = parse_date(n.get("due"))
            out.append(f"• {n.title}" + (f" (до {fmt_date(date.fromisoformat(due))})" if due else "")
                       + (" — жду ответа" if str(n.get("status")) == "ждёт" else ""))

    others = [n for n in related if n.type not in ("interaction", "task", "person")]
    if others:
        out += ["", "Ещё в заметках:"] + [
            f"• {n.title} [{TYPES[n.type].name.lower() if n.type in TYPES else n.type}]" for n in others[:6]]
    money = [n for n in related if n.type in ("document", "case")]
    if money and ada_text:
        out += ["", "Есть документы или дела — сверьтесь с финансами:", ada_text.splitlines()[0]]
    return "\n".join(out)
