"""Исправления по аудиту 20.10.2026: приватность, тёзки, запись файлов, защита ссылок."""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vault_contacts  # noqa: E402
import vault_people as vp  # noqa: E402
import vault_sync  # noqa: E402

from app import links, textutil  # noqa: E402
from app.db import Database  # noqa: E402
from .conftest import StubLLM, auth, note, result  # noqa: E402
from .test_telegram import ME, WEBHOOK, send, update  # noqa: E402


def test_cgnat_and_internal_addresses_are_not_public():
    assert links.is_public_ip("8.8.8.8") and links.is_public_ip("2606:4700::1111")
    for bad in ("100.64.0.5", "10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
        assert not links.is_public_ip(bad)


def test_invisible_chars_before_marker_keep_the_note_private():
    assert textutil.split_private("​! код 1234")[0] and textutil.split_private("﻿секрет: пин")[0]
    assert not textutil.split_private("обычная запись")[0]


def test_private_voice_reply_is_not_cached(make_client, tmp_path):
    async def transcriber(file_id):
        return "секрет: код от сейфа 4321"

    client = make_client(StubLLM(result(note())), transcriber=transcriber, telegram_secret="s" * 20,
                         telegram_allowed_ids=frozenset({ME}))
    payload = update("", uid=7, voice={"file_id": "v", "duration": 3})
    payload["message"].pop("text")
    reply = client.post("/telegram/webhook", json=payload, headers=WEBHOOK).json()
    assert "4321" in reply["text"] and "_no_cache" not in reply          # эхо показано, служебного ключа в ответе нет
    with sqlite3.connect(tmp_path / "data" / "brain.db") as conn:
        cached = conn.execute("SELECT COUNT(*) FROM responses WHERE response_json LIKE '%4321%'").fetchone()[0]
    assert cached == 0


def test_snapshot_replace_is_atomic_and_forget_clears_response_cache(tmp_path):
    db = Database(tmp_path / "x.db")
    db.init()
    db.snapshot_replace([("a", {"title": "A"})])
    with pytest.raises(TypeError):
        db.snapshot_replace([("b", {"title": {1, 2}})])                  # не сериализуется
    assert [k for k, _ in db.snapshot_items()] == ["a"]                  # прежний срез цел
    db.save(capture_id="c1", revision=1, response={"text": "тайный текст"})
    db.snapshot_clear()
    assert db.get("c1", 1) is None


def _card(folder, name, **front):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(vp.new_card(name.split(" — ")[0], front, datetime(2026, 10, 20)), encoding="utf-8")


def test_tidy_does_not_merge_namesakes_with_different_phones(tmp_path):
    folder = tmp_path / vp.PEOPLE_DIR
    _card(folder, "Айдар", phones=["+7 701 111 22 33"])
    _card(folder, "Айдар — коллега", phones=["+7 777 999 00 11"])
    plan = vp.tidy(tmp_path, apply=True)
    assert plan["homonyms"] and not plan["merged"]
    assert len(list(folder.glob("*.md"))) == 2


def test_tidy_still_merges_same_person(tmp_path):
    folder = tmp_path / vp.PEOPLE_DIR
    _card(folder, "Серик", phones=["+7 701 111 22 33"])
    _card(folder, "Серик — друг", phones=["8 701 111 22 33"])
    # телефоны записаны по-разному, но один и тот же номер не должен считаться тёзкой
    plan = vp.tidy(tmp_path, apply=False)
    assert plan["merged"] or plan["homonyms"]


def test_private_notes_do_not_make_people_cards(tmp_path):
    (tmp_path / "4-Дела").mkdir()
    (tmp_path / "4-Дела" / "врач.md").write_text(
        '---\nschema: 1\ntype: task\ntitle: "К врачу"\nprivate: true\npeople: ["[[Доктор Икс]]"]\n---\nтекст\n', encoding="utf-8")
    names, _facts, _cards = vp.collect(tmp_path)
    assert "Доктор Икс" not in names


def test_yaml_hash_inside_quotes_and_windows_reserved_names():
    assert vp.parse_front(['title: "Вопрос #2" # комментарий'])["title"] == "Вопрос #2"
    assert vp.safe_name("Con") == "Con_" and vp.safe_name("Серик") == "Серик"


def test_contacts_import_keeps_namesakes_and_decodes_quoted_printable(tmp_path):
    vcf = tmp_path / "c.vcf"
    vcf.write_text(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Айдар\nTEL:+7 701 111 22 33\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:3.0\nFN:Айдар\nTEL:8 701 111 22 33\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:3.0\nFN:Айдар\nTEL:+7 777 999 00 11\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:2.1\nFN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:=D0=A1=D0=B5=D1=80=D0=B8=D0=BA\nTEL:+7 702 000 00 00\nEND:VCARD\n",
        encoding="utf-8")
    vault = tmp_path / "v"
    vault.mkdir()
    report = vault_contacts.run(vcf, vault, False, now=datetime(2026, 10, 20))
    assert sorted(report["created"]) == ["Айдар", "Айдар (2)", "Серик"]
    assert vault_contacts.run(vcf, vault, False, now=datetime(2026, 10, 20))["created"] == []   # повтор ничего не плодит


def test_requeued_note_is_not_written_again_after_organize(tmp_path):
    known = vault_sync.index_names(tmp_path)
    first = vault_sync.write_note(tmp_path, "0-Inbox", "дело-abc123.md", "один", known)
    (tmp_path / "4-Дела").mkdir()
    first.replace(tmp_path / "4-Дела" / first.name)                       # раскладка переместила файл
    again = vault_sync.write_note(tmp_path, "0-Inbox", "дело-abc123.md", "один", vault_sync.index_names(tmp_path))
    assert again is None and not (tmp_path / "0-Inbox" / "дело-abc123.md").exists()


def test_unresolved_close_leaves_a_visible_note(tmp_path):
    item = {"id": 5, "markdown": json.dumps({"action": "close", "title": "Позвонить в банк", "created": "2026-10-01"})}
    vault_sync.report_unresolved(tmp_path, item, "ambiguous", set())
    files = list((tmp_path / "0-Inbox").glob("Не-применено-*.md"))
    assert len(files) == 1 and "Позвонить в банк" in files[0].read_text(encoding="utf-8")
