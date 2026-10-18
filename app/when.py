"""Понимание «когда» по-русски: «через 2 часа», «в 18:30», «завтра утром», «в пятницу», «25 октября».

Без ИИ и без сети: просто правила. Возвращает (момент, указано_ли_время). Если время не названо,
момент равен 9:00 нужного дня, а указано_ли_время = False: это перенос на день, а не на час.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
WEEKDAYS = {"понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "сред": 2, "ср": 2, "четверг": 3, "чт": 3,
            "пятниц": 4, "пт": 4, "суббот": 5, "сб": 5, "воскресень": 6, "вс": 6}
PARTS = (("после обеда", 14), ("до обеда", 12), ("утром", 9), ("днем", 14), ("вечером", 19), ("ночью", 23))
WORD_NUM = {"пару": 2, "пара": 2, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6}
MORNING = 9


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip(" .,!?")


def _naive(now: datetime) -> datetime:
    return now.replace(tzinfo=None, second=0, microsecond=0)


def _relative(t: str, now: datetime) -> tuple[datetime, bool] | None:
    m = re.search(r"\b(?:через|на)\s+(полчаса|полтора часа|(\d+|пару|пара|два|две|три|четыре|пять|шесть)?\s*"
                  r"(минут\w*|мин|час\w*|ч|дн\w*|день|недел\w*))\b", t)
    if not m:
        return None
    if m.group(1) == "полчаса":
        return _naive(now) + timedelta(minutes=30), True
    if m.group(1) == "полтора часа":
        return _naive(now) + timedelta(minutes=90), True
    raw = m.group(2)
    n = int(raw) if raw and raw.isdigit() else WORD_NUM.get(raw or "", 1)
    unit = m.group(3)
    if unit.startswith("мин"):
        return _naive(now) + timedelta(minutes=n), True
    if unit.startswith("ч"):
        return _naive(now) + timedelta(hours=n), True
    if unit.startswith("д"):
        return (_naive(now) + timedelta(days=n)).replace(hour=MORNING, minute=0), False
    return (_naive(now) + timedelta(weeks=n)).replace(hour=MORNING, minute=0), False


def _date_in(t: str, today: date) -> tuple[date | None, str]:
    """Явная дата («25 октября», «25.10», «25.10.2026») и текст без неё."""
    m = re.search(r"\b(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)", t)
    day = month = year = None
    if m:
        day = int(m.group(1))
        word = m.group(2)
        month = next((v for k, v in MONTHS.items() if word.startswith(k)), None)
        rest = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
        if not m:
            return None, t
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) + (2000 if m.group(3) and len(m.group(3)) == 2 else 0) if m.group(3) else None
        rest = t[:m.start()] + " " + t[m.end():]
    if not month or not 1 <= day <= 31:
        return None, t
    for y in ([year] if year else [today.year, today.year + 1]):
        try:
            found = date(y, month, day)
        except ValueError:
            continue
        if found >= today or year:
            return found, rest
    return None, t


def parse_when(text: str, now: datetime) -> tuple[datetime, bool] | None:
    t = _norm(text)
    if not t:
        return None
    rel = _relative(t, now)
    if rel:
        return rel
    base = _naive(now)
    today = base.date()
    day, t = _date_in(t, today)

    if day is None:
        if re.search(r"\bпослезавтра\b", t):
            day = today + timedelta(days=2)
        elif re.search(r"\bзавтра\b", t):
            day = today + timedelta(days=1)
        elif re.search(r"\bсегодня\b", t):
            day = today
        elif re.search(r"\b(на )?выходных?\b", t):
            day = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
        elif re.search(r"следующей неделе|на неделе после", t):
            day = today + timedelta(days=7 - today.weekday())
        else:
            for word, number in WEEKDAYS.items():
                if re.search(rf"\b(в|во|на|к)\s+{word}\w*\b", t) or (len(word) == 2 and re.search(rf"\b{word}\b", t)):
                    day = today + timedelta(days=(number - today.weekday()) % 7 or 7)
                    break

    hour = minute = None
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        hour, minute = int(m.group(1)), int(m.group(2))
        tail = t[m.end():]
    else:
        m = re.search(r"\b(?:в|к|до)\s+(\d{1,2})(?:\s*(?:час\w*|ч))?\b", t)
        if m and int(m.group(1)) < 24:
            hour, minute = int(m.group(1)), 0
            tail = t[m.end():]
        else:
            tail = ""
    if hour is not None:
        zone = re.match(r"\s*(утра|дня|вечера|ночи)\b", tail)
        if zone:
            if zone.group(1) in ("дня", "вечера") and hour < 12:
                hour += 12
            elif zone.group(1) == "ночи" and hour == 12:
                hour = 0
    else:
        for word, h in PARTS:
            if word in t:
                hour, minute = h, 0
                break

    if day is None and hour is None:
        return None
    if hour is None:                                                    # только день: «завтра», «в пятницу», «25 октября»
        return datetime.combine(day, datetime.min.time()).replace(hour=MORNING), False
    if day is None:                                                     # только время: сегодня, а если уже прошло, то завтра
        day = today
        if datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) <= base:
            day = today + timedelta(days=1)
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute), True


# ---------- варианты переноса для кнопок: зависят от времени суток ----------

LABELS = {"h1": "Через час", "h2": "Через 2 часа", "pm": "После обеда (14:00)", "ev": "Вечером (19:00)",
          "tm": "Завтра (9:00)", "te": "Завтра вечером", "wk": "Через неделю"}


def snooze_options(now: datetime) -> list[tuple[str, str]]:
    """Какие переносы предложить сейчас: утром — часы и «после обеда», поздно вечером — только завтра."""
    hour = now.hour
    codes: list[str] = []
    if hour < 21:
        codes += ["h1", "h2"]
    if hour < 12:
        codes.append("pm")
    if hour < 17:
        codes.append("ev")
    codes.append("tm")
    if hour >= 21:
        codes.append("te")
    codes.append("wk")
    return [(LABELS[c], c) for c in codes]


def snooze_time(code: str, now: datetime) -> tuple[datetime, bool] | None:
    base = _naive(now)
    today = datetime.combine(base.date(), datetime.min.time())
    if code == "h1":
        return base + timedelta(hours=1), True
    if code == "h2":
        return base + timedelta(hours=2), True
    if code == "pm":
        return today.replace(hour=14), True
    if code == "ev":
        return today.replace(hour=19), True
    if code == "tm":
        return today.replace(hour=9) + timedelta(days=1), True
    if code == "te":
        return today.replace(hour=19) + timedelta(days=1), True
    if code == "wk":
        return today.replace(hour=9) + timedelta(days=7), False
    return None


def human(when: datetime, has_time: bool, now: datetime) -> str:
    today = _naive(now).date()
    delta = (when.date() - today).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(delta)
    if day is None:
        names = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        day = f"{when.day} {months[when.month - 1]} ({names[when.weekday()]})"
    return f"{day} в {when:%H:%M}" if has_time else day
