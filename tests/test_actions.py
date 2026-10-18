"""Дела из сводки: номера, кнопки, «сделано 1», перенос и применение на ноутбуке."""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vault_sync  # noqa: E402

from app import actions, ask, telegram  # noqa: E402
from app.db import Database  # noqa: E402
from app.vault import VaultNote  # noqa: E402

NOW = datetime(2026, 10, 18, 8, 30)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.init()
    rows = [
        ("a" * 40, {"title": "Заехать к отцу", "type": "task", "status": "открыто", "due": "2026-10-17", "created": "2026-10-10T10:00"}),
        ("b" * 40, {"title": "Купить молоко", "type": "task", "status": "открыто", "due": "2026-10-18", "created": "2026-10-11T10:00"}),
        ("c" * 40, {"title": "Семейный завтрак", "type": "home", "next_due": "2026-10-18", "repeat": "каждые две недели"}),
        ("d" * 40, {"title": "Владислав Маскевич", "type": "person", "birthday": "10-29"}),
        ("e" * 40, {"title": "Владислав Маскевич — профиль", "type": "person", "birthday": "10-29"}),
        ("f" * 40, {"title": "Серик", "type": "person", "birthday": "10-25"}),
    ]
    d.snapshot_put(rows)
    return d


def test_digest_is_numbered_and_hides_own_birthday(db):
    text, buttons = actions.render_day(db, NOW)
    assert "1. 17 октября (сб), просрочено на 1 дн.: Заехать к отцу" in text
    assert "2. 18 октября (вс): Купить молоко" in text and "Семейный завтрак" in text
    assert "Серик" in text and "Владислав" not in text                  # своё имя и дубль «— профиль» не показываем
    assert [b[0] for b in buttons[0]] == ["1", "2", "3"] and buttons[0][0][1] == "it:" + "a" * 12     # только ряд номеров
    assert "сделано 1" in text


def test_done_cancel_snooze_update_snapshot_and_queue_for_laptop(db):
    actions.render_day(db, NOW)
    reply = telegram._task_action(1, "done", "1", db, NOW)
    assert "Готово: Заехать к отцу" in reply["text"]
    front = dict(db.snapshot_items())["a" * 40]
    assert front["status"] == "сделано"
    service = db.pending(service=True)
    assert json.loads(service[0]["markdown"]) == {"action": "close", "title": "Заехать к отцу", "created": "2026-10-10T10:00",
                                                    "type": "task", "status": "сделано"}
    assert db.pending() == []                                          # телефону служебные записи не отдаются

    assert "Отменено: Купить молоко" in telegram._task_action(1, "cancel", "купить молоко", db, NOW)["text"]
    assert dict(db.snapshot_items())["b" * 40]["status"] == "отменено"
    text, _ = actions.render_day(db, NOW)
    assert "Заехать" not in text and "молоко" not in text              # закрытое пропало из сводки сразу


def test_repeating_item_done_moves_to_next_time(db):
    actions.render_day(db, NOW)
    reply = telegram._task_action(1, "done", "3", db, NOW)
    assert "Готово: Семейный завтрак" in reply["text"]
    assert dict(db.snapshot_items())["c" * 40]["next_due"] == "2026-11-01"      # через две недели, а не закрыто навсегда
    assert "status" not in dict(db.snapshot_items())["c" * 40]
    payload = json.loads(db.pending(service=True)[0]["markdown"])
    assert payload["action"] == "set" and payload["fields"] == {"next_due": "2026-11-01"}


def test_snooze_keeps_time_and_moves_overdue_to_tomorrow(db):
    db.snapshot_put([("g" * 40, {"title": "Позвонить маме", "type": "task", "status": "открыто",
                                 "due": "2026-10-17", "remind_at": "2026-10-17T21:00"})])
    code, _ = actions.apply(db, "g" * 12, "snooze", NOW)
    front = dict(db.snapshot_items())["g" * 40]
    assert code == "ok" and front["due"] == "2026-10-19" and front["remind_at"] == "2026-10-19T21:00"


def test_numbers_need_a_fresh_list_and_unknown_phrases_are_notes(db):
    assert "Сначала открой список" in telegram._task_action(1, "done", "1", db, NOW)["text"]
    actions.render_day(db, NOW)
    assert "Нет дела с номером 9" in telegram._task_action(1, "done", "9", db, NOW)["text"]
    assert telegram._task_action(1, "done", "договор подписан у юриста", db, NOW) is None   # не дело: пойдёт как запись
    later = datetime(2026, 10, 25, 9, 0)
    assert "давно не обновлялся" in telegram._task_action(1, "done", "1", db, later)["text"]


