"""Недельный обзор и брифинг перед встречей. Всё считает код по свойствам заметок, без ИИ."""
from __future__ import annotations

import random
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

    days = {str(n.created)[:10] for n in notes if n.type == "journal" and (_created(n) or date.min) > week_ago}
    if notes:
        out += ["", f"Дневник: записи за {len(days)} из 7 последних дней."]

    backlog = Counter(n.type for n in notes if str(n.get("status")) == "хочу" and n.type in ("media", "place", "product"))
    if backlog:
        out += ["", "Список «хочу»: " + ", ".join(
            f"{TYPES[t].name.lower()} — {c}" for t, c in backlog.most_common())]
    inbox = [n for n in notes if str(n.get("stage")) == "inbox"]
    stale_marks = old_bookmarks(notes, today)
    if inbox or stale_marks:
        out += ["", f"Разобрать: во входящих {len(inbox)}, неразобранных закладок старше 2 недель {len(stale_marks)}."]
    marks_text = format_old_bookmarks(notes, today)
    if marks_text:
        out += ["", marks_text]
    if notes:
        score, issues = build_health(notes, today)
        if issues:
            out += ["", f"Здоровье базы: {score}/100. Подробности: /health"]
    back = format_resurface(pick_resurface(notes, today), today)
    if back:
        out += ["", back]
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
        extra = []
        if str(card.get("orbit")).strip() in ORBITS:
            extra.append(f"круг: {ORBITS[str(card.get('orbit')).strip()]}")
        if card.get("partner"):
            extra.append(f"партнёр: {one_line(str(card.get('partner')), 60)}")
        if as_list(card.get("children")):
            extra.append("дети: " + ", ".join(as_list(card.get("children"))[:5]))
        out += [f"• {e}" for e in extra]
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

    tasks = [n for n in related if n.type == "task" and str(n.get("status")) not in CLOSED]
    if tasks:
        out += ["", "Открытые дела с этим человеком:"]
        for n in tasks[:8]:
            due = parse_date(n.get("due"))
            mark = {"я": " — я обещал", "мне": " — мне обещали"}.get(str(n.get("promise")), "")
            out.append(f"• {n.title}" + (f" (до {fmt_date(date.fromisoformat(due))})" if due else "")
                       + mark + (" — жду ответа" if str(n.get("status")) == "ждёт" else ""))

    others = [n for n in related if n.type not in ("interaction", "task", "person")]
    if others:
        out += ["", "Ещё в заметках:"] + [
            f"• {n.title} [{TYPES[n.type].name.lower() if n.type in TYPES else n.type}]" for n in others[:6]]
    money = [n for n in related if n.type in ("document", "case")]
    if money and ada_text:
        out += ["", "Есть документы или дела — сверьтесь с финансами:", ada_text.splitlines()[0]]
    return "\n".join(out)


# ---------- Круги общения, радар и обещания ----------

CLOSED = ("сделано", "отменено", "отложено")
ORBITS = {"0": "семья", "1": "ближний", "2": "соратники", "3": "эксперты", "4": "сеть"}
ORBIT_DAYS = {"0": 3, "1": 14, "2": 30, "3": 90, "4": 180}


def build_radar(notes: list[VaultNote], today: date, limit: int = 10) -> str:
    """Люди из кругов, с кем контакт просрочен. Срок: cadence_days в карточке или по кругу (0: 3 дня … 4: 180)."""
    last: dict[str, date] = {}
    for n in notes:
        if n.type != "interaction":
            continue
        day = _created(n)
        for name in n.people:
            if day and (name.lower() not in last or day > last[name.lower()]):
                last[name.lower()] = day
    rows = []
    for card in (n for n in notes if n.type == "person"):
        orbit = str(card.get("orbit")).strip()
        try:
            cadence = int(float(str(card.get("cadence_days")))) if str(card.get("cadence_days")).strip() else ORBIT_DAYS.get(orbit)
        except ValueError:
            cadence = ORBIT_DAYS.get(orbit)
        if not cadence:
            continue
        seen = [d for d in (parse_date(card.get("last_contact")),) if d]
        days = [date.fromisoformat(d) for d in seen]
        for key in {card.title.lower(), str(card.get("name")).lower(), *[a.lower() for a in as_list(card.get("aliases"))]}:
            if key in last:
                days.append(last[key])
        latest = max(days) if days else None
        age = (today - latest).days if latest else None
        if age is None or age > cadence:
            rows.append(((age if age is not None else 10_000) - cadence, card.title, orbit, cadence, age))
    if not rows:
        return "Радар чист: все, кому ты задал круг или срок, на связи вовремя. Круг задаётся в карточке полем orbit (0–4) или cadence_days."
    rows.sort(reverse=True)
    out = ["Давно не общались:"]
    for _, name, orbit, cadence, age in rows[:limit]:
        when = "контактов не записано" if age is None else f"{age} дн. назад"
        circle = f", круг «{ORBITS[orbit]}»" if orbit in ORBITS else ""
        out.append(f"• {name} — {when} (нужно раз в {cadence} дн.{circle})")
    return "\n".join(out)


