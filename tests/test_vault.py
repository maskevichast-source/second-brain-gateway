from app.vault import SEPARATOR, compact_line, keywords, load_notes, parse_note, score, split_corpus, strip_original

from .conftest import vault_note


def test_split_by_marker_and_by_properties():
    a, b = vault_note("Первая"), vault_note("Вторая")
    assert len(split_corpus(f"{a}\n{SEPARATOR}\n{b}")) == 2
    assert len(split_corpus(f"{a}\n{b}")) == 2          # без маркера делим по началу свойств
    assert split_corpus("   ") == []
    assert len(split_corpus("просто текст без свойств")) == 1


def test_parse_note_reads_dates_lists_and_links():
    raw = vault_note("Дюна 2", type="media", status='"хочу"', created="2026-10-12T09:30",
                     people=["[[Серик]]"], finished_at="2026-10-01")
    note = parse_note(raw, 1)
    assert note.title == "Дюна 2" and note.type == "media"
    assert note.get("created") == "2026-10-12T09:30"
    assert note.get("finished_at") == "2026-10-01"
    assert note.people == ["Серик"]
    assert "Дюна 2" in note.body


def test_broken_or_missing_yaml_does_not_crash():
    broken = parse_note("---\ntitle: [не закрыто\n---\nТекст заметки", 3)
    assert broken.front == {} and broken.title == "Текст заметки"
    assert parse_note("Без свойств", 4).title == "Без свойств"
    assert parse_note("---\nнет закрытия", 5).front == {}


def test_private_notes_are_dropped_before_anything_else():
    notes, private = load_notes(vault_note("Обычная") + "\n" + vault_note("Пароль 4321", private=True))
    assert private == 1 and [n.title for n in notes] == ["Обычная"]


def test_compact_line_has_the_useful_bits_and_no_original_block():
    raw = vault_note("Дюна 2", type="media", kind='"фильм"', status='"хочу"', area='"отдых"',
                     created="2026-10-12T09:30", summary='"Фильм, посоветовал Серик"',
                     people=["[[Серик]]"], tags=["кино"])
    raw += "\n> [!quote]- Оригинал\n> секретный оригинал\n"
    line = compact_line(parse_note(raw, 7))
    for piece in ("[7] media/фильм", "статус: хочу", "сфера: отдых", "записано: 2026-10-12T09:30",
                  "«Дюна 2»", "люди: Серик", "теги: кино"):
        assert piece in line, piece
    assert "секретный оригинал" not in line
    assert "\n" not in line
    assert strip_original("Текст\n\n> [!quote]- Оригинал\n> x") == "Текст"


def test_keywords_and_score():
    assert "филь" in "".join(keywords("какие фильмы планировал на вечер?"))
    assert "планировал" not in keywords("планировал фильмы")
    note = parse_note(vault_note("Дюна", type="media", kind='"фильм"'), 1)
    assert score(note, keywords("фильмы")) > score(note, keywords("ремонт"))


def test_files_without_type_are_not_notes():
    start = "# Второй Мозг\n\nСтартовая страница\n"
    template = "---\nschema: 1\ncreated: {{date:YYYY-MM-DD}}\ntype: \"media\"\n---\n"
    notes, private = load_notes("\n=====NOTE=====\n".join([start, template, vault_note("Настоящая")]))
    assert [n.title for n in notes] == ["Настоящая"] and private == 0
