import asyncio
from datetime import date

from app.ask import INDEX_MIN_NOTES, NoteIndex, answer_question, select_lines
from app.review import build_health, build_merge_hint, build_week_review, format_merge, format_old_bookmarks, old_bookmarks
from app.vault import load_notes
from .conftest import FIXED_NOW, StubAsk, StubLLM, auth, make_settings, vault_note
from .test_telegram import send, tg, update  # noqa: F401


def notes_of(*parts):
    return load_notes("\n=====NOTE=====\n".join(parts))[0]


def dup_notes():
    return notes_of(
        vault_note("Фильм Дюна", type="media", created="2026-09-01T10:00"),
        vault_note("фильм  дюна", type="media", created="2026-10-01T10:00", year=2024, creator="Вильнёв"),
        vault_note("Уникальная", type="note"))


def test_merge_output_says_keep_and_merge_with_missing_fields():
    text = format_merge(dup_notes())
    assert "оставить главной: «фильм  дюна»" in text
    assert "объединить с главной: «Фильм Дюна»" in text and "/merge 1" in text and "Ничего не удаляю" in text


def test_merge_hint_file_is_a_normal_note_and_deletes_nothing():
    reply, file = build_merge_hint(dup_notes(), 1, FIXED_NOW)
    assert file and file["folder"] == "0-Inbox" and file["filename"].endswith(".md") and "Подготовил" in reply
    md = file["markdown"]
    assert md.startswith("---") and "merge-hint" in md and "[[фильм  дюна]]" in md and "[[Фильм Дюна]]" in md
    assert "сам удали" in md
    assert build_merge_hint(dup_notes(), 5, FIXED_NOW)[1] is None
    assert build_merge_hint(notes_of(vault_note("Один", type="note")), 1, FIXED_NOW)[1] is None


def test_merge_command_queues_hint_once(tg):
    client = tg(StubLLM(None))
    client.post("/snapshot", json={"corpus": "\n=====NOTE=====\n".join([
        vault_note("Дюна", type="media", created="2026-06-01T10:00"),
        vault_note("дюна", type="media", created="2026-06-02T10:00")])}, headers=auth())
    assert "Подсказка: /merge 1" in send(client, update("/merge", uid=1)).json()["text"]
    assert "Подготовил" in send(client, update("/merge 1", uid=2)).json()["text"]
    assert "уже готовил" in send(client, update("/merge 1", uid=3)).json()["text"]
    assert "Нет такой группы" in send(client, update("/merge 9", uid=4)).json()["text"]
    items = client.get("/pending", headers=auth()).json()["items"]
    assert len(items) == 1 and "merge-hint" in items[0]["markdown"]


def test_old_bookmarks_report_is_deterministic():
    today = date(2026, 10, 12)
    notes = notes_of(
        vault_note("Старая", type="bookmark", status="не-разобрано", created="2026-08-01T10:00"),
        vault_note("Свежая", type="bookmark", status="не-разобрано", created="2026-10-10T10:00"),
        vault_note("Разобранная", type="bookmark", status="разобрано", created="2026-07-01T10:00"))
    assert [(a, n.title) for a, n in old_bookmarks(notes, today)] == [(72, "Старая")]
    assert "«Старая» — 72 дн." in format_old_bookmarks(notes, today)
    assert format_old_bookmarks(notes[1:], today) == ""
    assert any("Закладки без разбора" in i for i in build_health(notes, today)[1])
    assert "Закладки без разбора старше 14 дней: 1" in build_week_review(notes, FIXED_NOW)


def big_vault(n=800):
    return [vault_note(f"Заметка {i}", type="note", summary=f"про тему{i} и всякое", created="2026-09-01T10:00")
            for i in range(n)] + [vault_note("Рецепт борща", type="idea", summary="свёкла, капуста", created="2026-09-02T10:00")]


def test_index_selects_like_plain_search_within_budget():
    notes = notes_of(*big_vault())
    assert len(notes) > INDEX_MIN_NOTES
    index = NoteIndex(notes)
    budget = 6000
    plain, plain_omitted = select_lines(notes, "что там про борщ?", budget)
    fast, fast_omitted = index.select("что там про борщ?", budget)
    assert sum(len(line) + 1 for _, line in fast) <= budget
    assert {n.title for n, _ in fast} == {n.title for n, _ in plain} and fast_omitted == plain_omitted
    assert "Рецепт борща" in {n.title for n, _ in fast}
    whole, omitted = index.select("борщ", 10 ** 9)
    assert len(whole) == len(notes) and omitted == 0


def test_answer_question_uses_index_and_keeps_budget(tmp_path):
    notes = notes_of(*big_vault())
    settings = make_settings(tmp_path, ask_budget_chars=5000)
    stub = StubAsk({"answer": "Борщ", "used": []})
    result = asyncio.run(answer_question(settings, notes, "борщ", FIXED_NOW, llm=stub, index=NoteIndex(notes)))
    sent = stub.calls[0]["lines"]
    assert sum(len(x) + 1 for x in sent) <= 5000 and any("борща" in x for x in sent) and result.omitted > 0


def test_snapshot_stamp_changes_with_snapshot(tmp_path):
    from app.db import Database
    db = Database(tmp_path / "s.db")
    db.init()
    first = db.snapshot_stamp()
    db.snapshot_put([("a", {"type": "note", "title": "Х"})])
    assert db.snapshot_stamp() != first
