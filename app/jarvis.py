"""Джарвис: секретарь, который сам замечает важное в жизни семьи по данным «Ады» и пишет вовремя.

Что делает сейчас:
- крупная покупка (из таблицы трат «Ады») -> просит документы: чек, договор, гарантию; кнопки «пришлю», «гарантия до…», «позже», «не нужно»;
- пришла зарплата или премия -> одно короткое сообщение;
- важные новости про сервисы (сбои, утечки, смена ключей) -> см. app/news.py.
Ночью (22:00–08:00) молчит, кроме тревог про безопасность. Каждое событие присылается один раз.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .db import Database

SEEN = "jarvis_seen"
META = "jarvis_meta"
LATER = "jarvis_later"
LAST_RUN = "jarvis_last_run"
RUN_EVERY_MIN = 20
QUIET_FROM, QUIET_TO = 22, 8
MAX_PER_RUN = 2
CAP = 400


def is_quiet(now: datetime) -> bool:
    return now.hour >= QUIET_FROM or now.hour < QUIET_TO


def _load(db: Database, key: str, default):
    try:
        data = json.loads(db.get_setting(key, "") or "")
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


def _save(db: Database, key: str, value) -> None:
    db.set_setting(key, json.dumps(value, ensure_ascii=False))


def short_id(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def money(value: Any) -> str:
    try:
        return f"{round(float(value)):,}".replace(",", " ") + " ₸"
    except (TypeError, ValueError):
        return "?"


def rus_date(day: str) -> str:
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return day
    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return f"{d.day} {months[d.month - 1]}"


# ---------- какие документы просить ----------

_SERVICE = re.compile(r"ремонт|строй|монтаж|клининг|уборк|услуг|мастер|работ|мебельщик|установк|доставк", re.I)
_FURNITURE = re.compile(r"мебел|кровать|диван|шкаф|кухн|стол|стул|матрас|шторы|текстиль", re.I)
_TECH = re.compile(r"техник|электрон|телефон|ноутбук|холодильник|стирал|пылесос|телевизор|планшет|наушник|замок|робот|кондиционер", re.I)


def advice(p: dict) -> tuple[str, str]:
    """(вид, что попросить). Работает по категории, подкатегории, продавцу и комментарию из таблицы трат."""
    blob = " ".join(str(p.get(k, "")) for k in ("category", "subcategory", "merchant", "comment"))
    cat = str(p.get("category", ""))
    if cat.startswith("Электроника") or _TECH.search(blob):
        return "tech", "чек, гарантийный талон (срок гарантии), серийный номер и инструкцию"
    if _SERVICE.search(blob) and not _FURNITURE.search(blob):
        return "service", "договор, акт выполненных работ, чек и гарантию на работы (срок, что покрывает)"
    if _FURNITURE.search(blob):
        return "furniture", "чек или договор, гарантийные условия и фото готовой сборки"
    if cat.startswith("Транспорт"):
        return "auto", "заказ-наряд или чек, гарантию на работы и запчасти"
    if cat.startswith("Здоровье"):
        return "health", "чек и назначение врача (пригодится для налогового вычета)"
    if cat.startswith("Путешествия"):
        return "travel", "билеты, бронь и страховку"
    return "other", "чек и гарантию, если она есть"


def purchase_text(p: dict) -> str:
    what = p.get("comment") or p.get("subcategory") or p.get("category") or "покупка"
    where = f" ({p['merchant']})" if p.get("merchant") and p["merchant"] not in ("Не указан", "") else ""
    return (f"🧾 Крупная покупка: {what}{where}\n{rus_date(p.get('date', ''))}, {money(p.get('amount'))}.\n\n"
            f"Попроси и сохрани: {advice(p)[1]}. Это пригодится при гарантийном случае, споре или продаже.")


def purchase_buttons(h: str) -> list:
    return [[("📎 Пришлю документы", f"pd:{h}"), ("📅 Гарантия до…", f"pw:{h}")],
            [("⏰ Напомни позже", f"pl:{h}"), ("✖ Не нужно", f"px:{h}")]]


# ---------- что пора сказать ----------

def due_alerts(data: dict, db: Database, now: datetime) -> list[tuple[str, str, list | None]]:
    """(id, текст, кнопки) того, о чём ещё не говорили. Данные из блока life ответа «Ады»."""
    seen = set(_load(db, SEEN, []))
    later = _load(db, LATER, {})
    meta = _load(db, META, {})
    out: list[tuple[str, str, list | None]] = []
    life = data.get("life") if isinstance(data, dict) and isinstance(data.get("life"), dict) else {}
    for p in life.get("big_purchases") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        aid = f"buy:{p['id']}"
        if aid in seen or later.get(aid, "") > now.date().isoformat():
            continue
        h = short_id(aid)
        meta[h] = {"id": aid, "title": str(p.get("comment") or p.get("subcategory") or p.get("category") or "покупка")[:80],
                   "date": p.get("date", ""), "amount": p.get("amount"), "merchant": p.get("merchant", "")}
        out.append((aid, purchase_text(p), purchase_buttons(h)))
    income = life.get("income") if isinstance(life.get("income"), dict) else {}
    for kind, label in (("salary", "зарплата"), ("bonus", "премия")):
        row = income.get(kind)
        if not isinstance(row, dict) or not row.get("date"):
            continue
        aid = f"inc:{kind}:{row['date']}"
        try:
            fresh = (now.date() - date.fromisoformat(row["date"][:10])).days <= 2
        except ValueError:
            fresh = False
        if fresh and aid not in seen:
            out.append((aid, f"💰 Пришла {label}: {money(row.get('amount'))} ({rus_date(row['date'])}). "
                             "Хороший момент свериться с платежами и лимитами месяца: /budget.", None))
    if out:
        _save(db, META, dict(list(meta.items())[-40:]))
    return out


def mark_seen(db: Database, aid: str) -> None:
    seen = _load(db, SEEN, [])
    if aid not in seen:
        seen.append(aid)
    _save(db, SEEN, seen[-CAP:])


def after_sent(db: Database, aid: str, now: datetime) -> None:
    """Доход говорим один раз. Покупку без ответа напомним ещё до двух раз с интервалом в 3 дня."""
    if not aid.startswith("buy:"):
        mark_seen(db, aid)
        return
    counts = _load(db, "jarvis_nudges", {})
    counts[aid] = int(counts.get(aid, 0)) + 1
    _save(db, "jarvis_nudges", dict(list(counts.items())[-CAP:]))
    if counts[aid] >= 3:
        mark_seen(db, aid)
    else:
        snooze(db, aid, 3, now)


def snooze(db: Database, aid: str, days: int, now: datetime) -> None:
    later = _load(db, LATER, {})
    later[aid] = (now.date() + timedelta(days=days)).isoformat()
    _save(db, LATER, dict(list(later.items())[-CAP:]))


def purchase_meta(db: Database, h: str) -> dict | None:
    return _load(db, META, {}).get(h)


def should_run(db: Database, now: datetime) -> bool:
    raw = db.get_setting(LAST_RUN, "")
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return (now.replace(tzinfo=None) - last).total_seconds() >= RUN_EVERY_MIN * 60


def stamp_run(db: Database, now: datetime) -> None:
    db.set_setting(LAST_RUN, now.replace(tzinfo=None).isoformat(timespec="seconds"))


# ---------- гарантия: «до какого числа» ----------

_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")
_SPAN = re.compile(r"(\d+)\s*(год|года|лет|мес|месяц|месяца|месяцев)", re.I)


def parse_warranty(text: str, today: date) -> date | None:
    """«23.10.2028», «2 года», «18 месяцев», «год». None, если не поняли."""
    low = text.lower().strip()
    m = _DATE.search(low)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = _SPAN.search(low)
    n, unit = (int(m[1]), m[2]) if m else ((1, "год") if re.search(r"\bгод\b", low) else (0, ""))
    if not n:
        return None
    months = n * 12 if unit.startswith(("год", "лет")) else n
    y, mo = divmod(today.month - 1 + months, 12)
    year, month = today.year + y, mo + 1
    day = min(today.day, 28)
    return date(year, month, day)
