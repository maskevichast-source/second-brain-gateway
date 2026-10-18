from datetime import datetime

import pytest

from app import when

NOW = datetime(2026, 10, 18, 10, 0)      # воскресенье, утро


@pytest.mark.parametrize("text,expected,has_time", [
    ("через час", datetime(2026, 10, 18, 11, 0), True),
    ("Через 2 часа", datetime(2026, 10, 18, 12, 0), True),
    ("через полчаса", datetime(2026, 10, 18, 10, 30), True),
    ("через 45 минут", datetime(2026, 10, 18, 10, 45), True),
    ("через пару часов", datetime(2026, 10, 18, 12, 0), True),
    ("на 2 часа", datetime(2026, 10, 18, 12, 0), True),             # «отложи на 2 часа» = через 2 часа
    ("на час", datetime(2026, 10, 18, 11, 0), True),
    ("на полчаса", datetime(2026, 10, 18, 10, 30), True),
    ("на 3 дня", datetime(2026, 10, 21, 9, 0), False),
    ("через неделю", datetime(2026, 10, 25, 9, 0), False),
    ("через 3 дня", datetime(2026, 10, 21, 9, 0), False),
    ("в 18:30", datetime(2026, 10, 18, 18, 30), True),
    ("в 18", datetime(2026, 10, 18, 18, 0), True),
    ("в 6 вечера", datetime(2026, 10, 18, 18, 0), True),
    ("в 9", datetime(2026, 10, 19, 9, 0), True),              # 9:00 уже прошло: завтра
    ("вечером", datetime(2026, 10, 18, 19, 0), True),
    ("после обеда", datetime(2026, 10, 18, 14, 0), True),
    ("завтра", datetime(2026, 10, 19, 9, 0), False),
    ("завтра в 10", datetime(2026, 10, 19, 10, 0), True),
    ("завтра утром", datetime(2026, 10, 19, 9, 0), True),
    ("завтра вечером", datetime(2026, 10, 19, 19, 0), True),
    ("послезавтра", datetime(2026, 10, 20, 9, 0), False),
    ("в пятницу", datetime(2026, 10, 23, 9, 0), False),
    ("в пятницу в 18:00", datetime(2026, 10, 23, 18, 0), True),
    ("в воскресенье", datetime(2026, 10, 25, 9, 0), False),   # сегодня воскресенье: имеется в виду следующее
    ("на выходных", datetime(2026, 10, 24, 9, 0), False),
    ("на следующей неделе", datetime(2026, 10, 19, 9, 0), False),
    ("25 октября", datetime(2026, 10, 25, 9, 0), False),
    ("25.10", datetime(2026, 10, 25, 9, 0), False),
    ("5 ноября в 15:30", datetime(2026, 11, 5, 15, 30), True),
    ("1 октября", datetime(2027, 10, 1, 9, 0), False),        # в этом году прошло: в следующем
])
def test_parse_when(text, expected, has_time):
    assert when.parse_when(text, NOW) == (expected, has_time)


@pytest.mark.parametrize("text", ["", "ерунда какая-то", "купить молоко", "когда-нибудь", "в 25 часов"])
def test_unparseable(text):
    assert when.parse_when(text, NOW) is None


def test_timezone_aware_now_is_fine():
    from zoneinfo import ZoneInfo
    aware = datetime(2026, 10, 18, 10, 0, tzinfo=ZoneInfo("Asia/Almaty"))
    assert when.parse_when("через час", aware) == (datetime(2026, 10, 18, 11, 0), True)


def test_options_adapt_to_time_of_day():
    codes = lambda h: [c for _, c in when.snooze_options(datetime(2026, 10, 18, h, 0))]  # noqa: E731
    assert codes(10) == ["h1", "h2", "pm", "ev", "tm", "wk"]          # раннее утро: часы, после обеда, вечером
    assert codes(15) == ["h1", "h2", "ev", "tm", "wk"]
    assert codes(18) == ["h1", "h2", "tm", "wk"]
    assert codes(22) == ["tm", "te", "wk"]                           # поздно: только завтра
    assert when.snooze_time("h2", NOW) == (datetime(2026, 10, 18, 12, 0), True)
    assert when.snooze_time("tm", NOW) == (datetime(2026, 10, 19, 9, 0), True)


def test_human():
    assert when.human(datetime(2026, 10, 18, 12, 0), True, NOW) == "сегодня в 12:00"
    assert when.human(datetime(2026, 10, 19, 9, 0), False, NOW) == "завтра"
    assert when.human(datetime(2026, 10, 25, 9, 0), False, NOW) == "25 октября (вс)"
