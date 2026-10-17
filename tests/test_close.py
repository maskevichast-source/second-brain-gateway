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
    assert mods <= {"__future__", "argparse", "json", "os", "sys", "time", "urllib", "pathlib", "hashlib", "vault_organize", "vault_people"}   # два последних лежат рядом, тоже stdlib
