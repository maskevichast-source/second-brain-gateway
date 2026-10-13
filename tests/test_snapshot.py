import sqlite3

from app.snapshot import row_from_note, to_notes
from app.vault import load_notes
from .conftest import StubAsk, StubLLM, auth, note, result, vault_note
from .test_telegram import send, tg, update  # noqa: F401


def corpus(*parts):
    return "\n=====NOTE=====\n".join(parts)


def test_row_has_no_body_and_skips_private():
    notes, _ = load_notes(vault_note("Договор", type="document", body="СЕКРЕТНЫЙ ТЕКСТ", valid_until="2026-12-01"))
    key, front = row_from_note(notes[0])
    assert "СЕКРЕТНЫЙ ТЕКСТ" not in str(front) and front["valid_until"] == "2026-12-01" and len(key) == 40
    private = parse = vault_note("Тайна", type="note", private=True)
    assert load_notes(private)[0] == []


def test_sync_endpoint_and_fallback_to_snapshot(make_client, tmp_path):
    client = make_client()
    body = corpus(vault_note("Серик", type="person", body="ПАРОЛЬ 123", last_contact="2026-08-01"),
                  vault_note("Тайна", type="note", private=True),
                  vault_note("Сделать", type="task", status="открыто", due="2026-10-13"))
    r = client.post("/snapshot", json={"corpus": body}, headers=auth())
    assert r.json()["stored"] == 2 and r.json()["private_skipped"] == 1
    assert client.get("/snapshot", headers=auth()).json()["notes"] == 2
    # без corpus сервис использует срез
    r = client.post("/digest", json={"mode": "week"}, headers=auth())
    assert "Давно не общались" in r.json()["digest"]
    # в базе нет текста заметок и приватного
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    dump = " ".join(row[0] for row in conn.execute("SELECT front FROM snapshot"))
    assert "ПАРОЛЬ" not in dump and "Тайна" not in dump
    assert client.delete("/snapshot", headers=auth()).json()["removed"] == 2
    assert client.get("/snapshot", headers=auth()).json()["notes"] == 0
    assert client.post("/snapshot", json={"corpus": ""}, headers=auth()).status_code == 422
    assert client.get("/snapshot").status_code == 401


def test_capture_adds_to_snapshot_but_private_does_not(make_client, movie_result):
    client = make_client(StubLLM(movie_result))
    client.post("/capture", json={"capture_id": "cap-aaaaaaaa", "content": "посмотреть Дюну", "revision": 1}, headers=auth())
    client.post("/capture", json={"capture_id": "cap-bbbbbbbb", "content": "! личное", "revision": 1}, headers=auth())
    info = client.get("/snapshot", headers=auth()).json()
    assert info["notes"] == 1
    r = client.post("/ask", json={"question": "что хочу посмотреть?"}, headers=auth())
    assert r.status_code == 200


def test_sync_keeps_pending_telegram_notes(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "x.db")
    db.init()
    db.snapshot_put([("k1", {"title": "A", "type": "note"})])
    db.snapshot_replace([("k2", {"title": "B", "type": "note"})])
    assert [f["title"] for f in db.snapshot_fronts()] == ["B"]
    assert db.snapshot_info()[0] == 1 and db.snapshot_info()[1]
    db.snapshot_put([(f"x{i}", {"title": str(i), "type": "note"}) for i in range(5)], max_rows=3)
    assert db.snapshot_info()[0] == 3
    assert db.snapshot_clear() == 3 and db.snapshot_info() == (0, "")


def test_telegram_secretary_commands(tg):
    stub = StubAsk({"answer": "Серику обещал смету", "used": [1]})
    client = tg(StubLLM(result(note(type="task", title="Смета Серику", body="x", people=["Серик"],
                                    fields={"due": "2026-10-12"}))), ask_llm=stub)
    assert "Срез хранилища пуст" in send(client, update("/day", uid=1)).json()["text"]
    client.post("/snapshot", json={"corpus": corpus(
        vault_note("Серик", type="person", relation="друг", last_contact="2026-08-01"),
        vault_note("Смета Серику", type="task", status="ждёт", people=["Серик"], due="2026-10-10"))}, headers=auth())
    assert "Просрочено" in send(client, update("/day", uid=2)).json()["text"]
    assert "Обзор недели" in send(client, update("/week", uid=3)).json()["text"]
    assert "Брифинг: Серик" in send(client, update("/brief серик", uid=4)).json()["text"]
    assert "С кем встреча" in send(client, update("/brief", uid=5)).json()["text"]
    assert "Напиши вопрос" in send(client, update("/ask", uid=6)).json()["text"]
    answer = send(client, update("/ask что я обещал Серику?", uid=7)).json()["text"]
    assert "Серику обещал смету" in answer and "Источники" in answer
    assert "не подключены" in send(client, update("/budget", uid=8)).json()["text"]
    assert "В срезе заметок: 2" in send(client, update("/sync", uid=9)).json()["text"]
    assert "Стёр срез" in send(client, update("/forget", uid=10)).json()["text"]
    assert "В срезе заметок: 0" in send(client, update("/sync", uid=11)).json()["text"]
