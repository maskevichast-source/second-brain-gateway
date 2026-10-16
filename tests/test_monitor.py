import asyncio
import json
import logging

import httpx

from app import monitor
from app.monitor import Monitor
from app import ada, llm
from app.db import Database
from .conftest import StubLLM, auth, make_settings, note, result, vault_note
from .test_telegram import send, tg, update  # noqa: F401


class Sink:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))
        return True


def fresh(tmp_path):
    db = Database(tmp_path / "m.db")
    db.init()
    sink = Sink()
    m = Monitor()
    m.configure(db, sink, [111])
    return m, db, sink


def test_alert_needs_repeats_and_is_limited_to_once_an_hour(tmp_path):
    m, db, sink = fresh(tmp_path)
    t = 10_000.0
    m.failure("deepseek", httpx.ReadTimeout("секретный текст заметки https://api.telegram.org/botTOKEN"), now=t)
    m.failure("telegram", RuntimeError("x"), now=t + 1)
    assert asyncio.run(m.flush(t + 2)) is False                         # пока всего два сбоя
    m.failure("deepseek", ValueError("y"), now=t + 3)
    assert asyncio.run(m.flush(t + 4)) is True and len(sink.sent) == 1
    text = sink.sent[0][1]
    assert "DeepSeek: 2" in text and "ReadTimeout" in text and "TOKEN" not in text and "секретный" not in text
    for i in range(5):
        m.failure("webhook", KeyError("z"), now=t + 10 + i)
    assert asyncio.run(m.flush(t + 600)) is False and len(sink.sent) == 1   # час ещё не прошёл
    for i in range(3):
        m.failure("webhook", KeyError("z"), now=t + 4000 + i)
    assert asyncio.run(m.flush(t + 4100)) is True and len(sink.sent) == 2


def test_alert_without_sender_or_chats_is_silent(tmp_path):
    m, db, _ = fresh(tmp_path)
    m.configure(db, None, [111])
    for i in range(4):
        m.failure("stt", None, now=100.0 + i)
    assert asyncio.run(m.flush(200.0)) is False


def test_structured_log_has_no_exception_text(tmp_path, caplog):
    m, _, _ = fresh(tmp_path)
    with caplog.at_level(logging.ERROR, logger="second_brain.monitor"):
        m.failure("deepseek", ValueError("текст заметки и токен"))
    line = caplog.records[0].getMessage()
    payload = json.loads(line.split("gateway_error ", 1)[1])
    assert payload == {"event": "gateway_error", "source": "deepseek", "error": "ValueError"}
    assert "токен" not in line


def test_llm_final_failure_is_recorded(tmp_path):
    m = monitor.MONITOR
    m.configure(None)

    class Boom(httpx.AsyncClient):
        async def post(self, *a, **k):
            raise httpx.ConnectError("нет сети")

    settings = make_settings(tmp_path)
    from datetime import datetime
    asyncio.run(llm.structure_capture(settings, content="x", link_context="", clarifications="", revision=1,
                                      now=datetime(2026, 10, 12, 9, 0), context="", people_text="",
                                      http_client=Boom(), attempts=1, retry_delay=0))
    assert [e[1:] for e in m.events] == [("deepseek", "ConnectError")]


def test_webhook_exception_is_recorded_and_user_gets_message(tg, monkeypatch):
    client = tg(StubLLM(None))
    monitor.MONITOR.events.clear()

    async def boom(*a, **k):
        raise RuntimeError("секрет")

    monkeypatch.setattr("app.main.handle_update", boom)
    r = send(client, update("привет", uid=1)).json()
    assert "Что-то пошло не так" in r["text"]
    assert [e[1:] for e in monitor.MONITOR.events] == [("webhook", "RuntimeError")]


def test_status_shows_queue_and_last_snapshot(tg):
    client = tg(StubLLM(None))
    text = send(client, update("/status", uid=1)).json()["text"]
    assert "Ждут выгрузки в хранилище: 0" in text and "Последний успешный срез: полная синхронизация ещё не было" in text
    client.post("/snapshot", json={"corpus": vault_note("Дюна", type="media")}, headers=auth())
    text = send(client, update("/status", uid=2)).json()["text"]
    assert "В срезе заметок: 1" in text and "UTC" in text


def test_backup_command_sends_json_without_note_texts(make_client, tg):
    sent = []

    async def fake(chat_id, filename, data, caption=""):
        sent.append((chat_id, filename, data))
        return True

    client = tg(StubLLM(None), backup_sender=fake)
    client.post("/profile", json={"context": "Живу в Астане", "people": [{"name": "Серик", "role": "друг"}]}, headers=auth())
    client.post("/snapshot", json={"corpus": "\n=====NOTE=====\n".join([
        vault_note("Дюна", type="media", body="ТЕЛО ЗАМЕТКИ"), vault_note("Секрет", type="note", private=True)])}, headers=auth())
    reply = send(client, update("/backup", uid=1)).json()["text"]
    assert "Отправил файл" in reply and sent[0][0] == 111 and sent[0][1].startswith("second-brain-backup-")
    data = json.loads(sent[0][2])
    assert data["profile"]["context"] == "Живу в Астане" and data["profile"]["people"][0]["name"] == "Серик"
    titles = [f["title"] for f in data["snapshot"]]
    assert titles == ["Дюна"] and "ТЕЛО ЗАМЕТКИ" not in sent[0][2].decode("utf-8")


def test_backup_only_for_allow_list_and_needs_token(tg):
    client = tg(StubLLM(None))
    assert "TELEGRAM_BOT_TOKEN" in send(client, update("/backup", uid=1)).json()["text"]
    assert send(client, update("/backup", uid=2, user=999)).json() == {}
