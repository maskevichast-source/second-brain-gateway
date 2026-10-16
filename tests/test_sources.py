import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vault_sync  # noqa: E402

from app import sources
from .conftest import StubLLM, auth, note, result
from .test_files import Fetcher, make_pdf, pdf_update
from .test_telegram import send, tg, update  # noqa: F401


def pending(client):
    return client.get("/pending?service=1", headers=auth()).json()["items"]


def test_collect_all_kinds_and_too_big():
    msg = {"photo": [{"file_id": "s", "file_size": 10}, {"file_id": "big", "file_size": 999}],
           "document": {"file_id": "d", "file_name": "Договор.pdf", "mime_type": "application/pdf", "file_size": 5},
           "voice": {"file_id": "v", "duration": 3}}
    ok, big = sources.collect(msg)
    assert [i["kind"] for i in ok] == ["фото", "pdf", "голос"] and ok[0]["file_id"] == "big" and not big
    ok, big = sources.collect({"document": {"file_id": "x", "file_name": "film.mp4", "file_size": sources.MAX_BYTES + 1}})
    assert not ok and big == ["film.mp4"]
    ok, _ = sources.collect({"document": {"file_id": "i", "file_name": "scr.png", "mime_type": "image/png"}})
    assert ok[0]["kind"] == "скрин"


def test_plan_names_are_stable_and_attach_is_idempotent():
    items = [{"file_id": "a", "kind": "pdf", "name": "Договор аренды.pdf", "mime": "application/pdf", "size": 1},
             {"file_id": "b", "kind": "голос", "name": "voice.ogg", "mime": "audio/ogg", "size": 1}]
    a = sources.plan(items, "tg-1", "2026-10-16")
    assert a == sources.plan(items, "tg-1", "2026-10-16")
    assert a[0]["filename"].startswith("2026-10-16-договор-аренды-") and a[0]["filename"].endswith(".pdf")
    assert a[1]["filename"].endswith(".ogg") and a[0]["folder"] == "7-Исходники/2026-10"
    md = '---\nschema: 1\ntitle: "X"\n---\n\nТекст\n'
    out = sources.attach(md, a)
    assert "source_files:" in out and f'  - "[[{a[0]["filename"]}]]"' in out
    assert f"![[{a[1]['filename']}]]" in out and out.count("## Исходники") == 1
    assert sources.attach(out, a) == out


def test_pdf_and_voice_originals_go_to_queue_and_notes_link_them(tg):
    llm = StubLLM(result(note(type="document", title="Договор", body="x")))
    client = tg(llm, file_fetcher=Fetcher(make_pdf("Contract valid until 2026-12-01")))
    reply = send(client, pdf_update(caption="Договор аренды", file_id="PDF1")).json()["text"]
    assert "7-Исходники" in reply
    items = pending(client)
    kinds = [i["kind"] for i in items]
    assert kinds.count("note") == 1 and kinds.count("file") == 1
    f = next(i for i in items if i["kind"] == "file")
    meta = json.loads(f["markdown"])
    assert meta["file_id"] == "PDF1" and f["folder"].startswith("7-Исходники/")
    n = next(i for i in items if i["kind"] == "note")
    assert f"[[{f['filename']}]]" in n["markdown"] and "## Исходники" in n["markdown"]
    # телефон (без service=1) файлы не видит и не может их стереть
    phone = client.get("/pending", headers=auth()).json()["items"]
    assert [i["kind"] for i in phone] == ["note"]


def test_file_endpoint_streams_from_telegram_without_storing(tg):
    client = tg(StubLLM(result(note(type="document", title="Д", body="x"))), file_fetcher=Fetcher(make_pdf("hello")))
    send(client, pdf_update(file_id="PDF2"))
    f = next(i for i in pending(client) if i["kind"] == "file")
    assert client.get(f"/file/{f['id']}").status_code == 401
    r = client.get(f"/file/{f['id']}", headers=auth())
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    note_id = next(i for i in pending(client) if i["kind"] == "note")["id"]
    assert client.get(f"/file/{note_id}", headers=auth()).status_code == 404       # только исходники
    assert client.get("/file/99999", headers=auth()).status_code == 404


def test_file_endpoint_reports_telegram_failure(tg):
    fetcher = Fetcher(make_pdf("hello"))
    client = tg(StubLLM(result(note(type="document", title="Д", body="x"))), file_fetcher=fetcher)
    send(client, pdf_update(file_id="PDF3"))
    f = next(i for i in pending(client) if i["kind"] == "file")
    fetcher.data = None                                  # Telegram больше не отдаёт файл
    assert client.get(f"/file/{f['id']}", headers=auth()).status_code == 502