def build_promises(notes: list[VaultNote], today: date) -> str:
    """Открытые обещания: что я обещал и что обещали мне (поле promise у дел)."""
    groups: dict[str, list[VaultNote]] = {"я": [], "мне": []}
    for n in notes:
        if n.type == "task" and str(n.get("status")) not in CLOSED and str(n.get("promise")) in groups:
            groups[str(n.get("promise"))].append(n)
    if not groups["я"] and not groups["мне"]:
        return "Открытых обещаний нет. Скажи боту, например: «пообещал Арману скинуть контакт во вторник» — он отметит это обещание."

    def line(n: VaultNote) -> str:
        due = parse_date(n.get("due"))
        who = f" ({', '.join(n.people[:2])})" if n.people else ""
        tail = ""
        if due:
            day = date.fromisoformat(due)
            tail = f" — до {fmt_date(day)}" + (" ⚠ просрочено" if day < today else "")
        return f"• {n.title}{who}{tail}"

    def key(n: VaultNote):
        return parse_date(n.get("due")) or "9999"

    out = []
    if groups["я"]:
        out += ["🔴 Я обещал:"] + [line(n) for n in sorted(groups["я"], key=key)[:15]]
    if groups["мне"]:
        out += ([""] if out else []) + ["🟢 Мне обещали:"] + [line(n) for n in sorted(groups["мне"], key=key)[:15]]
    return "\n".join(out)


# ---------- «Из прошлого»: случайные старые мысли и хотелки (идея из систем с затуханием памяти) ----------

RESURFACE_TYPES = frozenset({"idea", "learning", "quote", "decision", "goal"})
RESURFACE_CLOSED = frozenset({"реализована", "отброшена", "достигнута", "пересмотрено", "отложена"})


def pick_resurface(notes: list[VaultNote], today: date, count: int = 1, min_age: int = 45) -> list[VaultNote]:
    pool = []
    for n in notes:
        created = _created(n)
        if not created or (today - created).days < min_age:
            continue
        status = str(n.get("status"))
        if (n.type in RESURFACE_TYPES and status not in RESURFACE_CLOSED) or (
                n.type in ("media", "place", "product") and status == "хочу"):
            pool.append(n)
    if not pool:
        return []
    return random.Random(today.toordinal()).sample(pool, min(count, len(pool)))


def format_resurface(items: list[VaultNote], today: date) -> str:
    if not items:
        return ""
    rows = ["🕰 Из прошлого:"]
    for n in items:
        created = _created(n)
        age = f", записано {(today - created).days} дн. назад" if created else ""
        kind = TYPES[n.type].name.lower() if n.type in TYPES else n.type
        summary = one_line(str(n.get("summary")), 100)
        rows.append(f"• {n.title} ({kind}{age})" + (f" — {summary}" if summary and summary != n.title else ""))
    return "\n".join(rows)


# ---------- закладки без разбора: детерминированный отчёт, без ИИ ----------

BOOKMARK_STALE_DAYS = 14


def old_bookmarks(notes: list[VaultNote], today: date, days: int = BOOKMARK_STALE_DAYS) -> list[tuple[int, VaultNote]]:
    """Закладки не в статусе «разобрано» старше `days` дней: (возраст в днях, заметка), самые старые первыми."""
    found = []
    for n in notes:
        created = _created(n)
        if n.type == "bookmark" and str(n.get("status")) != "разобрано" and created and (today - created).days >= days:
            found.append(((today - created).days, n))
    found.sort(key=lambda item: (-item[0], item[1].title))
    return found


def format_old_bookmarks(notes: list[VaultNote], today: date, limit: int = 5) -> str:
    found = old_bookmarks(notes, today)
    if not found:
        return ""
    rows = [f"Закладки без разбора старше {BOOKMARK_STALE_DAYS} дней: {len(found)}. Самые старые:"]
    for age, n in found[:limit]:
        rows.append(f"• «{one_line(n.title, 70)}» — {age} дн.")
    rows.append("Разбери вручную: открой закладку, поменяй тип на нужный или поставь статус «разобрано».")
    return "\n".join(rows)


# ---------- проверка здоровья базы (дубли, неразобранное, зависшие дела) ----------

