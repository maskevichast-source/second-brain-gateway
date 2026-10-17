"""Круги общения, радар, обещания и личные поля, которые не уходят на сервер."""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vault_sync  # noqa: E402

from app import review, snapshot  # noqa: E402
from app.vault import VaultNote  # noqa: E402


def note(**front):
    return VaultNote(index=1, front=front, body="")


TODAY = date(2026, 10, 17)


def test_radar_lists_overdue_people_by_orbit_and_cadence():
    notes = [
        note(title="Арман", type="person", orbit="1", last_contact="2026-09-01"),
        note(title="Серик", type="person", orbit="1", last_contact="2026-10-15"),
        note(title="Карина", type="person", cadence_days="10"),
        note(title="Без круга", type="person"),
        note(title="Звонок", type="interaction", created="2026-10-10T10:00", people=["Карина"]),
    ]
    text = review.build_radar(notes, TODAY)
    assert "Арман" in text and "Серик" not in text and "Без круга" not in text
    assert "Карина" not in text          # звонок 7 дней назад, срок 10
    assert "Радар чист" in review.build_radar([note(title="X", type="person")], TODAY)


def test_promises_split_by_direction_and_flag_overdue():
    notes = [
        note(title="Скинуть контакт оценщика", type="task", status="открыто", promise="я", people=["Арман"], due="2026-10-10"),
        note(title="Прислать смету", type="task", status="открыто", promise="мне", people=["Серик"], due="2026-10-25"),
        note(title="Закрытое", type="task", status="сделано", promise="я"),
        note(title="Обычное дело", type="task", status="открыто"),
    ]
    text = review.build_promises(notes, TODAY)
    assert "🔴 Я обещал" in text and "🟢 Мне обещали" in text
    assert "Арман" in text and "просрочено" in text
    assert "Закрытое" not in text and "Обычное" not in text


def test_brief_shows_orbit_family_and_promise_marks():
    notes = [
        note(title="Арман", type="person", orbit="1", partner="Алексей", children=["Мира"]),
        note(title="Контакт", type="task", status="открыто", promise="я", people=["Арман"]),
    ]
    text = review.build_brief(notes, "Арман", datetime(2026, 10, 17, 12))
    assert "круг: ближний" in text and "партнёр: Алексей" in text and "дети: Мира" in text and "я обещал" in text


def test_private_card_fields_never_reach_server_snapshot(tmp_path):
    folder = tmp_path / "1-Люди"
    folder.mkdir()
    (folder / "Арман.md").write_text(
        '---\nschema: 1\ntitle: "Арман"\ntype: "person"\norbit: 1\ntriggers:\n  - "срыв раунда"\n'
        'taboo_topics: ["развод"]\npersuasion_style: "цифры"\nrelation: "друг"\n---\nТекст\n', encoding="utf-8")
    corpus = vault_sync.build_snapshot_corpus(tmp_path)
    assert "орбита" not in corpus and "orbit: 1" in corpus and 'relation: "друг"' in corpus
    for secret in ("срыв раунда", "развод", "цифры", "triggers", "taboo_topics", "persuasion_style"):
        assert secret not in corpus
    kept = snapshot.row_from_note(note(title="Арман", type="person", triggers=["х"], persuasion_style="у", orbit="1"))[1]
    assert "triggers" not in kept and "persuasion_style" not in kept and kept["orbit"] == "1"
