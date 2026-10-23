import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vault_people as vp  # noqa: E402

from app.service import normalize_note  # noqa: E402
from app.llm import LLMNote  # noqa: E402
from app.textutil import split_person_title  # noqa: E402
from datetime import datetime  # noqa: E402


def person(title, extra="", body="Текст"):
    return f'---\nschema: 1\ntitle: "{title}"\ntype: "person"\nstatus: "актуален"\n{extra}---\n\n{body}\n'


def put(vault, name, text):
    path = vault / "1-Люди" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_split_title():
    assert vp.split_title("Ануар — друг, работает в комитете") == ("Ануар", "друг, работает в комитете")
    assert vp.split_title("Анна-Мария Иванова") == ("Анна-Мария Иванова", "")
    assert split_person_title("Никита - друг из Томска") == ("Никита", "друг из Томска")


def test_tidy_merges_card_and_described_note(tmp_path):
    put(tmp_path, "Ануар.md", person("Ануар", body="# Ануар\n\n```dataview\nTABLE x\n```\n"))
    put(tmp_path, "Ануар — друг, работает в комитете.md",
        person("Ануар — друг, работает в комитете", 'phones:\n  - "+7 700 000 00 00"\n', body="Познакомились в Томске"))
    plan = vp.tidy(tmp_path)                                         # без apply ничего не меняется
    assert plan["merged"] == ["Ануар — друг, работает в комитете -> Ануар"]
    assert (tmp_path / "1-Люди/Ануар — друг, работает в комитете.md").exists()
    vp.tidy(tmp_path, apply=True)
    card = (tmp_path / "1-Люди/Ануар.md").read_text(encoding="utf-8")
    assert "+7 700 000 00 00" in card and 'relation: "друг"' in card and "работает в комитете" in card
    assert "Познакомились в Томске" in card
    assert not (tmp_path / "1-Люди/Ануар — друг, работает в комитете.md").exists()
    assert (tmp_path / "3-Архив/Люди-дубли/Ануар — друг, работает в комитете.md").exists()     # не удалён
    assert vp.tidy(tmp_path)["merged"] == []                                                    # повторно пусто


def test_single_described_note_becomes_card(tmp_path):
    put(tmp_path, "Алия Аблаевна — тёща.md", person("Алия Аблаевна — тёща"))
    vp.tidy(tmp_path, apply=True)
    card = (tmp_path / "1-Люди/Алия Аблаевна.md").read_text(encoding="utf-8")
    assert 'title: "Алия Аблаевна"' in card and 'relation: "тёща"' in card
    assert not (tmp_path / "1-Люди/Алия Аблаевна — тёща.md").exists()


def test_profile_suffix_does_not_become_a_relation(tmp_path):
    put(tmp_path, "Владислав Маскевич.md", person("Владислав Маскевич"))
    put(tmp_path, "Владислав Маскевич — профиль.md", person("Владислав Маскевич — профиль", 'city: "Астана"\n'))
    vp.tidy(tmp_path, apply=True)
    card = (tmp_path / "1-Люди/Владислав Маскевич.md").read_text(encoding="utf-8")
    assert 'city: "Астана"' in card and "relation" not in card


def test_filled_fields_are_never_overwritten_and_similar_names_only_reported(tmp_path):
    put(tmp_path, "Елена Маскевич.md", person("Елена Маскевич", 'relation: "мама"\n'))
    put(tmp_path, "Елена Маскевич — тётя.md", person("Елена Маскевич — тётя"))
    put(tmp_path, "Владимир Маскевич.md", person("Владимир Маскевич"))
    put(tmp_path, "Владимир Маскевия — отец.md", person("Владимир Маскевия — отец"))
    plan = vp.tidy(tmp_path, apply=True)
    assert 'relation: "мама"' in (tmp_path / "1-Люди/Елена Маскевич.md").read_text(encoding="utf-8")
    assert any("Владимир Маскевич" in row and "Владимир Маскевия" in row for row in plan["similar"])
    assert (tmp_path / "1-Люди/Владимир Маскевия.md").exists()        # опечатки сами не склеиваем


def test_non_person_notes_in_people_folder_are_only_listed(tmp_path):
    put(tmp_path, "Распорядок дня и привычки.md", '---\nschema: 1\ntitle: "Распорядок"\ntype: "learning"\n---\n\nТекст\n')
    plan = vp.tidy(tmp_path, apply=True)
    assert plan["foreign"] == ["Распорядок дня и привычки.md"]
    assert (tmp_path / "1-Люди/Распорядок дня и привычки.md").exists()


