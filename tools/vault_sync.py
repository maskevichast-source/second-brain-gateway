#!/usr/bin/env python3
"""Забирает заметки из очереди (например, присланные через Telegram) и кладёт их в хранилище.

Запускается на ноутбуке. Только стандартная библиотека Python 3.9+.

    python vault_sync.py --url https://ТВОЙ-АДРЕС --vault "C:\\путь\\к\\SecondBrain"          # один раз
    python vault_sync.py --url https://ТВОЙ-АДРЕС --vault "C:\\путь\\к\\SecondBrain" --loop 300  # каждые 5 минут

Токен берётся из переменной окружения SECOND_BRAIN_TOKEN (или параметра --token).
Файл сначала записывается на диск и только потом подтверждается на сервере, поэтому заметка не потеряется.
Существующие файлы не перезаписываются.

После каждой выгрузки запускаются vault_organize.py (раскладка по папкам и связи между заметками) и vault_people.py
(карточки людей), если лежат рядом. Отключить: --no-organize.
Следом на сервер уходит срез заметок (только блоки свойств, без текстов и без приватных): так сводки и ответы бота
видят раскладку, закрытые дела и склеенных людей. Отключить: --no-snapshot.

Исходники (голос, PDF, фото, скриншоты) приходят записями kind=file: скрипт скачивает файл с сервера и кладёт в
7-Исходники/ГОД-МЕСЯЦ; пока файл не скачан, запись остаётся в очереди.

Кроме заметок, очередь содержит служебные записи «закрыть дело» (кнопка «Сделано» в Telegram): скрипт находит
заметку по названию (и дате создания) и меняет в ней status. Файл не удаляется; если заметка не найдена,
подходит несколько или статус уже закрыт, запись просто пропускается и подтверждается на сервере.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


def download(url: str, token: str, path: str) -> bytes:
    request = urllib.request.Request(url.rstrip("/") + path, headers={"x-api-key": token})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def safe_rel(folder: str, default: str = "7-Исходники") -> Path:
    """Папка внутри хранилища из двух частей максимум (7-Исходники/2026-10), без выхода за пределы."""
    parts = [safe_part(p) for p in folder.replace("\\", "/").split("/") if p.strip().strip(".")]
    return Path(*parts[:2]) if parts else Path(default)


def save_source(vault: Path, item: dict, data: bytes) -> Path | None:
    """Кладёт исходник в папку и не затирает существующий. None, если такой файл уже лежит."""
    target_dir = vault / safe_rel(item.get("folder") or "")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / safe_part(item["filename"])
    if path.exists():
        return None
    tmp = path.with_name(path.name + ".tmp-sync")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def safe_part(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip().strip(".")
    return name or "zametka.md"


def index_names(vault: Path) -> set[str]:
    """Имена всех .md в хранилище (после раскладки заметка лежит уже не в 0-Inbox): чтобы не записать её второй раз."""
    try:
        return {p.name for p in vault.rglob("*.md")}
    except OSError:
        return set()


def write_note(vault: Path, folder: str, filename: str, markdown: str, known: set[str] | None = None) -> Path | None:
    """Пишет файл, не затирая существующий. Возвращает путь или None, если такой же уже лежит."""
    target_dir = vault / safe_part(folder or "0-Inbox")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = safe_part(filename)
    stem, suffix = (name[:-3], ".md") if name.lower().endswith(".md") else (name, ".md")
    path = target_dir / f"{stem}{suffix}"
    if known is not None and path.name in known and not path.exists():
        return None                          # заметка уже была выгружена и разложена по папкам (/requeue, повтор после сбоя ack)
    counter = 2
    while path.exists():
        if path.read_text(encoding="utf-8") == markdown:
            return None                      # уже синхронизировано раньше
        path = target_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    path.write_text(markdown, encoding="utf-8", newline="\n")
    if known is not None:
        known.add(path.name)
    return path


CLOSED_STATUSES = {"сделано", "отменено", "готово", "состоялось", "отказался", "отменена", "завершена",
                   "достигнута", "отброшена", "расторгнут", "пересмотрено"}
_HEAD_BYTES = 16384


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    return value


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _read_text(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:     # newline="" сохраняет \r\n как есть
        return handle.read()


def _front_lines(text: str):
    """Индексы строк блока свойств: (список строк с \\n, номер закрывающей ---) или None."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines, i
    return None


