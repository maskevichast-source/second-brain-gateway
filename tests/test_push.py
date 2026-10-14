import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import scheduler
from app.db import Database
from app.review import build_health, format_health, format_resurface, pick_resurface
from app.vault import load_notes
from .conftest import StubLLM, auth, make_settings, vault_note
from .test_telegram import send, tg, update  # noqa: F401

TZ = ZoneInfo("Asia/Almaty")


def at(h, m=0, d=12):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


def corpus(*parts):
    return "\n=====NOTE=====\n".join(parts)


def notes_of(*parts):
    return load_notes(corpus(*parts))[0]


def fresh_db(tmp_path, *rows):
    db = Database(tmp_path / "p.db")
    db.init()
    db.snapshot_put(list(rows))
    return db


class Sender:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return True


def settings_for(tmp_path):
    return make_settings(tmp_path, telegram_allowed_ids=frozenset({111}), telegram_bot_token="x")


def test_resurface_picks_old_open_things_only():
    notes = notes_of(
        vault_note("Старая идея", type="idea", status="сырая", created="2026-07-01T10:00", summary='"Сделать сад"'),
        vault_note("Реализованная", type="idea", status="реализована", created="2026-07-01T10:00"),
        vault_note("Свежая", type="idea", status="сырая", created="2026-10-10T10:00"),
        vault_note("Фильм", type="media", status="хочу", created="2026-06-01T10:00"),
        vault_note("Задача", type="task", status="открыто", created="2026-06-01T10:00"))
    picked = pick_resurface(notes, date(2026, 10, 12), count=5)
    assert {n.title for n in picked} == {"Старая идея", "Фильм"}
    assert pick_resurface(notes, date(2026, 10, 12)) == pick_resurface(notes, date(2026, 10, 12))   # на день одно и то же
    assert "Из прошлого" in format_resurface(picked, date(2026, 10, 12))
    assert format_resurface([], date(2026, 10, 12)) == ""


def test_health_finds_problems():
    notes = notes_of(
        vault_note("Дюна", type="media", status="хочу"), vault_note("дюна", type="media", status="хочу"),
        vault_note("Ссылка 1", type="bookmark", source_url="https://a.com/x"),
        vault_note("Ссылка 2", type="bookmark", source_url="https://a.com/x/"),
        vault_note("Старое дело", type="task", status="открыто", created="2026-08-01T10:00"),
        vault_note("Непонятное", type="note", tags=["unprocessed"]),
        vault_note("Вася", type="person"))
    score, issues = build_health(notes, date(2026, 10, 12))
    assert score < 100 and len(issues) == 4
    text = format_health(notes, date(2026, 10, 12))
    assert "Здоровье базы" in text and "дубли" in text and "Старое дело" in text
    assert "Замечаний нет" in format_health(notes_of(vault_note("Чисто", type="note")), date(2026, 10, 12))


def test_due_reminder_fires_once_in_window(tmp_path):
    db = fresh_db(tmp_path, ("k1", {"type": "task", "title": "Позвонить Серику", "status": "открыто", "remind_at": "2026-10-12T15:00"}))
    settings, sender = settings_for(tmp_path), Sender()
    run = lambda now: asyncio.run(scheduler.tick(db, settings, now, sender))  # noqa: E731
    run(at(14, 59))
    assert sender.sent == []
    run(at(15, 1))
    assert len(sender.sent) == 1 and "Позвонить Серику" in sender.sent[0][1] and sender.sent[0][2]
    run(at(15, 5))
    assert len(sender.sent) == 1                       # второй раз не шлём


def test_stale_reminder_and_done_are_skipped(tmp_path):
    db = fresh_db(tmp_path,
                  ("k1", {"type": "task", "title": "Давнее", "status": "открыто", "remind_at": "2026-10-10T10:00"}),
                  ("k2", {"type": "task", "title": "Сделано", "status": "сделано", "remind_at": "2026-10-12T10:00"}),
                  ("k3", {"type": "task", "title": "Только дата", "status": "открыто", "remind_at": "2026-10-12"}))
    sender = Sender()
    asyncio.run(scheduler.tick(db, settings_for(tmp_path), at(15), sender))
    assert not [m for m in sender.sent if "Давнее" in m[1] or "Сделано" in m[1] or "Только дата" in m[1]]


def test_event_reminder_30_minutes_before(tmp_path):
    db = fresh_db(tmp_path, ("e1", {"type": "event", "title": "Встреча с врачом", "status": "запланировано", "event_at": "2026-10-12T16:00"}))
    sender = Sender()
    settings = settings_for(tmp_path)
    asyncio.run(scheduler.tick(db, settings, at(15, 20), sender))
    assert sender.sent == []
    asyncio.run(scheduler.tick(db, settings, at(15, 31), sender))
    assert "Скоро событие" in sender.sent[0][1]


