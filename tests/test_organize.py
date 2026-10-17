import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import vault_organize as org  # noqa: E402
import vault_sync  # noqa: E402


def note(title, type_="task", confidence=0.9, tags=(), people=(), related=(), stage="inbox", status="открыто",
         created="2026-10-10T10:00", body="Текст", extra=""):
    lines = ["---", "schema: 1", f'title: "{title}"', f'summary: "{title}"', f"created: {created}", f'type: "{type_}"']
    if status:
        lines.append(f'status: "{status}"')
    lines.append(f'stage: "{stage}"')
    for key, vals in (("tags", tags), ("people", [f"[[{p}]]" for p in people]), ("related", [f"[[{r}]]" for r in related])):
        if vals:
            lines.append(f"{key}:")
            lines += [f'  - "{v}"' for v in vals]
    lines.append(f"confidence: {confidence}")
    if extra:
        lines.append(extra)
    lines += ["---", "", body, ""]
    return "\n".join(lines)


def put(vault, rel, text):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


NOW = 10_000_000_000.0   # все файлы «давно записаны»


def run(vault, **kw):
    return org.organize(vault, now=NOW, **kw)


def test_confident_note_is_filed_and_marked(tmp_path):
    p = put(tmp_path, "0-Inbox/a.md", note("Позвонить в УК"))
    rep = run(tmp_path)
    assert not p.exists()
    moved = tmp_path / "4-Дела" / "Дела и события" / "a.md"
    assert moved.exists() and 'stage: "filed"' in moved.read_text(encoding="utf-8")
    assert rep["filed"] == ["Позвонить в УК -> 4-Дела/Дела и события"] or rep["filed"][0].startswith("Позвонить в УК")


def test_doubtful_notes_stay_in_inbox(tmp_path):
    put(tmp_path, "0-Inbox/low.md", note("Неясное", confidence=0.5))
    put(tmp_path, "0-Inbox/chk.md", note("Проверить", tags=["проверить"]))
    put(tmp_path, "0-Inbox/plain.md", note("Просто заметка", type_="note", status=""))
    put(tmp_path, "0-Inbox/pers.md", note("Серик", type_="person", status="актуален"))
    rep = run(tmp_path)
    for name in ("low", "chk", "plain", "pers"):
        assert (tmp_path / "0-Inbox" / f"{name}.md").exists()
    assert len(rep["kept"]) == 3 or len(rep["kept"]) == 4
    assert 'stage: "inbox"' in (tmp_path / "0-Inbox/low.md").read_text(encoding="utf-8")


def test_links_between_notes_by_people_and_words(tmp_path):
    old = put(tmp_path, "2-Знания/Медиа/2026-10-01-dune-ab12.md",
              note("Дюна: часть вторая", "media", people=["Серик"], stage="processed", status="хочу",
                   tags=["кино"], body="Фильм Дени Вильнёва про планету Арракис"))
    new = put(tmp_path, "0-Inbox/new.md", note("Позвать Серика на фильм", "task", people=["Серик"], tags=["кино"],
                                                body="Позвать Серика на Дюну в кино"))
    run(tmp_path)
    moved = tmp_path / "4-Дела/Дела и события/new.md"
    text = moved.read_text(encoding="utf-8")
    assert "<!-- sb:links -->" in text and "## Связано" in text
    assert "[[2026-10-01-dune-ab12|Дюна: часть вторая]]" in text      # ссылка по имени ФАЙЛА, а не названию
    assert "люди: серик" in text
    assert old.read_text(encoding="utf-8").count("sb:links") == 0       # старые не трогаем


def test_unrelated_notes_do_not_get_links(tmp_path):
    put(tmp_path, "2-Знания/Идеи и выводы/x.md", note("Рецепт борща", "learning", stage="processed", status="",
                                                        body="свёкла капуста мясо"))
    put(tmp_path, "0-Inbox/y.md", note("Заменить фильтр", "home", status="открыто", body="фильтр воды раз в три месяца"))
    run(tmp_path)
    assert "sb:links" not in (tmp_path / "5-Дом и жизнь/Дом/y.md").read_text(encoding="utf-8")


def test_idempotent_and_block_replaced_not_duplicated(tmp_path):
    put(tmp_path, "2-Знания/Медиа/m.md", note("Дюна", "media", people=["Серик"], stage="processed", status="хочу"))
    put(tmp_path, "0-Inbox/n.md", note("Серик и Дюна", "idea", people=["Серик"], status="сырая"))
    run(tmp_path)
    path = tmp_path / "2-Знания/Идеи и выводы/n.md"
    first = path.read_text(encoding="utf-8")
    rep = run(tmp_path)                           # уже filed: блок пересчитывается, но идентичен
    assert path.read_text(encoding="utf-8") == first
    assert first.count("<!-- sb:links -->") == 1 and not rep["linked"]