def _front_value(lines, end: int, key: str):
    prefix = key + ":"
    for i in range(1, end):
        if lines[i].startswith(prefix):
            return i, _unquote(lines[i][len(prefix):])
    return None, ""


def find_notes(vault: Path, title: str, created: str = "") -> list:
    """Заметки, у которых title совпадает (и created, если он есть у обеих сторон)."""
    wanted, found = _norm(title), []
    for path in sorted(vault.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(vault).parts[:-1]):
            continue                                              # .obsidian, .trash и т.п.
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                head = handle.read(_HEAD_BYTES)
            if not head.startswith("---"):
                continue
            parsed = _front_lines(head)
            if not parsed:
                continue
            lines, end = parsed
            if _norm(_front_value(lines, end, "title")[1]) != wanted:
                continue
            found.append((path, _front_value(lines, end, "created")[1]))
        except (OSError, UnicodeDecodeError):
            continue
    if created and len(found) > 1:
        exact = [f for f in found if f[1][:16] == created[:16]]
        found = exact or found
    elif created and len(found) == 1 and found[0][1] and found[0][1][:16] != created[:16]:
        pass                                                        # единственная по названию: доверяем названию
    return [f[0] for f in found]


def close_note(vault: Path, payload: dict) -> str:
    """Применяет «закрыть дело». Возвращает: closed | already | not_found | ambiguous | invalid."""
    title = str(payload.get("title") or "").strip()
    status = str(payload.get("status") or "готово").strip()
    if not title or not status or "\n" in status:
        return "invalid"
    candidates = find_notes(vault, title, str(payload.get("created") or ""))
    if not candidates:
        return "not_found"
    if len(candidates) > 1:
        return "ambiguous"
    path = candidates[0]
    try:
        text = _read_text(path)
        parsed = _front_lines(text)
        if not parsed:
            return "not_found"
        lines, end = parsed
        index, current = _front_value(lines, end, "status")
        if _norm(current) in CLOSED_STATUSES:
            return "already"
        newline = "\r\n" if lines[end].endswith("\r\n") else "\n"
        row = "status: " + json.dumps(status, ensure_ascii=False) + newline
        if index is None:
            lines.insert(end, row)
        else:
            lines[index] = row
        tmp = path.with_name(path.name + ".tmp-sync")
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        os.replace(tmp, path)
    except (OSError, UnicodeDecodeError):
        return "invalid"
    return "closed"


ARCHIVE_DIR = ("3-Архив", "Удалённые-контакты")


def archive_note(vault: Path, payload: dict) -> str:
    """Карточка человека уходит в 3-Архив/Удалённые-контакты (не стирается). Возвращает: archived | not_found | ambiguous | not_person | invalid."""
    title = str(payload.get("title") or "").strip()
    if not title or str(payload.get("type") or "") != "person":
        return "invalid"
    archive = vault.joinpath(*ARCHIVE_DIR)
    candidates = [c for c in find_notes(vault, title, str(payload.get("created") or "")) if archive not in c.parents]
    if not candidates:
        return "not_found"
    if len(candidates) > 1:
        return "ambiguous"
    path = candidates[0]
    try:
        text = _read_text(path)
        parsed = _front_lines(text)
        if not parsed or _norm(_front_value(parsed[0], parsed[1], "type")[1]) != "person":
            return "not_person"
        target = vault.joinpath(*ARCHIVE_DIR)
        target.mkdir(parents=True, exist_ok=True)
        dest, n = target / path.name, 2
        while dest.exists():
            dest = target / f"{path.stem} ({n}){path.suffix}"
            n += 1
        os.replace(path, dest)
    except (OSError, UnicodeDecodeError):
        return "invalid"
    return "archived"