def test_morning_push_once_and_skips_when_nothing(tmp_path):
    db = fresh_db(tmp_path, ("k1", {"type": "task", "title": "Купить кабель", "status": "открыто", "due": "2026-10-12"}))
    sender, settings = Sender(), settings_for(tmp_path)
    asyncio.run(scheduler.tick(db, settings, at(8, 0), sender))
    assert sender.sent == []                            # ещё рано
    asyncio.run(scheduler.tick(db, settings, at(8, 31), sender))
    assert "Доброе утро" in sender.sent[0][1] and "Купить кабель" in sender.sent[0][1]
    asyncio.run(scheduler.tick(db, settings, at(9, 0), sender))
    assert len(sender.sent) == 1
    # пустая сводка без «из прошлого» не отправляется (проверяем на новой базе)
    db2 = Database(tmp_path / "quiet.db")
    db2.init()
    db2.snapshot_put([("z", {"type": "note", "title": "Заметка", "created": "2026-10-11T10:00"})])
    sender2 = Sender()
    asyncio.run(scheduler.tick(db2, settings, at(9, 0), sender2))
    assert sender2.sent == []


def test_weekly_push_on_sunday_evening_only(tmp_path):
    db = fresh_db(tmp_path, ("k1", {"type": "note", "title": "Заметка", "created": "2026-10-11T10:00"}))
    sender, settings = Sender(), settings_for(tmp_path)
    asyncio.run(scheduler.tick(db, settings, at(20, 0, d=13), sender))       # вторник
    assert not [m for m in sender.sent if "Обзор недели" in m[1]]
    asyncio.run(scheduler.tick(db, settings, at(20, 0, d=18), sender))       # воскресенье
    assert len([m for m in sender.sent if "Обзор недели" in m[1]]) == 1
    asyncio.run(scheduler.tick(db, settings, at(21, 0, d=18), sender))
    assert len([m for m in sender.sent if "Обзор недели" in m[1]]) == 1


def test_push_settings_commands_and_switches(tg, tmp_path):
    client = tg(StubLLM(None))
    assert "утренняя сводка: вкл, 08:30" in send(client, update("/push", uid=1)).json()["text"]
    assert "утренняя сводка: вкл, 07:45" in send(client, update("/push утро 07:45", uid=2)).json()["text"]
    assert "утренняя сводка: выкл" in send(client, update("/push утро выкл", uid=3)).json()["text"]
    assert "обзор недели (воскресенье, 19:00): выкл" in send(client, update("/push неделя выкл", uid=4)).json()["text"]
    assert "Время пиши так" in send(client, update("/push утро поздно", uid=5)).json()["text"]
    assert "Команды" in send(client, update("/push что-то", uid=6)).json()["text"]


def test_callback_buttons_snooze_and_done(tg):
    client = tg(StubLLM(None))
    key = "a1b2c3d4e5f6"

    def cb(data, uid=111, cid="1"):
        return {"update_id": 900 + hash(data) % 100, "callback_query": {"id": cid, "from": {"id": uid}, "data": data}}

    r = send(client, cb(f"snz1:{key}")).json()
    assert r["method"] == "answerCallbackQuery" and "через час" in r["text"]
    assert "завтра" in send(client, cb(f"snz24:{key}")).json()["text"]
    assert "больше не напомню" in send(client, cb(f"ok:{key}")).json()["text"]
    assert "Не понял" in send(client, cb("ok:xyz")).json()["text"]
    assert send(client, cb(f"ok:{key}", uid=999)).json() == {}                        # чужие кнопки игнорируются


def test_snoozed_reminder_fires_later(tmp_path):
    db = fresh_db(tmp_path, ("k1", {"type": "task", "title": "Позвонить", "status": "открыто", "remind_at": "2026-10-12T15:00"}))
    key = scheduler.reminder_key("Позвонить", "2026-10-12T15:00")
    db.set_setting(f"rem_{key}", "2026-10-12T16:00")
    sender, settings = Sender(), settings_for(tmp_path)
    asyncio.run(scheduler.tick(db, settings, at(15, 30), sender))
    assert sender.sent == []
    asyncio.run(scheduler.tick(db, settings, at(16, 1), sender))
    assert len(sender.sent) == 1


def test_health_and_remember_commands(tg):
    client = tg(StubLLM(None))
    client.post("/snapshot", json={"corpus": corpus(
        vault_note("Дюна", type="media", status="хочу", created="2026-06-01T10:00"),
        vault_note("дюна", type="media", status="хочу", created="2026-06-02T10:00"))}, headers=auth())
    assert "дубли" in send(client, update("/health", uid=1)).json()["text"]
    assert "Из прошлого" in send(client, update("/remember", uid=2)).json()["text"]