def _norm(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def build_health(notes: list[VaultNote], today: date) -> tuple[int, list[str]]:
    """Возвращает (оценка 0–100, список замечаний). Только читает, ничего не меняет."""
    issues: list[str] = []
    penalty = 0
    seen: dict[tuple[str, str], list[str]] = {}
    urls: dict[str, list[str]] = {}
    for n in notes:
        seen.setdefault((n.type, _norm(n.title)), []).append(n.title)
        url = str(n.get("source_url")).strip().lower().rstrip("/")
        if url:
            urls.setdefault(url, []).append(n.title)
    dupes = [titles[0] for titles in seen.values() if len(titles) > 1 and _norm(titles[0])]
    dupes += [titles[0] for titles in urls.values() if len(titles) > 1 and titles[0] not in dupes]
    if dupes:
        penalty += min(30, 5 * len(dupes))
        issues.append(f"Похожие записи (возможные дубли): {len(dupes)}. Например: " + "; ".join(f"«{d}»" for d in dupes[:3]))
    weak = [n for n in notes if "unprocessed" in [t.lower() for t in as_list(n.get("tags"))] or "проверить" in [t.lower() for t in as_list(n.get("tags"))]]
    if weak:
        penalty += min(20, 2 * len(weak))
        issues.append(f"Нужна проверка (теги unprocessed/проверить): {len(weak)}. Например: " + "; ".join(f"«{n.title}»" for n in weak[:3]))
    stale = [n for n in notes if n.type == "task" and str(n.get("status")) in ("открыто", "") and not parse_date(n.get("due"))
             and (_created(n) or today) <= today - timedelta(days=30)]
    if stale:
        penalty += min(20, 2 * len(stale))
        issues.append(f"Дела без срока старше месяца: {len(stale)}. Закрой или назначь дату: " + "; ".join(f"«{n.title}»" for n in stale[:3]))
    old_inbox = [n for n in notes if str(n.get("stage")) == "inbox" and (_created(n) or today) <= today - timedelta(days=14)]
    if old_inbox:
        penalty += min(20, len(old_inbox))
        issues.append(f"Во входящих дольше 2 недель: {len(old_inbox)}.")
    marks = old_bookmarks(notes, today)
    if marks:
        penalty += min(10, len(marks))
        issues.append(f"Закладки без разбора старше {BOOKMARK_STALE_DAYS} дней: {len(marks)}. Самые старые: "
                      + "; ".join(f"«{n.title}» ({age} дн.)" for age, n in marks[:3]))
    bare = [n for n in notes if n.type == "person" and not (n.get("relation") or n.get("relation_group") or n.get("company"))]
    if bare:
        penalty += min(10, len(bare))
        issues.append(f"Карточки людей без роли: {len(bare)}. Например: " + "; ".join(f"«{n.title}»" for n in bare[:3]))
    return max(0, 100 - penalty), issues


def format_health(notes: list[VaultNote], today: date) -> str:
    score, issues = build_health(notes, today)
    head = f"Здоровье базы: {score}/100 (заметок: {len(notes)})"
    return head + ("\n" + "\n".join(f"• {i}" for i in issues) if issues else "\nЗамечаний нет.")


# ---------- слияние дублей: только предпросмотр, ничего не удаляется ----------

def find_duplicates(notes: list[VaultNote]) -> list[list[VaultNote]]:
    """Группы похожих записей: тот же тип и название, та же ссылка, или человек, чьё имя входит в имя другого."""
    groups: list[list[VaultNote]] = []
    placed: set[int] = set()

    def add(members: list[VaultNote]) -> None:
        fresh = [n for n in members if n.index not in placed]
        if len(members) > 1 and len(fresh) > 1:
            groups.append(sorted(fresh, key=lambda n: (n.created or "", n.index)))
            placed.update(n.index for n in fresh)

    by_title: dict[tuple[str, str], list[VaultNote]] = {}
    by_url: dict[str, list[VaultNote]] = {}
    for n in notes:
        key = _norm(n.title)
        if key:
            by_title.setdefault((n.type, key), []).append(n)
        url = str(n.get("source_url")).strip().lower().rstrip("/")
        if url:
            by_url.setdefault(url, []).append(n)
    for members in by_title.values():
        add(members)
    for members in by_url.values():
        add(members)
    people = [n for n in notes if n.type == "person" and n.index not in placed and _norm(n.title)]
    for a in people:
        if a.index in placed:
            continue
        a_tokens = set(_norm(a.title).split())
        twins = [a]
        for b in people:
            if b is a or b.index in placed:
                continue
            b_tokens = set(_norm(b.title).split())
            if len(a_tokens) != len(b_tokens) and (a_tokens <= b_tokens or b_tokens <= a_tokens):
                twins.append(b)
        add(twins)
    return groups


def _filled(n: VaultNote) -> int:
    return sum(1 for value in n.front.values() if value not in ("", None, [], {}))


_SKIP_DIFF = frozenset({"schema", "created", "capture_id", "confidence", "source", "stage", "ai_filled", "title", "summary"})


def _diff_fields(keep: VaultNote, other: VaultNote) -> list[str]:
    """Свойства, которые есть у `other`, но пусты или другие в главной: их стоит перенести руками."""
    out = []
    for key, value in other.front.items():
        if key in _SKIP_DIFF or value in ("", None, [], {}):
            continue
        if keep.front.get(key) in ("", None, [], {}) or keep.front.get(key) != value:
            out.append(key)
    return out


def _main_of(group: list[VaultNote]) -> VaultNote:
    return max(group, key=lambda n: (_filled(n), -(n.index)))


def format_merge(notes: list[VaultNote]) -> str:
    groups = find_duplicates(notes)
    if not groups:
        return "Дублей не нашёл."
    rows = [f"Похожих групп: {len(groups)}. Ничего не удаляю и не сливаю сам. По каждой группе: что оставить главной, "
            "что объединить с ней. Файл-подсказку для ручного слияния подготовит команда /merge НОМЕР."]
    for i, group in enumerate(groups[:8], 1):
        keep = _main_of(group)
        kind = TYPES[group[0].type].name.lower() if group[0].type in TYPES else group[0].type
        rows += ["", f"{i}. {kind}:"]
        for n in group:
            if n is keep:
                rows.append(f"• ✅ оставить главной: «{n.title}» от {(n.created or '?')[:10]}, заполнено полей: {_filled(n)}")
            else:
                more = _diff_fields(keep, n)
                extra = f", перенести: {', '.join(more[:6])}" if more else ", переносить нечего"
                rows.append(f"• ➕ объединить с главной: «{n.title}» от {(n.created or '?')[:10]}, "
                            f"заполнено полей: {_filled(n)}{extra}")
        rows.append(f"  Подсказка: /merge {i}")
    if len(groups) > 8:
        rows += ["", f"…и ещё групп: {len(groups) - 8}."]
    return "\n".join(rows)


def build_merge_hint(notes: list[VaultNote], number: int, now: datetime) -> tuple[str, dict | None]:
    """Файл-подсказка для ручного слияния группы №number. Возвращает (ответ боту, файл для очереди или None)."""
    from .frontmatter import Raw, dump
    from .textutil import file_suffix, slugify

    groups = find_duplicates(notes)
    if not groups:
        return "Дублей не нашёл, подсказка не нужна.", None
    if not 1 <= number <= min(len(groups), 8):
        return f"Нет такой группы. Выбери номер от 1 до {min(len(groups), 8)} (список: /merge).", None
    group = groups[number - 1]
    keep = _main_of(group)
    others = [n for n in group if n is not keep]
    head = dump([
        ("schema", 1), ("title", f"Слияние дублей: {one_line(keep.title, 60)}"),
        ("summary", f"Подсказка для ручного слияния: {len(group)} похожих записей"),
        ("created", Raw(now.strftime("%Y-%m-%dT%H:%M"))), ("type", "note"), ("stage", "inbox"), ("area", "разное"),
        ("tags", ["merge-hint"]), ("source", "telegram"),
    ])
    body = ["Подсказка составлена по свойствам заметок (тексты на сервере не хранятся). Ничего не удалено и не изменено.",
            "", f"## Оставить главной", f"- [[{keep.title}]] от {(keep.created or '?')[:10]}, заполнено полей: {_filled(keep)}",
            "", "## Объединить с главной"]
    for n in others:
        more = _diff_fields(keep, n)
        body.append(f"- [[{n.title}]] от {(n.created or '?')[:10]}, заполнено полей: {_filled(n)}"
                    + (f". Перенести свойства: {', '.join(more)}" if more else ". Переносить в свойствах нечего"))
    body += ["", "## Что сделать руками", "1. Открой главную заметку и сравни с остальными: перенеси недостающие факты из текста.",
             "2. Проверь ссылки `[[...]]` на дубли в других заметках и поменяй их на главную.",
             "3. Когда всё перенесено, сам удали или архивируй лишние заметки. Эту подсказку тоже можно удалить.", ""]
    filename = f"{now:%Y-%m-%d}-{slugify('слияние ' + keep.title)}-{file_suffix(f'merge-{keep.title}-{now:%Y%m%d}')}.md"
    file = {"filename": filename, "folder": "0-Inbox", "type": "note", "title": f"Слияние дублей: {keep.title}",
            "markdown": head + "\n\n" + "\n".join(body)}
    return f"Подготовил файл-подсказку «{file['title']}». Появится в 0-Inbox, когда ноутбук или телефон заберёт очередь.", file
