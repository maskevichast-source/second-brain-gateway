import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import export_for_claude as ex  # noqa: E402


def put(vault, rel, text):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_export_skips_private_sources_and_hides_long_numbers(tmp_path):
    v = tmp_path / "vault"
    put(v, "1-Люди/Серик.md", '---\ntitle: "Серик"\nphones:\n  - "+7 701 000 00 00"\n---\nИИН 123456789012 и карта 4000 1234 5678 9010\n')
    put(v, "0-Inbox/секрет.md", '---\ntitle: "х"\nprivate: true\n---\nпароль\n')
    put(v, "0-Inbox/восклицание.md", "! личное\n")
    put(v, "7-Исходники/чек.md", "чек\n")
    put(v, ".obsidian/x.md", "настройки\n")
    put(v, "картинка.png", "x")
    target, count, skipped = ex.export(v, tmp_path / "out", datetime(2026, 10, 23))
    assert count == 1 and skipped["приватных"] == 2
    with zipfile.ZipFile(target) as z:
        assert z.namelist() == ["1-Люди/Серик.md"]
        text = z.read("1-Люди/Серик.md").decode("utf-8")
    assert "+7 701 000 00 00" in text and "123456789012" not in text and "4000 1234" not in text and "[номер скрыт]" in text


def test_export_skips_huge_files(tmp_path):
    v = tmp_path / "v"
    put(v, "a.md", "x" * (ex.MAX_FILE_BYTES + 1))
    put(v, "b.md", "ok")
    _, count, skipped = ex.export(v, tmp_path / "o")
    assert count == 1 and skipped["слишком больших"] == 1