SETTABLE = {"status", "due", "remind_at", "next_due", "event_at", "deadline"}


def update_note(vault: Path, payload: dict) -> str:
    """Меняет свойства дела (перенос срока). Возвращает: updated | not_found | ambiguous | invalid."""
    title = str(payload.get("title") or "").strip()
    fields = payload.get("fields")
    if not title or not isinstance(fields, dict) or not fields or not set(fields) <= SETTABLE:
        return "invalid"
    if not all(isinstance(v, str) and v and "\n" not in v for v in fields.values()):
        return "invalid"
    candidates = find_notes(vault, title, str(payload.get("created") or ""))
    if not candidates:
        return "not_found"
    if len(candidates) > 1:
        return "ambiguous"
    path = candidates[0]
    try:
        text = _read_text(path)
        parsed = _front_lines(text)
        if not parsed:
            return "not_found"
        lines, end = parsed
        newline = "\r\n" if lines[end].endswith("\r\n") else "\n"
        for key, value in fields.items():
            index, _current = _front_value(lines, end, key)
            plain = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", value))
            row = f"{key}: " + (value if plain else json.dumps(value, ensure_ascii=False)) + newline
            if index is None:
                lines.insert(end, row)
                end += 1
            else:
                lines[index] = row
        tmp = path.with_name(path.name + ".tmp-sync")
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        os.replace(tmp, path)
    except (OSError, UnicodeDecodeError):
        return "invalid"
    return "updated"


def apply_service(vault: Path, item: dict) -> str:
    """Служебная запись из очереди. Неизвестные виды пропускаются (подтверждаются), чтобы очередь не стояла."""
    try:
        payload = json.loads(item.get("markdown") or "{}")
    except ValueError:
        return "invalid"
    if not isinstance(payload, dict):
        return "invalid"
    if payload.get("action") == "set":
        return update_note(vault, payload)
    if payload.get("action") == "archive":
        return archive_note(vault, payload)
    if payload.get("action") != "close":
        return "invalid"
    return close_note(vault, payload)


SERVICE_RU = {"closed": "дело закрыто", "updated": "срок перенесён", "already": "уже закрыто, пропускаю", "not_found": "заметка не найдена, пропускаю",
              "ambiguous": "подходит несколько заметок, пропускаю", "invalid": "непонятная запись, пропускаю",
              "archived": "карточка убрана в 3-Архив/Удалённые-контакты", "not_person": "это не карточка человека, не трогаю"}


SNAPSHOT_SKIP_DIRS = {"Шаблоны", "Дашборды", ".obsidian", ".trash", "7-Исходники"}
SNAPSHOT_SKIP_PATHS = ("3-Архив/Люди-дубли", "3-Архив/Удалённые-контакты")
SNAPSHOT_MAX_CHARS = 2_500_000
_last_snapshot_hash = ""


# Личные поля карточек: остаются только в Obsidian и на сервер не уходят.
PRIVATE_KEYS = ("triggers", "taboo_topics", "persuasion_style", "private_notes", "psychology")


def strip_private_keys(lines: list[str]) -> list[str]:
    """Убирает из блока свойств личные поля вместе с их многострочными значениями."""
    out, skipping = [], False
    for line in lines:
        key = line.split(":", 1)[0].strip().lower() if ":" in line and not line[:1].isspace() and not line.startswith("-") else ""
        if key:
            skipping = key in PRIVATE_KEYS
        elif skipping and not (line[:1].isspace() or line.lstrip().startswith("-")) and line.strip() not in ("", "---"):
            skipping = False
        if line.strip() == "---":
            skipping = False
        if not skipping:
            out.append(line)
    return out


