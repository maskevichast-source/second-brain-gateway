"""Перенос списка «Дом» из «Ады» во «Второй Мозг»: уход за техникой, растениями и фильтрами по расписанию.

Каждая задача становится заметкой типа «дом» со сроком и периодом (сводка двигает повторяющееся дело на
следующий раз после «сделано»), а общая заметка собирает всё по предметам. Собирает код, ИИ не нужен.
Дат покупки и гарантий в «Аде» нет: их не придумываем, а оставляем место, чтобы дописать.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime

from . import ada as ada_module
from .config import Settings
from .db import Database
from .frontmatter import Raw, dump
from .snapshot import rows_from_markdown
from .textutil import file_suffix, parse_date, slugify

PLANTS = ("кактус", "замиокулькас", "фикус", "орхиде", "цвет", "растен", "монстер", "пальм")
HUB_TITLE = "Дом: техника, растения и уход"


def classify(item: str) -> str:
    low = item.lower()
    if any(word in low for word in PLANTS):
        return "растение"
    if re.search(r"[A-Za-z]{3,}", item):          # бренд или модель латиницей: это техника
        return "техника"
    return "уход"


def schedule_text(days) -> str:
    try:
        n = int(float(days))
    except (TypeError, ValueError):
        return ""
    return f"каждые {n} дней" if n > 1 else "ежедневно"


def _date(value) -> str:
    return parse_date(value) or ""


def care_file(rec: dict, now: datetime) -> dict:
    item, task = str(rec.get("item", "")).strip(), str(rec.get("task", "")).strip() or "обслужить"
    care_id = str(rec.get("care_id", ""))
    title = f"{item} — {task}"
    kind = classify(item)
    due, last = _date(rec.get("next_due")), _date(rec.get("last_done"))
    every = schedule_text(rec.get("interval_days"))
    items = [
        ("schema", 1), ("title", title), ("summary", f"{task}: {every}" if every else task),
        ("created", Raw(f"{now:%Y-%m-%dT%H:%M}")), ("type", "home"), ("kind", kind), ("status", "открыто"),
        ("item", item), ("schedule", every), ("next_due", Raw(due) if due else None),
        ("area", "дом"), ("tags", ["дом", "уход"]), ("source", "ada"), ("capture_id", f"ada-home-{care_id}"), ("confidence", 1),
    ]
    body = [f"**Что делать:** {task}", f"**Предмет:** {item}", f"**Как часто:** {every or 'не указано'}",
            f"**Последний раз:** {last or 'не отмечено'}", f"**Следующий срок:** {due or 'не указан'}"]
    if str(rec.get("note", "")).strip():
        body.append(f"**Заметка:** {str(rec['note']).strip()}")
    body += ["", "Перенесено из списка «Дом» бота «Ада». Когда сделано, отметь в сводке бота («сделано 2» или кнопкой)."]
    markdown = dump(items) + "\n\n" + "\n".join(body) + "\n"
    return {"filename": f"{now:%Y-%m-%d}-dom-{slugify(title, 40)}-{file_suffix(care_id)}.md", "folder": "0-Inbox",
            "type": "home", "title": title, "markdown": markdown}


def warranty_file(rec: dict, now: datetime) -> dict:
    item, care_id = str(rec.get("item", "")).strip(), str(rec.get("care_id", ""))
    until = _date(rec.get("next_due"))
    title = f"Гарантия — {item}"
    items = [
        ("schema", 1), ("title", title), ("summary", f"Гарантия на «{item}» до {until}" if until else f"Гарантия на «{item}»"),
        ("created", Raw(f"{now:%Y-%m-%dT%H:%M}")), ("type", "home"), ("kind", classify(item)), ("status", "открыто"),
        ("item", item), ("warranty_until", Raw(until) if until else None),
        ("area", "дом"), ("tags", ["дом", "гарантия"]), ("source", "ada"), ("capture_id", f"ada-home-{care_id}"), ("confidence", 1),
    ]
    markdown = dump(items) + f"\n\n**Предмет:** {item}\n**Гарантия до:** {until or 'не указано'}\n"
    return {"filename": f"{now:%Y-%m-%d}-dom-{slugify(title, 40)}-{file_suffix(care_id)}.md", "folder": "0-Inbox",
            "type": "home", "title": title, "markdown": markdown}


def hub_file(records: list[dict], now: datetime) -> dict:
    """Одна общая заметка: что есть дома и как за этим ухаживать. Даты покупки и гарантий оставлены для ручного ввода."""
    by_item: "OrderedDict[str, list[dict]]" = OrderedDict()
    for rec in records:
        by_item.setdefault(str(rec.get("item", "")).strip(), []).append(rec)
    sections: dict[str, list[str]] = {"техника": [], "растение": [], "уход": []}
    for item, recs in by_item.items():
        lines = [f"### {item}"]
        if classify(item) == "техника":
            lines.append("Куплено: _допиши дату_. Гарантия до: _допиши_.")
        for r in recs:
            if r.get("kind") == "warranty":
                lines.append(f"- гарантия до {_date(r.get('next_due')) or '?'}")
                continue
            every = schedule_text(r.get("interval_days"))
            lines.append(f"- {str(r.get('task', '')).strip()}: {every}, следующий раз {_date(r.get('next_due')) or '?'}")
        sections[classify(item)].append("\n".join(lines))
    body = ["Перенесено из «Ады». Даты покупки и сроки гарантии в «Аде» не хранились, их нужно дописать самому.", ""]
    for kind, title in (("техника", "Техника"), ("растение", "Растения"), ("уход", "Фильтры и прочий уход")):
        if sections[kind]:
            body += [f"## {title}", "", *sections[kind], ""]
    items = [
        ("schema", 1), ("title", HUB_TITLE), ("summary", f"Что есть дома и как за этим ухаживать ({len(by_item)} предметов)"),
        ("created", Raw(f"{now:%Y-%m-%dT%H:%M}")), ("type", "home"), ("kind", "другое"), ("status", "открыто"),
        ("area", "дом"), ("tags", ["дом", "техника", "уход"]), ("source", "ada"), ("capture_id", "ada-home-hub"), ("confidence", 1),
    ]
    return {"filename": f"{now:%Y-%m-%d}-dom-obzor-{file_suffix('ada-home-hub')}.md", "folder": "0-Inbox", "type": "home",
            "title": HUB_TITLE, "markdown": dump(items) + "\n\n" + "\n".join(body).rstrip() + "\n"}


async def import_home(settings: Settings, db: Database, now: datetime, force: bool = False) -> str:
    if not ada_module.configured(settings):
        return "«Ада» не подключена (ADA_SUMMARY_URL и ADA_SUMMARY_TOKEN на сервере)."
    records = await ada_module.fetch_homecare(settings)
    if records is None:
        return ("Не достучался до «Ады»: список «Дом» недоступен. Если «Ада» только что обновлялась, подожди минуту. "
                "Если не пройдёт, проверь, что в ней задан SECRETARY_TOKEN.")
    records = [r for r in records if str(r.get("care_id", "")).strip() and str(r.get("item", "")).strip()]
    if not records:
        return "В списке «Дом» у «Ады» пусто: переносить нечего."
    try:
        done = set(json.loads(db.get_setting("ada_home_imported", "[]")))
    except ValueError:
        done = set()
    fresh = records if force else [r for r in records if str(r["care_id"]) not in done]
    if not fresh:
        return "Всё из списка «Дом» уже перенесено раньше. Перенести ещё раз целиком: /import дом заново."
    files = [hub_file(records, now)] if (force or not done) else []
    for rec in fresh:
        files.append(warranty_file(rec, now) if rec.get("kind") == "warranty" else care_file(rec, now))
    added = 0
    for f in files:
        added += db.enqueue(f"ada-home-{file_suffix(f['filename'])}", [f])
        db.snapshot_put(rows_from_markdown(f["markdown"]))
    db.set_setting("ada_home_imported", json.dumps(sorted(done | {str(r["care_id"]) for r in fresh})))
    items = len({str(r["item"]).strip() for r in fresh})
    return (f"Перенёс из «Ады»: задач по уходу и гарантий {len(fresh)} (предметов: {items}) и одну общую заметку «{HUB_TITLE}». "
            "В хранилище они появятся, когда телефон или ноутбук заберёт очередь, а в сводке уже видны. "
            "Дат покупки и гарантий в «Аде» нет: допиши их в общей заметке или скажи мне.\n\n"
            "В «Аде» всё осталось как было: она по-прежнему напоминает про это сама, так что напоминания будут приходить "
            "от двух ботов. Когда выберешь один, второй отключим.")