def test_collect_does_not_create_phantom_cards_for_described_titles(tmp_path):
    put(tmp_path, "Никита — друг из Томска.md", person("Никита — друг из Томска"))
    names, _, _ = vp.collect(tmp_path)
    assert "Никита" in names and not any("—" in n for n in names)


def test_server_person_title_is_name_only():
    raw = LLMNote(type="person", title="Асхат — друг, работает в BI Group", summary="s", body="b", confidence=0.9)
    note = normalize_note(raw, content_urls=[], links={}, client="telegram", capture_id="c" * 10,
                          now=datetime(2026, 10, 17, 7, 0), fallback_body="x")
    assert note.title == "Асхат"
    md = note.markdown()
    assert 'title: "Асхат"' in md and "работает в BI Group" in md


def test_tidy_merges_phone_label_card_into_base_person(tmp_path):
    put(tmp_path, "Eichholtz.md", person("Eichholtz", 'phones:\n  - "+7 700 111 11 11"\n'))
    put(tmp_path, "Eichholtz Рабочий.md", person("Eichholtz Рабочий", 'phones:\n  - "+7 700 222 22 22"\n', body="Это рабочий номер"))
    plan = vp.tidy(tmp_path)
    assert plan["merged"] == ["Eichholtz Рабочий -> Eichholtz"] and not plan["homonyms"]
    vp.tidy(tmp_path, apply=True)
    card = (tmp_path / "1-Люди/Eichholtz.md").read_text(encoding="utf-8")
    assert "+7 700 222 22 22" in card and "+7 700 111 11 11" in card and "Рабочий" in card


def test_tidy_keeps_lone_label_card(tmp_path):
    put(tmp_path, "Иван Рабочий.md", person("Иван Рабочий", 'phones:\n  - "+7 700 111 11 11"\n'))
    plan = vp.tidy(tmp_path, apply=True)
    assert plan["merged"] == [] and (tmp_path / "1-Люди/Иван Рабочий.md").exists()


def test_tidy_merges_reordered_names(tmp_path):
    put(tmp_path, "Маскевич Владислав.md", person("Маскевич Владислав", 'phones:\n  - "+7 700 111 11 11"\n'))
    put(tmp_path, "Владислав Маскевич.md", person("Владислав Маскевич", 'company: "Банк"\n'))
    plan = vp.tidy(tmp_path)
    assert len(plan["merged"]) == 1
    vp.tidy(tmp_path, apply=True)
    left = list((tmp_path / "1-Люди").glob("*.md"))
    assert len(left) == 1
    text = left[0].read_text(encoding="utf-8")
    assert "+7 700 111 11 11" in text and "Банк" in text


def test_tidy_merges_short_name_into_unique_full_name(tmp_path):
    put(tmp_path, "Серик.md", person("Серик", 'relation: "друг"\n'))
    put(tmp_path, "Серик Нурланов.md", person("Серик Нурланов", 'phones:\n  - "+7 701 000 00 00"\n'))
    assert len(vp.tidy(tmp_path)["merged"]) == 1
    vp.tidy(tmp_path, apply=True)
    assert [p.name for p in (tmp_path / "1-Люди").glob("*.md")] == ["Серик Нурланов.md"]


def test_tidy_does_not_merge_short_name_when_two_full_names(tmp_path):
    put(tmp_path, "Серик.md", person("Серик"))
    put(tmp_path, "Серик Нурланов.md", person("Серик Нурланов"))
    put(tmp_path, "Серик Ахметов.md", person("Серик Ахметов"))
    assert vp.tidy(tmp_path)["merged"] == []


def test_tidy_does_not_merge_short_name_with_conflicting_phone(tmp_path):
    put(tmp_path, "Серик.md", person("Серик", 'phones:\n  - "+7 700 111 11 11"\n'))
    put(tmp_path, "Серик Нурланов.md", person("Серик Нурланов", 'phones:\n  - "+7 701 000 00 00"\n'))
    assert vp.tidy(tmp_path)["merged"] == []


def test_run_folds_label_and_reordered_names_into_existing_card(tmp_path):
    put(tmp_path, "Eichholtz.md", person("Eichholtz"))
    inbox = tmp_path / "0-Inbox"
    inbox.mkdir()
    (inbox / "a.md").write_text(person("Eichholtz Рабочий", 'phones:\n  - "+7 700 222 22 22"\n'), encoding="utf-8")
    (inbox / "b.md").write_text(person("Eichholtz Рабочий"), encoding="utf-8")
    report = vp.run(tmp_path)
    assert report["created"] == []                                      # «Eichholtz Рабочий» новой карточкой не стала
    assert "+7 700 222 22 22" in (tmp_path / "1-Люди/Eichholtz.md").read_text(encoding="utf-8")
