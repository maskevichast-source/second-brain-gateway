from datetime import date, datetime

from app import review, scheduler
from app.snapshot import to_notes

TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 19, 0)


def notes(*fronts):
    return to_notes([dict(f) for f in fronts])


def test_week_review_has_decisions_promises_and_stuck_tasks():
    ns = notes(
        {"title": "Берём подрядчика B", "type": "decision", "created": "2026-10-23"},
        {"title": "Скинуть контакт Арману", "type": "task", "status": "открыто", "promise": "я", "created": "2026-10-20"},
        {"title": "Разобрать антресоль", "type": "task", "status": "открыто", "created": "2026-09-20"},
    )
    text = review.build_week_review(ns, NOW)
    assert "Решили за неделю" in text and "Берём подрядчика B" in text
    assert "я должен 1" in text and "/promises" in text
    assert "Зависло больше 3 недель" in text and "Разобрать антресоль" in text


def test_health_flags_past_events_and_long_overdue_tasks():
    ns = notes(
        {"title": "Встреча с врачом", "type": "event", "status": "запланировано", "event_at": "2026-10-10"},
        {"title": "Починить кран", "type": "task", "status": "открыто", "due": "2026-08-01"},
        {"title": "Арман", "type": "person", "orbit": "1"},
    )
    score, issues = review.build_health(ns, TODAY)
    joined = " ".join(issues)
    assert "События в прошлом" in joined and "просрочены больше месяца" in joined and "последнего контакта нет" in joined
    assert score < 100


def test_week_buttons_open_panel_for_overdue_tasks():
    ns = to_notes([{"title": "Позвонить в банк", "type": "task", "status": "открыто", "due": "2026-10-20", "_key": "a" * 12},
                   {"title": "Будущее дело", "type": "task", "status": "открыто", "due": "2026-11-20", "_key": "b" * 12}])
    rows = scheduler.week_buttons(ns, TODAY)
    assert rows == [[("✅ Позвонить в банк", "it:" + "a" * 12)]]
