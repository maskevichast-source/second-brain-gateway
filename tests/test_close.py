import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vault_sync  # noqa: E402
import json

from app import scheduler
from .conftest import StubLLM, auth, vault_note
from .test_telegram import send, tg, update  # noqa: F401


def cb(data, uid=111):
    return {"update_id": 5000 + abs(hash(data)) % 1000, "callback_query": {"id": "1", "from": {"id": uid}, "data": data}}


NOTE = ('---\nschema: 1\ntitle: "Позвонить маме"\ncreated: 2026-10-11T10:00\ntype: "task"\nstatus: "открыто"\n'
        'remind_at: 2026-10-12T15:00\n---\n\nТекст\n')


def test_done_button_queues_close_record_for_laptop_only(tg):
    client = tg(StubLLM(None))
    client.post("/snapshot", json={"corpus": vault_note(
        "Позвонить маме", type="task", status="открыто", remind_at="2026-10-12T15:00", created="2026-10-11T10:00")},
        headers=auth())
    key = scheduler.reminder_key("Позвонить маме", "2026-10-12T15:00")
    assert "Отмечу сделанным" in send(client, cb(f"ok:{key}")).json()["text"]
    send(client, cb(f"ok:{key}"))                                              # повторное нажатие не плодит записи
    assert client.get("/pending", headers=auth()).json()["items"] == []        # телефон и старые клиенты не видят
    items = client.get("/pending?service=1", headers=auth()).json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "close"
    assert json.loads(items[0]["markdown"]) == {"action": "close", "title": "Позвонить маме",
                                                "created": "2026-10-11T10:00", "type": "task", "status": "сделано"}
    assert "Ждут выгрузки в хранилище: 0" in send(client, update("/status", uid=7)).json()["text"]
    assert client.post("/ack", json={"ids": [items[0]["id"]]}, headers=auth()).json()["removed"] == 1


def test_done_button_unknown_key_is_harmless(tg):
    client = tg(StubLLM(None))
    assert "больше не напомню" in send(client, cb("ok:a1b2c3d4e5f6")).json()["text"]
    assert client.get("/pending?service=1", headers=auth()).json()["items"] == []


def test_vault_close_changes_status_only(tmp_path):
    folder = tmp_path / "0-Inbox"
    folder.mkdir()
    path = folder / "a.md"
    path.write_text(NOTE, encoding="utf-8", newline="")
    payload = {"action": "close", "title": "Позвонить маме", "created": "2026-10-11T10:00", "status": "сделано"}
    assert vault_sync.close_note(tmp_path, payload) == "closed"
    text = path.read_text(encoding="utf-8")
    assert 'status: "сделано"' in text and text.replace('"сделано"', '"открыто"') == NOTE
    assert vault_sync.close_note(tmp_path, payload) == "already"
    assert not list(folder.glob("*.tmp-sync"))


def test_vault_close_not_found_ambiguous_and_hidden(tmp_path):
    payload = {"action": "close", "title": "Позвонить маме", "created": "2026-10-11T10:00", "status": "сделано"}
    assert vault_sync.close_note(tmp_path, payload) == "not_found"
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(NOTE, encoding="utf-8")
    assert vault_sync.close_note(tmp_path, payload) == "ambiguous"
    (tmp_path / "b.md").unlink()
    hidden = tmp_path / ".trash"
    hidden.mkdir()
    (hidden / "c.md").write_text(NOTE, encoding="utf-8")
    assert vault_sync.close_note(tmp_path, payload) == "closed"
    assert 'status: "открыто"' in (hidden / "c.md").read_text(encoding="utf-8")   # корзина не трогается
    assert vault_sync.close_note(tmp_path, {"title": "", "status": "x"}) == "invalid"


def test_vault_close_adds_missing_status_and_keeps_crlf(tmp_path):
    path = tmp_path / "x.md"
    path.write_bytes('---\r\ntitle: Дело\r\ntype: task\r\n---\r\nтело\r\n'.encode("utf-8"))
    assert vault_sync.close_note(tmp_path, {"title": "Дело", "status": "сделано"}) == "closed"
    assert path.read_bytes().decode("utf-8") == '---\r\ntitle: Дело\r\ntype: task\r\nstatus: "сделано"\r\n---\r\nтело\r\n'


