#!/usr/bin/env python3
"""Архив заметок хранилища, чтобы прикрепить его в чат с Claude. Ничего не отправляет в сеть.

В архив попадают только .md заметки. Не попадают: приватные заметки (`private: true`, «!» в начале), папки 7-Исходники,
Шаблоны, .obsidian, .trash, вложения, слишком большие файлы. В тексте закрываются длинные номера (12 цифр и больше):
ИИН, счета, карты. Телефоны остаются.

    python export_for_claude.py "C:\\путь\\к\\SecondBrain" [--out папка_для_архива]
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {".obsidian", ".trash", "7-Исходники", "Шаблоны", ".git"}
MAX_FILE_BYTES = 200_000
LONG_NUMBER = re.compile(r"(?<![\d+])(?:\d[ -]?){12,}(?!\d)")
PRIVATE_FRONT = re.compile(r"(?mi)^private:\s*[\"']?(true|yes|да|1)[\"']?\s*$")


def is_private(text: str) -> bool:
    head = text[:3000]
    if PRIVATE_FRONT.search(head):
        return True
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).lstrip()
    return body.startswith("!")


def redact(text: str) -> str:
    return LONG_NUMBER.sub("[номер скрыт]", text)


def collect(vault: Path) -> tuple[list[tuple[Path, str]], dict[str, int]]:
    files: list[tuple[Path, str]] = []
    skipped = {"приватных": 0, "слишком больших": 0, "нечитаемых": 0}
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped["слишком больших"] += 1
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["нечитаемых"] += 1
            continue
        if is_private(text):
            skipped["приватных"] += 1
            continue
        files.append((rel, redact(text)))
    return files, skipped


def export(vault: Path, out_dir: Path, now: datetime | None = None) -> tuple[Path, int, dict[str, int]]:
    now = now or datetime.now()
    files, skipped = collect(vault)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"SecondBrain-export-{now:%Y-%m-%d}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, text in files:
            archive.writestr(rel.as_posix(), text)
    return target, len(files), skipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Архив заметок для прикрепления в чат с Claude.")
    parser.add_argument("vault")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"Папка не найдена: {vault}")
        return 1
    out = Path(args.out) if args.out else (Path.home() / "Desktop" if (Path.home() / "Desktop").is_dir() else vault.parent)
    target, count, skipped = export(vault, out)
    size = target.stat().st_size / 1_048_576
    print(f"Готово: {target}")
    print(f"Заметок в архиве: {count}, размер {size:.1f} МБ.")
    print("Пропущено: " + ", ".join(f"{k} {v}" for k, v in skipped.items() if v) if any(skipped.values()) else "Пропущенных нет.")
    if size > 25:
        print("Архив больше 25 МБ: возможно, чат его не примет. Напишите, разделю по папкам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
