"""Дневник: вечерний диалог в Telegram и логика напоминания."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .ask import MONTHS
from .llm import WEEKDAYS

INTRO = (
    "📓 Дневник. Пять коротких вопросов, отвечай как удобно, хоть голосом через диктовку.\n"
    "• /skip пропускает вопрос, /done сохраняет то, что уже рассказал, /stop отменяет.\n"
    "• Начни любой ответ с «!», и вся запись сохранится без ИИ."
)

QUESTIONS = (
    ("Как прошёл день", "Как прошёл день? Расскажи свободно."),
    ("Что получилось", "Что получилось или удалось сегодня?"),
    ("Что не получилось", "Что не получилось, тревожит или отняло силы?"),
    ("Завтра", "Что важно сделать завтра? Дела, встречи, сроки."),
    ("Запомнить", "За что благодарен или что хочется запомнить? Оценку дня от 1 до 10 можно назвать, если хочешь."),
)

DEFAULT_TIME = "21:00"
REMINDER = (
    "📓 Вечер. Как прошёл день?\n/diary — расспрошу за пару минут. Или просто напиши «дневник: …».\n"
    "Выключить напоминание: /diary выкл. Другое время: /diary время 22:00."
)


def entry_date(now: datetime) -> date:
    """До 4 утра запись относится к вчерашнему дню."""
    return (now - timedelta(hours=4)).date()


def day_label(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year} ({WEEKDAYS[day.weekday()]})"


def question_text(index: int) -> str:
    return f"[{index + 1}/{len(QUESTIONS)}] {QUESTIONS[index][1]}"


def compile_entry(day: date, answers: dict[int, str], private: bool = False) -> str:
    lines = [f"[Дневник за {day_label(day)}] Запись дня: одна главная заметка типа journal. "
             "Дела на завтра и обещания вынеси отдельными заметками task, людей добавь в people."]
    for i, (title, _) in enumerate(QUESTIONS):
        text = answers.get(i, "").strip()
        if text:
            lines.append(f"{title}: {text}")
    body = "\n".join(lines)
    return "! " + body if private else body


def parse_time(text: str) -> str | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text) or re.fullmatch(r"\s*(\d{1,2})()\s*", text)
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def has_entry(fronts: list[dict], day: date) -> bool:
    return any(f.get("type") == "journal" and str(f.get("created", ""))[:10] == day.isoformat() for f in fronts)


def should_remind(now: datetime, *, on: bool, at: str, last_sent: str, entry_today: bool) -> bool:
    if not on or entry_today or last_sent == now.date().isoformat():
        return False
    hour, minute = (int(x) for x in at.split(":"))
    return (now.hour, now.minute) >= (hour, minute) and now.hour < 24 and entry_date(now) == now.date()