def test_phrases_and_ambiguity():
    assert actions.parse_phrase("Сделано 1 и 3") == ("done", "1 и 3")
    assert actions.parse_phrase("отмена: купить молоко") == ("cancel", "купить молоко")
    assert actions.parse_phrase("отложи 2") == ("snooze", "2")
    assert actions.parse_phrase("Сегодня сделал отличный проект" + " x" * 60) is None
    assert actions.parse_phrase("/done 1") is None


def test_two_similar_tasks_ask_to_clarify(db):
    db.snapshot_put([("h" * 40, {"title": "Заказать пиццу детям", "type": "task", "status": "открыто"}),
                     ("i" * 40, {"title": "Заказать пиццу маме", "type": "task", "status": "открыто"})])
    reply = telegram._task_action(1, "done", "заказать пиццу", db, NOW)
    assert "несколько дел" in reply["text"] and "детям" in reply["text"]
    assert "Готово: Заказать пиццу маме" in telegram._task_action(1, "done", "заказать пиццу маме", db, NOW)["text"]


def test_laptop_applies_set_and_close(tmp_path):
    note = tmp_path / "2-Дела" / "a.md"
    note.parent.mkdir()
    note.write_text('---\ntitle: "Позвонить маме"\ntype: task\ncreated: 2026-10-10T10:00\nstatus: "открыто"\ndue: 2026-10-17\n---\nтекст\n',
                    encoding="utf-8")
    payload = {"action": "set", "title": "Позвонить маме", "created": "2026-10-10T10:00",
               "fields": {"due": "2026-10-19", "remind_at": "2026-10-19T21:00"}}
    assert vault_sync.apply_service(tmp_path, {"markdown": json.dumps(payload)}) == "updated"
    text = note.read_text(encoding="utf-8")
    assert "due: 2026-10-19\n" in text and "remind_at: 2026-10-19T21:00\n" in text and text.endswith("---\nтекст\n")
    bad = {"action": "set", "title": "Позвонить маме", "fields": {"title": "взлом"}}
    assert vault_sync.apply_service(tmp_path, {"markdown": json.dumps(bad)}) == "invalid"      # менять можно только сроки и статус


def test_buttons_work_through_callbacks(db):
    from .conftest import make_settings
    settings = make_settings(Path(db.path).parent, telegram_allowed_ids=frozenset({111}))
    reply = telegram._callback({"id": "1", "from": {"id": 111}, "data": "dn:" + "a" * 12}, settings, db, lambda: NOW)
    assert reply["method"] == "answerCallbackQuery" and "Готово: Заехать к отцу" in reply["text"]
    again = telegram._callback({"id": "2", "from": {"id": 111}, "data": "dn:" + "a" * 12}, settings, db, lambda: NOW)
    assert "Уже закрыто" in again["text"]


def test_panel_options_follow_time_of_day(db):
    morning = actions.panel(db, "a" * 12, datetime(2026, 10, 18, 10, 0))
    labels = [b[0] for row in morning[1] for b in row]
    assert "✅ Готово" in labels and "🗑 Отменить" in labels and "⏰ Через час" in labels and "⏰ После обеда (14:00)" in labels
    assert "Заехать к отцу" in morning[0] and "просрочено на 1 дн." in morning[0]
    night = actions.panel(db, "a" * 12, datetime(2026, 10, 18, 22, 30))
    night_labels = [b[0] for row in night[1] for b in row]
    assert "⏰ Через час" not in night_labels and "⏰ Завтра (9:00)" in night_labels
    assert any(b[1] == "sx:" + "a" * 12 for row in morning[1] for b in row)       # «своё время»


def test_reschedule_by_hours_sets_reminder_and_clears_overdue(db):
    when = datetime(2026, 10, 18, 12, 0)
    assert actions.reschedule(db, "a" * 12, when, True, NOW) == ("ok", "Заехать к отцу")
    front = dict(db.snapshot_items())["a" * 40]
    assert front["remind_at"] == "2026-10-18T12:00" and front["due"] == "2026-10-18"     # больше не просрочено
    payload = json.loads(db.pending(service=True)[0]["markdown"])
    assert payload["action"] == "set" and payload["fields"] == {"remind_at": "2026-10-18T12:00", "due": "2026-10-18"}
    # в 12:00 бот сам напомнит, и у напоминания есть кнопка «другое время»
    from app import scheduler
    due = scheduler.due_reminders(db.snapshot_items(), db, datetime(2026, 10, 18, 12, 1))
    assert len(due) == 1 and due[0][1] == "Заехать к отцу" and due[0][3] == "a" * 12
    assert scheduler.due_reminders(db.snapshot_items(), db, datetime(2026, 10, 18, 11, 0)) == []