def test_dry_run_changes_nothing(tmp_path):
    p = put(tmp_path, "0-Inbox/a.md", note("Позвонить"))
    before = p.read_text(encoding="utf-8")
    rep = run(tmp_path, dry_run=True)
    assert p.read_text(encoding="utf-8") == before and rep["filed"]


def test_recent_file_is_not_touched_until_settled(tmp_path):
    p = put(tmp_path, "0-Inbox/a.md", note("Позвонить"))
    rep = org.organize(tmp_path, now=p.stat().st_mtime + 1)
    assert p.exists() and not rep["filed"]
    rep = org.organize(tmp_path, now=p.stat().st_mtime + 1, settle=0)
    assert rep["filed"]


def test_private_notes_are_not_linked_or_targets(tmp_path):
    put(tmp_path, "2-Знания/Идеи и выводы/p.md", note("Секрет Серика", "idea", people=["Серик"], stage="processed",
                                                     status="сырая", extra="private: true"))
    put(tmp_path, "0-Inbox/n.md", note("Серик", "idea", people=["Серик"], status="сырая"))
    run(tmp_path)
    assert "Секрет" not in (tmp_path / "2-Знания/Идеи и выводы/n.md").read_text(encoding="utf-8")


def test_learns_users_own_folders(tmp_path):
    for i in range(3):
        put(tmp_path, f"Мои дела/t{i}.md", note(f"Старое дело {i}", "task", stage="processed"))
    put(tmp_path, "0-Inbox/n.md", note("Новое дело"))
    run(tmp_path)
    assert (tmp_path / "Мои дела/n.md").exists()


def test_two_notes_with_same_filename_do_not_clobber(tmp_path):
    put(tmp_path, "4-Дела/Дела и события/a.md", note("Другое", stage="processed"))
    put(tmp_path, "0-Inbox/a.md", note("Новое"))
    run(tmp_path)
    assert (tmp_path / "4-Дела/Дела и события/a.md").exists() and (tmp_path / "4-Дела/Дела и события/a-2.md").exists()


def test_closed_filed_task_goes_to_archive_by_year(tmp_path):
    put(tmp_path, "4-Дела/Дела и события/z.md", note("Сделано", stage="filed", status="сделано", created="2026-03-01T10:00"))
    put(tmp_path, "4-Дела/Дела и события/own.md", note("Моё закрытое", stage="processed", status="сделано"))
    run(tmp_path)
    assert (tmp_path / "3-Архив/2026/z.md").exists()
    assert (tmp_path / "4-Дела/Дела и события/own.md").exists()           # чужую раскладку не трогаем


def test_journal_by_month_and_crlf_preserved(tmp_path):
    text = note("Вечер", "journal", status="", created="2026-10-14T21:00").replace("\n", "\r\n")
    path = tmp_path / "0-Inbox" / "j.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode("utf-8"))
    run(tmp_path)
    moved = tmp_path / "6-Дневник/2026-10/j.md"
    assert moved.exists() and b"\r\n" in moved.read_bytes() and b'stage: "filed"' in moved.read_bytes()


def test_all_mode_writes_health_report(tmp_path):
    (tmp_path / "Дашборды").mkdir()
    put(tmp_path, "2-Знания/Идеи и выводы/o.md", note("Одинокая", "idea", stage="processed", status="сырая",
                                                      related=["Несуществующее"]))
    put(tmp_path, "2-Знания/Идеи и выводы/s.md", note("Сирота", "idea", stage="processed", status="сырая"))
    run(tmp_path, refresh_all=True)
    report = (tmp_path / "Дашборды/Здоровье связей.md").read_text(encoding="utf-8")
    assert "Сирота" in report and "Несуществующее" in report
    assert (tmp_path / "Дашборды/Разложено автоматически.md").exists()


def test_sync_calls_organizer_and_survives_its_failure(tmp_path, monkeypatch):
    put(tmp_path, "0-Inbox/a.md", note("Позвонить"))
    vault_sync.organize_after_sync(tmp_path, fresh=True)
    assert (tmp_path / "4-Дела/Дела и события/a.md").exists()
    monkeypatch.setattr(org, "organize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    vault_sync.organize_after_sync(tmp_path)          # не падает


def test_organizer_is_stdlib_only():
    import ast
    tree = ast.parse(Path(org.__file__).read_text(encoding="utf-8"))
    mods = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (getattr(n, "names", None) or [n])}
    assert mods <= {"__future__", "argparse", "json", "math", "os", "re", "sys", "time", "collections", "dataclasses",
                    "datetime", "pathlib"}


def test_empty_target_folders_are_created_up_front(tmp_path):
    rep = run(tmp_path)
    for name in ("4-Дела/Дела и события", "2-Знания/Медиа", "5-Дом и жизнь/Дом", "6-Дневник", "7-Исходники", "0-Inbox"):
        assert (tmp_path / name).is_dir(), name
    assert "7-Исходники" in rep["folders"]
    assert run(tmp_path)["folders"] == []                # повторно ничего не создаёт
