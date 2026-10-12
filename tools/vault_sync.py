#!/usr/bin/env python3
"""Забирает заметки из очереди (например, присланные через Telegram) и кладёт их в хранилище.

Запускается на ноутбуке. Только стандартная библиотека Python 3.9+.

    python vault_sync.py --url https://ТВОЙ-АДРЕС --vault "C:\\путь\\к\\SecondBrain"          # один раз
    python vault_sync.py --url https://ТВОЙ-АДРЕС --vault "C:\\путь\\к\\SecondBrain" --loop 300  # каждые 5 минут

Токен берётся из переменной окружения SECOND_BRAIN_TOKEN (или параметра --token).
Файл сначала записывается на диск и только потом подтверждается на сервере, поэтому заметка не потеряется.
Существующие файлы не перезаписываются.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def api(url: str, token: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url.rstrip("/") + path, data=data, method="POST" if body is not None else "GET",
        headers={"x-api-key": token, "content-type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_part(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip().strip(".")
    return name or "zametka.md"


def write_note(vault: Path, folder: str, filename: str, markdown: str) -> Path | None:
    """Пишет файл, не затирая существующий. Возвращает путь или None, если такой же уже лежит."""
    target_dir = vault / safe_part(folder or "0-Inbox")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = safe_part(filename)
    stem, suffix = (name[:-3], ".md") if name.lower().endswith(".md") else (name, ".md")
    path = target_dir / f"{stem}{suffix}"
    counter = 2
    while path.exists():
        if path.read_text(encoding="utf-8") == markdown:
            return None                      # уже синхронизировано раньше
        path = target_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    path.write_text(markdown, encoding="utf-8", newline="\n")
    return path


def sync_once(url: str, token: str, vault: Path) -> int:
    saved = 0
    while True:
        batch = api(url, token, "/pending?limit=50")
        items = batch.get("items", [])
        if not items:
            return saved
        done = []
        for item in items:
            path = write_note(vault, item.get("folder", ""), item["filename"], item["markdown"])
            if path:
                saved += 1
                print(f"  + {path.relative_to(vault)}")
            done.append(item["id"])           # дошли сюда — файл на диске (или уже был)
        api(url, token, "/ack", {"ids": done})
        if batch.get("remaining", 0) <= len(items):
            return saved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Забрать заметки из очереди в хранилище.")
    parser.add_argument("--url", required=True, help="адрес сервиса, например https://xxx.up.railway.app")
    parser.add_argument("--vault", required=True, help="папка хранилища SecondBrain")
    parser.add_argument("--token", default=os.environ.get("SECOND_BRAIN_TOKEN", ""))
    parser.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 — один раз)")
    args = parser.parse_args(argv)
    if not args.token:
        print("Не задан токен: переменная SECOND_BRAIN_TOKEN или параметр --token")
        return 1
    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"Папка не найдена: {vault}")
        return 1
    while True:
        try:
            saved = sync_once(args.url, args.token, vault)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] новых заметок: {saved}")
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(f"Не получилось связаться с сервисом: {type(exc).__name__}. Попробую позже.")
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    sys.exit(main())