def build_snapshot_corpus(vault: Path) -> str:
    """Только блоки свойств заметок (без текстов и без приватных): этого хватает срезу на сервере."""
    parts = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if rel.parts[0] in SNAPSHOT_SKIP_DIRS or any(p.startswith(".") for p in rel.parts) or len(rel.parts) == 1:
            continue
        if str(rel.parent).replace("\\", "/").startswith(SNAPSHOT_SKIP_PATHS):
            continue
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                head = handle.read(_HEAD_BYTES)
        except (OSError, UnicodeDecodeError):
            continue
        parsed = _front_lines(head)
        if not parsed or not head.startswith("---"):
            continue
        lines, end = parsed
        if any(re.match(r"^\s*private:\s*[\"']?(true|yes|1|да)[\"']?\s*(#.*)?$", row, re.I) for row in lines[1:end]):
            continue                            # private: true, "true", yes, да: любая запись приватности
        parts.append("".join(strip_private_keys(lines[: end + 1])).replace("\r\n", "\n"))
    corpus = "\n=====NOTE=====\n".join(parts)
    return corpus if len(corpus) <= SNAPSHOT_MAX_CHARS else ""


def push_snapshot(url: str, token: str, vault: Path) -> str:
    """Отправляет срез на сервер, если он изменился с прошлого раза. Нужен, чтобы сводки и ответы бота
    видели раскладку, закрытые дела и склеенных людей. Возвращает: sent | same | empty | failed."""
    global _last_snapshot_hash
    corpus = build_snapshot_corpus(vault)
    if not corpus.strip():
        return "empty"
    digest = hashlib.sha256(corpus.encode("utf-8")).hexdigest()
    if digest == _last_snapshot_hash:
        return "same"
    try:
        api(url, token, "/snapshot", {"corpus": corpus})
    except (urllib.error.URLError, OSError, ValueError):
        return "failed"
    _last_snapshot_hash = digest
    return "sent"


_last_profile_hash = ""


def push_profile_to_server(url: str, token: str, vault: Path) -> str:
    """Отправляет серверу список людей (имя, роль, псевдонимы), чтобы бот не переспрашивал про тех, кто уже есть."""
    global _last_profile_hash
    try:
        import vault_people
        payload = vault_people.build_profile(vault)
    except ImportError:
        return "skipped"
    if not payload.get("context"):
        payload.pop("context", None)            # пустой «Профиль.md» не должен стирать профиль на сервере
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if digest == _last_profile_hash or not payload.get("people"):
        return "same"
    try:
        api(url, token, "/profile", payload)
    except (urllib.error.URLError, OSError, ValueError):
        return "failed"
    _last_profile_hash = digest
    return "sent"


def organize_after_sync(vault: Path, fresh: bool = False) -> None:
    """Раскладка и связи для новых заметок (tools/vault_organize.py) и карточки людей. Сбой не мешает очереди."""
    try:
        import vault_organize
        report = vault_organize.organize(vault, settle=0 if fresh else vault_organize.SETTLE_SECONDS)
        for key, title in (("folders", "создана папка"), ("filed", "разложено"), ("archived", "в архив"), ("linked", "связи"), ("errors", "ошибка")):
            for row in report.get(key, []):
                print(f"  {title}: {row}")
    except ImportError:
        return                                  # файл организатора не положили рядом: просто пропускаем
    except Exception as exc:  # noqa: BLE001 - организатор не должен ронять синхронизацию
        print(f"Организатор заметок не сработал: {type(exc).__name__}")
    try:
        import vault_people
        created = vault_people.run(vault)["created"]
        for name in created:
            print(f"  карточка человека: {name}")
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"Карточки людей не обновились: {type(exc).__name__}")


