import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import diary, scheduler
from app.db import Database
from .conftest import FIXED_NOW, StubLLM, auth, make_settings, note, result
from .test_telegram import send, tg, update  # noqa: F401

TZ = ZoneInfo("Asia/Almaty")


def at(h, m=0, d=12):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


def test_entry_date_and_time_parsing():
    assert diary.entry_date(at(2, 30)) == date(2026, 10, 11)
    assert diary.entry_date(at(21)) == date(2026, 10, 12)
    assert diary.parse_time("21:30") == "21:30" and diary.parse_time("9") == "09:00" and diary.parse_time("25:00") is None
    assert diary.parse_time("abc") is None


def test_should_remind_rules():
    kw = dict(on=True, at="21:00", last_sent="", entry_today=False)
    assert not diary.should_remind(at(20, 59), **kw)
    assert diary.should_remind(at(21, 0), **kw)
    assert diary.should_remind(at(23, 30), **kw)
    assert not diary.should_remind(at(1, 0), **kw)                       # ночью после полуночи не будим
    assert not diary.should_remind(at(21), **{**kw, "on": False})
    assert not diary.should_remind(at(21), **{**kw, "entry_today": True})
    assert not diary.should_remind(at(21), **{**kw, "last_sent": "2026-10-12"})


def test_tick_sends_once_and_respects_entry(tmp_path):
    db = Database(tmp_path / "d.db")
    db.init()
    settings = make_settings(tmp_path, telegram_allowed_ids=frozenset({111}), telegram_bot_token="x")
    sent = []

    async def send_fn(chat_id, text):
        sent.append((chat_id, text))
        return True

    assert asyncio.run(scheduler.tick(db, settings, at(20), send_fn)) == 0
    assert asyncio.run(scheduler.tick(db, settings, at(21, 5), send_fn)) == 1
    assert asyncio.run(scheduler.tick(db, settings, at(21, 6), send_fn)) == 0      # уже отправляли
    assert sent == [(111, diary.REMINDER)]
    assert asyncio.run(scheduler.tick(db, settings, at(21, 5, d=13), send_fn)) == 1  # на следующий день снова
    db.set_setting("diary_time", "22:00")
    assert asyncio.run(scheduler.tick(db, settings, at(21, 30, d=14), send_fn)) == 0
    db.snapshot_put([("k", {"type": "journal", "title": "День", "created": "2026-10-14T20:00"})])
    assert asyncio.run(scheduler.tick(db, settings, at(22, 30, d=14), send_fn)) == 0  # запись уже есть


def test_failed_send_is_retried(tmp_path):
    db = Database(tmp_path / "d.db")
    db.init()
    settings = make_settings(tmp_path, telegram_allowed_ids=frozenset({111}))

    async def bad(chat_id, text):
        return False

    asyncio.run(scheduler.tick(db, settings, at(21, 5), bad))
    assert db.get_setting("diary_last_sent") == ""


def test_diary_dialog_saves_one_entry(tg):
    llm = StubLLM(result(note(type="journal", title="День 12 октября", body="x")))
    client = tg(llm)
    start = send(client, update("/diary", uid=1)).json()["text"]
    assert "Дневник" in start and "[1/5]" in start
    assert "[2/5]" in send(client, update("Хороший день, " + "много " * 100, uid=2)).json()["text"]    # длинный ответ не считается новой записью
    assert "[3/5]" in send(client, update("/skip", uid=3)).json()["text"]
    assert "[4/5]" in send(client, update("тревожно из-за УК", uid=4)).json()["text"]
    done = send(client, update("/done", uid=5)).json()["text"]
    assert "Дневник записан" in done
    content = llm.calls[0]["content"]
    assert content.startswith("[Дневник за 12 октября 2026") and "Как прошёл день: Хороший день" in content
    assert "Что не получилось: тревожно" in content and "Завтра" not in content
    assert llm.calls[0]["revision"] == 2
    # после сохранения диалог закрыт
    assert "[" not in send(client, update("мысль", uid=6)).json()["text"][:1]


def test_diary_inline_prefix_and_private(tg):
    llm = StubLLM(result(note(type="journal", title="День", body="x")))
    client = tg(llm)
    assert "Дневник записан" in send(client, update("дневник: сегодня был на встрече", uid=1)).json()["text"]
    assert "Как прошёл день: сегодня был на встрече" in llm.calls[0]["content"]
    send(client, update("/diary", uid=2))
    send(client, update("! только для меня", uid=3))
    out = send(client, update("/done", uid=4)).json()["text"]
    assert "без ИИ" in out and len(llm.calls) == 1                      # приватный дневник до ИИ не дошёл
    assert "только для меня" in str(client.get("/pending", headers=auth()).json())


def test_diary_settings_commands(tg):
    client = tg(StubLLM(None))
    assert "21:00" in send(client, update("/diary статус", uid=1)).json()["text"]
    assert "22:15" in send(client, update("/diary время 22:15", uid=2)).json()["text"]
    assert "Напиши время" in send(client, update("/diary время вечером", uid=3)).json()["text"]
    assert "выключено" in send(client, update("/diary выкл", uid=4)).json()["text"]
    assert "22:15" in send(client, update("/diary вкл", uid=5)).json()["text"]
    assert "токен бота" in send(client, update("/diary статус", uid=6)).json()["text"]


def test_stop_cancels_diary(tg):
    client = tg(StubLLM(None))
    send(client, update("/diary", uid=1))
    assert "остановил" in send(client, update("/stop", uid=2)).json()["text"].lower()