def test_reschedule_by_day_keeps_clock_time(db):
    db.snapshot_put([("g" * 40, {"title": "Позвонить маме", "type": "task", "status": "открыто",
                                 "due": "2026-10-17", "remind_at": "2026-10-17T21:00"})])
    actions.reschedule(db, "g" * 12, datetime(2026, 10, 21, 9, 0), False, NOW)
    front = dict(db.snapshot_items())["g" * 40]
    assert front["due"] == "2026-10-21" and front["remind_at"] == "2026-10-21T21:00"


def test_typed_snooze_with_time_and_without(db):
    actions.render_day(db, NOW)
    reply = telegram._task_action(1, "snooze", "1 на 2 часа", db, NOW)
    assert "Перенёс: Заехать к отцу — сегодня в 10:30. Напомню в Telegram." in reply["text"]
    panel = telegram._task_action(1, "snooze", "2", db, NOW)               # без времени: варианты кнопками
    assert panel["reply_markup"]["inline_keyboard"] and "Что сделать?" in panel["text"]
    assert "Не понял, на когда" in telegram._task_action(1, "snooze", "2 на абракадабру", db, NOW)["text"]
    assert "Выбери одно дело" in telegram._task_action(1, "snooze", "1 и 2", db, NOW)["text"]


def test_callback_flow_edits_message_in_place(db):
    from .conftest import make_settings
    settings = make_settings(Path(db.path).parent, telegram_allowed_ids=frozenset({111}))
    msg = {"message_id": 77, "chat": {"id": 111}, "text": "Сводка на 18 октября"}

    def tap(data):
        return telegram._callback({"id": "1", "from": {"id": 111}, "data": data, "message": msg}, settings, db, lambda: NOW)

    opened = tap("it:" + "a" * 12)
    assert opened["method"] == "editMessageText" and opened["message_id"] == 77 and "Что сделать?" in opened["text"]
    snoozed = tap("sn:" + "a" * 12 + ":h2")
    assert snoozed["method"] == "editMessageText" and "Заехать к отцу: сегодня в 10:30" in snoozed["text"]
    assert "Напомню в Telegram" in snoozed["text"] and "1. " in snoozed["text"]          # и сразу обновлённый список
    done = tap("dn:" + "b" * 12)
    assert "Готово: Купить молоко" in done["text"] and "Купить молоко" not in done["text"].split("\n\n", 1)[1]
    back = tap("bk:" + "0" * 12)
    assert back["method"] == "editMessageText" and "Сводка на" in back["text"]
    ask_time = tap("sx:" + "a" * 12)
    assert ask_time["method"] == "sendMessage" and "На когда перенести" in ask_time["text"]
    assert db.get_state(111)[0] == "resched"


def test_free_time_answer_reschedules_and_new_note_is_not_swallowed(db):
    import asyncio
    from .conftest import make_settings
    settings = make_settings(Path(db.path).parent, telegram_allowed_ids=frozenset({111}))
    db.set_state(111, "resched", {"key": "a" * 12, "title": "Заехать к отцу"})

    async def process(**kw):
        return {"files": [], "needs_review": False, "count": 0}

    def say(text, uid):
        update = {"update_id": uid, "message": {"message_id": uid, "from": {"id": 111}, "chat": {"id": 111, "type": "private"}, "text": text}}
        return asyncio.run(telegram.handle_update(update, settings=settings, db=db, process=process, clock=lambda: NOW))

    assert "Не понял" in say("ну не знаю когда", 1)["text"] and db.get_state(111)[0] == "resched"
    assert "Перенёс: Заехать к отцу — сегодня в 11:30" in say("через 3 часа", 2)["text"]
    assert db.get_state(111) is None
    db.set_state(111, "resched", {"key": "a" * 12, "title": "Заехать к отцу"})
    say("Купить подарок маме на день рождения и торт", 3)           # похоже на новую запись, а не на время
    assert db.get_state(111) is None