def report_unresolved(vault: Path, item: dict, result: str, known: set[str] | None = None) -> None:
    """Действие из Telegram не применилось (не найдено или подходит несколько заметок): не теряем молча,
    а оставляем заметку в 0-Inbox, чтобы человек увидел это в Obsidian."""
    try:
        payload = json.loads(item.get("markdown") or "{}")
    except ValueError:
        payload = {}
    title = str(payload.get("title") or "?")
    action = "закрыть дело" if payload.get("action") == "close" else "изменить срок дела"
    reason = "не найдено ни одной заметки с таким названием" if result == "not_found" else "подходит несколько заметок с таким названием"
    seed = f"{item.get('id')}-{title}-{payload.get('created')}"
    name = f"Не-применено-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:8]}.md"
    body = ("---\nschema: 1\ntype: note\n" f"title: {json.dumps('Не применено из Telegram: ' + title, ensure_ascii=False)}\n"
            "status: \"не-разобрано\"\n---\n\n"
            f"Из Telegram пришло действие «{action}» для дела «{title}», но {reason}. "
            "Найди дело в Obsidian и поменяй вручную (статус или срок).\n")
    write_note(vault, "0-Inbox", name, body, known)


def sync_once(url: str, token: str, vault: Path) -> int:
    saved = 0
    known = index_names(vault)
    while True:
        batch = api(url, token, "/pending?limit=50&service=1")
        items = batch.get("items", [])
        if not items:
            return saved
        done = []
        for item in items:
            if item.get("kind") == "file":
                try:
                    data = download(url, token, f"/file/{item['id']}")
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        print(f"  ! исходник {item['filename']}: на сервере его уже нет, пропускаю")
                        done.append(item["id"])
                    else:
                        print(f"  ! исходник {item['filename']}: не получилось скачать ({exc.code}), попробую позже")
                    continue
                except (urllib.error.URLError, OSError) as exc:
                    print(f"  ! исходник {item['filename']}: {type(exc).__name__}, попробую позже")
                    continue
                path = save_source(vault, item, data)
                if path:
                    print(f"  + {path.relative_to(vault)}")
                done.append(item["id"])
                continue
            if item.get("kind", "note") != "note":
                result = apply_service(vault, item)
                if result in ("not_found", "ambiguous"):
                    report_unresolved(vault, item, result, known)
                print(f"  ~ {(json.loads(item['markdown']).get('title') if result != 'invalid' else '?')}: {SERVICE_RU[result]}")
                done.append(item["id"])
                continue
            path = write_note(vault, item.get("folder", ""), item["filename"], item["markdown"], known)
            if path:
                saved += 1
                print(f"  + {path.relative_to(vault)}")
            done.append(item["id"])           # дошли сюда — файл на диске (или уже был)
        if done:
            api(url, token, "/ack", {"ids": done})
        if len(done) < len(items) or batch.get("remaining", 0) <= len(items):
            return saved            # что-то не скачалось: повторим на следующем проходе, а не крутимся по кругу


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Забрать заметки из очереди в хранилище.")
    parser.add_argument("--url", required=True, help="адрес сервиса, например https://xxx.up.railway.app")
    parser.add_argument("--vault", required=True, help="папка хранилища SecondBrain")
    parser.add_argument("--token", default=os.environ.get("SECOND_BRAIN_TOKEN", ""))
    parser.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд (0 — один раз)")
    parser.add_argument("--no-snapshot", action="store_true", help="не отправлять на сервер срез заметок (свойства без текстов)")
    parser.add_argument("--no-organize", action="store_true", help="не раскладывать заметки и не связывать их")
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
            if not args.no_organize:
                organize_after_sync(vault, fresh=bool(saved))
            if not args.no_organize and push_profile_to_server(args.url, args.token, vault) == "sent":
                print("  список людей для бота обновлён")
            if not args.no_snapshot:
                state = push_snapshot(args.url, args.token, vault)
                if state == "sent":
                    print("  срез для бота обновлён")
                elif state == "failed":
                    print("  срез для бота обновить не получилось, попробую позже")
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