def test_photo_screenshot_is_kept_even_without_text_recognition(tg):
    client = tg(StubLLM(result(note(type="note", title="Скрин", body="x"))))
    out = send(client, update("глянуть потом", uid=9, photo=[{"file_id": "s1", "file_size": 10},
                                                              {"file_id": "s2", "file_size": 20}])).json()["text"]
    assert "7-Исходники" in out
    f = next(i for i in pending(client) if i["kind"] == "file")
    assert json.loads(f["markdown"])["file_id"] == "s2" and f["filename"].endswith(".jpg")


def test_voice_with_clarification_keeps_sources(tg):
    from .test_voice import voice_update
    from app.llm import LLMResult
    llm = StubLLM(LLMResult.model_validate({"notes": [{"type": "task", "title": "Позвонить", "body": "x", "confidence": 0.6}],
                                            "needs_review": True, "review_questions": ["Кому?"]}))
    async def stt(file_id):
        return "позвонить тому парню"
    client = tg(llm, transcriber=stt)
    reply = send(client, voice_update(1)).json()["text"]
    assert "Нужно уточнение" in reply and pending(client) == []
    llm.value = result(note(type="task", title="Позвонить Серику", body="x"))
    send(client, update("Серику насчёт ремонта", uid=2))
    items = pending(client)
    f = next(i for i in items if i["kind"] == "file")
    n = next(i for i in items if i["kind"] == "note")
    assert f["filename"].endswith(".ogg") and f"![[{f['filename']}]]" in n["markdown"]


def test_big_file_is_reported_not_queued(tg):
    client = tg(StubLLM(result(note(type="note", title="Видео", body="x"))))
    msg = update("смотри", uid=5, document={"file_id": "V", "file_name": "film.mp4", "mime_type": "video/mp4",
                                           "file_size": sources.MAX_BYTES + 5})
    out = send(client, msg).json()["text"]
    assert "20 МБ" in out and all(i["kind"] != "file" for i in pending(client))


def test_commands_never_attach_sources(tg):
    client = tg()
    send(client, update("/help", uid=6, photo=[{"file_id": "p"}]))
    assert pending(client) == []


# ---------- ноутбук ----------

def test_sync_saves_file_without_overwriting_and_acks(tmp_path, monkeypatch):
    calls = []
    batch = {"items": [{"id": 7, "kind": "file", "filename": "2026-10-16-фото-ab12cd.jpg",
                        "folder": "7-Исходники/2026-10", "markdown": "{}"}], "remaining": 1}
    monkeypatch.setattr(vault_sync, "api", lambda url, tok, path, body=None: calls.append((path, body)) or (batch if path.startswith("/pending") else {}))
    monkeypatch.setattr(vault_sync, "download", lambda url, tok, path: b"JPEGDATA")
    assert vault_sync.sync_once("http://x", "t", tmp_path) == 0
    saved = tmp_path / "7-Исходники/2026-10/2026-10-16-фото-ab12cd.jpg"
    assert saved.read_bytes() == b"JPEGDATA"
    assert ("/ack", {"ids": [7]}) in calls
    saved.write_bytes(b"MINE")
    vault_sync.sync_once("http://x", "t", tmp_path)
    assert saved.read_bytes() == b"MINE"


def test_sync_keeps_item_when_download_fails_and_does_not_loop(tmp_path, monkeypatch):
    import urllib.error
    pulls = []
    batch = {"items": [{"id": 8, "kind": "file", "filename": "a.pdf", "folder": "7-Исходники/2026-10", "markdown": "{}"}],
             "remaining": 5}
    monkeypatch.setattr(vault_sync, "api", lambda url, tok, path, body=None: pulls.append(path) or batch)
    def boom(url, tok, path):
        raise urllib.error.HTTPError(url, 502, "bad", {}, None)
    monkeypatch.setattr(vault_sync, "download", boom)
    vault_sync.sync_once("http://x", "t", tmp_path)
    assert len(pulls) == 1 and "/ack" not in pulls            # не подтверждаем и не крутимся по кругу


def test_sync_path_cannot_escape_vault(tmp_path):
    item = {"folder": "../../etc", "filename": "../../evil.pdf"}
    path = vault_sync.save_source(tmp_path, item, b"x")
    assert tmp_path in path.parents and path.name == "evil.pdf"
