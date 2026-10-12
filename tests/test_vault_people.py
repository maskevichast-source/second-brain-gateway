import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vault_people as vp  # noqa: E402

from .conftest import vault_note  # noqa: E402

NOW = datetime(2026, 10, 12, 23, 30)


def make_vault(tmp_path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_creates_cards_for_mentioned_people_and_is_idempotent(tmp_path):
    vault = make_vault(tmp_path, {
        "0-Inbox/a.md": vault_note("Позвонить", type="task", people=["[[Серик]]", "[[Аля]]"]),
        "0-Inbox/b.md": vault_note("Фильм", type="media", people=["[[Серик]]"]),
    })
    report = vp.run(vault, now=NOW)
    assert sorted(report["created"]) == ["Аля", "Серик"]
    card = (vault / "1-Люди" / "Серик.md").read_text(encoding="utf-8")
    assert card.startswith('---\nschema: 1\ntitle: "Серик"\n') and 'type: "person"' in card
    assert "this.file.link" in card and "## Заметки от руки" in card
    again = vp.run(vault, now=NOW)
    assert again == {"created": [], "updated": []}


def test_person_notes_are_merged_without_overwriting(tmp_path):
    vault = make_vault(tmp_path, {
        "0-Inbox/p.md": vault_note("Серик", type="person", phones=["8 707 000 00 00"], company='"Рога"',
                                   birthday="1990-03-05", email='"s@example.com"'),
        "1-Люди/Серик.md": '---\nschema: 1\ntitle: "Серик"\ntype: "person"\ncompany: "Копыта"\nphones: []\nemail:\n---\n# Серик\n\nМои заметки\n',
    })
    report = vp.run(vault, now=NOW)
    assert report["created"] == [] and "Серик" in report["updated"][0]
    text = (vault / "1-Люди" / "Серик.md").read_text(encoding="utf-8")
    assert 'company: "Копыта"' in text and "Рога" not in text          # заполненное не затёрто
    assert 'phones:\n  - "8 707 000 00 00"' in text and 'email: "s@example.com"' in text
    assert 'birthday: "1990-03-05"' in text
    assert text.endswith("# Серик\n\nМои заметки\n")                      # тело не тронуто
    assert vp.run(vault, now=NOW)["updated"] == []


def test_lists_are_extended_not_replaced(tmp_path):
    vault = make_vault(tmp_path, {
        "0-Inbox/p.md": vault_note("Аля", type="person", phones=["111", "222"]),
        "1-Люди/Аля.md": '---\ntitle: "Аля"\ntype: "person"\nphones:\n  - "111"\n---\nтело\n',
    })
    vp.run(vault, now=NOW)
    text = (vault / "1-Люди" / "Аля.md").read_text(encoding="utf-8")
    assert text.count('"111"') == 1 and '"222"' in text


def test_dry_run_writes_nothing_and_templates_are_skipped(tmp_path):
    vault = make_vault(tmp_path, {
        "0-Inbox/a.md": vault_note("Дело", type="task", people=["[[Серик]]"]),
        "Шаблоны/Дело.md": vault_note("Шаблон", type="task", people=["[[ИмяИзШаблона]]"]),
        "3-Архив/old.md": vault_note("Старое", type="task", people=["[[Архивный]]"]),
    })
    report = vp.run(vault, dry_run=True, now=NOW)
    assert report["created"] == ["Серик"]
    assert not (vault / "1-Люди").exists()


def test_odd_names_and_obsidian_style_yaml(tmp_path):
    vault = make_vault(tmp_path, {
        "0-Inbox/a.md": "---\ntype: task\npeople:\n- \"[[Иван: Петров/мл]]\"\n- '[[Мария|Маша]]'\n---\nтекст\n",
        "0-Inbox/b.md": "---\ntype: task\npeople: [\"[[Олег]]\", \"[[Нина]]\"]\n---\nтекст\n",
        "0-Inbox/broken.md": "---\nэто: [не закрыто\n",
    })
    report = vp.run(vault, now=NOW)
    assert sorted(report["created"]) == ["Иван Петров мл", "Мария", "Нина", "Олег"]


def test_build_profile_collects_context_and_people(tmp_path):
    vault = make_vault(tmp_path, {
        "Профиль.md": "---\ntitle: x\n---\n# Профиль\n<!-- подсказка -->\n## Семья\n- Диана — жена\n- …\n\n## Слова\n- УК — Damo Group\n",
        "1-Люди/Серик.md": '---\ntitle: "Серик"\ntype: "person"\nrelation: "друг"\naliases:\n  - "Серёга"\n---\n# Серик\n',
        "1-Люди/Аля.md": '---\ntitle: "Аля"\ntype: "person"\nrelation: []\n---\n',
    })
    payload = vp.build_profile(vault)
    assert "Диана — жена" in payload["context"] and "подсказка" not in payload["context"] and "- …" not in payload["context"]
    assert payload["people"] == [{"name": "Аля", "role": "", "aliases": []},
                                 {"name": "Серик", "role": "друг", "aliases": ["Серёга"]}]
    assert vp.build_profile(tmp_path / "нет")["context"] == ""


def test_push_option_posts_profile(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, {"Профиль.md": "# П\nтекст\n"})
    sent = {}

    def fake_push(url, token, payload):
        sent.update(url=url, token=token, payload=payload)
        return {"people": 0, "context_chars": len(payload["context"])}

    monkeypatch.setattr(vp, "push_profile", fake_push)
    assert vp.main([str(vault), "--push", "https://x.example", "--token", "T"]) == 0
    assert sent["url"] == "https://x.example" and sent["token"] == "T" and "текст" in sent["payload"]["context"]
    assert vp.main([str(vault), "--push", "https://x.example", "--token", ""]) == 1