def test_sync_once_applies_service_and_still_writes_notes(tmp_path, monkeypatch):
    (tmp_path / "t.md").write_text(NOTE, encoding="utf-8")
    calls = []
    close = {"action": "close", "title": "Позвонить маме", "created": "2026-10-11T10:00", "status": "сделано"}

    def fake_api(url, token, path, body=None):
        calls.append(path)
        if path.startswith("/pending"):
            assert "service=1" in path
            if "/ack" in calls:
                return {"items": [], "remaining": 0}
            return {"items": [
                {"id": 1, "kind": "note", "folder": "0-Inbox", "filename": "n.md", "markdown": "текст"},
                {"id": 2, "kind": "close", "folder": "", "filename": "close-x.json", "markdown": json.dumps(close)},
                {"id": 3, "kind": "close", "folder": "", "filename": "close-y.json", "markdown": "не json"},
                {"id": 4, "folder": "0-Inbox", "filename": "old.md", "markdown": "без kind"},
            ], "remaining": 4}
        assert body == {"ids": [1, 2, 3, 4]}
        return {"removed": 4}

    monkeypatch.setattr(vault_sync, "api", fake_api)
    assert vault_sync.sync_once("http://x", "t", tmp_path) == 2           # две обычные заметки
    assert (tmp_path / "0-Inbox" / "n.md").exists() and (tmp_path / "0-Inbox" / "old.md").exists()
    assert 'status: "сделано"' in (tmp_path / "t.md").read_text(encoding="utf-8")
    assert not list((tmp_path / "0-Inbox").glob("close*"))


def test_vault_sync_is_stdlib_only():
    import ast
    import pathlib
    tree = ast.parse((pathlib.Path(vault_sync.__file__)).read_text(encoding="utf-8"))
    mods = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (getattr(n, "names", None) or [n])}
    assert mods <= {"__future__", "argparse", "json", "os", "re", "sys", "time", "urllib", "pathlib", "hashlib", "vault_organize", "vault_people"}   # два последних лежат рядом, тоже stdlib


def test_ack_accepts_single_id_number_or_text(tg):
    client = tg(StubLLM(None))
    for value in (5, "5", [5], ["5"]):
        assert client.post("/ack", json={"ids": value}, headers=auth()).status_code == 200
    assert client.post("/ack", json={"id": 5}, headers=auth()).status_code == 200
    assert client.post("/ack", json={"ids": "abc"}, headers=auth()).status_code == 422


def test_requeue_returns_acked_notes_but_does_not_duplicate_queued_ones(tmp_path):
    from app.db import Database
    db = Database(tmp_path / "t.db")
    db.init()
    file = {"filename": "2026-10-17-test.md", "folder": "0-Inbox", "markdown": "---\ntype: note\n---\nтекст", "reminders": []}
    db.save(capture_id="tgi-1-1", revision=1, response={"files": [file], "needs_review": False})
    db.save(capture_id="tgi-2-1", revision=1, response={"files": [dict(file, filename="x.md")], "needs_review": True})
    db.save(capture_id="reply-3", revision=1, response={"method": "sendMessage"})
    assert db.requeue_recent(6) == 1 and db.requeue_recent(6) == 0
    items = db.pending(10)
    assert [i["filename"] for i in items] == ["2026-10-17-test.md"]
    db.ack([items[0]["id"]])
    assert db.requeue_recent(6) == 1


def test_facts_join_context_and_feed_voice_hint(tmp_path):
    from app import stt
    from app.db import Database
    db = Database(tmp_path / "f.db")
    db.init()
    db.set_context("Живу в Астане")
    db.add_fact("Собаку зовут Янка")
    db.add_fact("собаку зовут янка")                      # повтор не плодится
    assert db.get_facts() == ["собаку зовут янка"]
    assert "Живу в Астане" in db.get_context() and "янка" in db.get_context()
    hint = stt.build_hint(["Диана Шангараева"], ["Собаку зовут Янка"])
    assert "Янка" in hint and "Диана" in hint
    db.clear_profile()
    assert db.get_facts() == [] and db.get_context() == ""


def test_interview_position_survives_person_question(tmp_path):
    import asyncio

    from app import telegram
    from app.db import Database

    db = Database(tmp_path / "t.db")
    db.init()
    db.set_state(1, "interview", {"i": 20, "saved": 3})
    telegram._ask_person(1, db, ["Ерасыл", "Виктор"], "ok", batch=False)
    state = db.get_state(1)
    assert state[0] == "person" and state[1]["resume"]["i"] == 20

    reply = asyncio.run(telegram._person_step(1, 5, "", "/skip", state[1], db, None, None))
    assert state[1]["rest"] == ["Виктор"] and db.get_state(1)[1]["resume"]["i"] == 20 and "Виктор" in reply["text"]
    reply = asyncio.run(telegram._person_step(1, 6, "", "/skip", db.get_state(1)[1], db, None, None))
    restored = db.get_state(1)
    assert restored[0] == "interview" and restored[1]["i"] == 20 and not restored[1].get("paused")
    assert "Продолжаем интервью" in reply["text"]


def test_interview_jump_to_number(tmp_path):
    from app import telegram
    from app.db import Database

    db = Database(tmp_path / "t.db")
    db.init()
    db.set_state(1, "person", {"current": "X", "rest": [], "resume": {"i": 3, "saved": 2}})
    text = telegram._interview_start(1, db, "22")["text"]
    assert db.get_state(1)[0] == "interview" and db.get_state(1)[1]["i"] == 21 and "[22/" in text
