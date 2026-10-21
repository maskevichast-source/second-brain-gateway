#!/usr/bin/env python3
"""Автоматическая раскладка заметок и связи между ними. Запускается на ноутбуке, ничего не отправляет в сеть.

Что делает за один проход (вызывается из vault_sync.py после каждой выгрузки или вручную):
1. Находит новые заметки в 0-Inbox со `stage: "inbox"`.
2. Ищет в хранилище родственные заметки: общие люди, общие «связанные» объекты, общие редкие слова и теги,
   упоминание названия другой заметки в тексте. Записывает их в конец заметки блоком «Связано»
   (`[[имя файла|Название]]`). Блок служебный: при каждом проходе он пересчитывается, остальной текст не трогается.
3. Если ИИ был уверен в типе (confidence >= 0.75, нет тега «проверить», тип не «заметка»), переносит заметку из
   0-Inbox в папку по типу и ставит `stage: "filed"`. Сомнительные остаются в 0-Inbox на твою проверку.
4. Закрытые дела, которые разложил этот скрипт, уезжают в 3-Архив/ГОД.
5. Папки подстраиваются под тебя: если ты уже держишь заметки какого-то типа в своей папке
   (3 штуки и больше, и это большинство), новые заметки этого типа едут туда же.

Ничего не удаляется и не перезаписывается: файл меняется атомарно, карточки людей и заметки типа person не переносятся.
Только стандартная библиотека Python 3.9+. Запуск:
    python vault_organize.py "C:\\путь\\к\\SecondBrain"             # раскладка и связи
    python vault_organize.py "C:\\путь\\к\\SecondBrain" --dry-run   # только показать, что будет
    python vault_organize.py "C:\\путь\\к\\SecondBrain" --all       # пересчитать связи у всех заметок и составить отчёт
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

INBOX = "0-Inbox"
PEOPLE_DIR = "1-Люди"
ARCHIVE_DIR = "3-Архив"
SKIP_DIRS = {"7-Исходники", "Шаблоны", "Дашборды", ".obsidian", ".trash", ARCHIVE_DIR}
NEVER_MOVE_TYPES = {"person", "note", "bookmark"}
CLOSED = {"сделано", "отменено", "готово", "состоялось", "отказался", "отменена", "завершена",
          "достигнута", "отброшена", "расторгнут", "пересмотрено", "отвечен", "реализована", "отброшена"}
ARCHIVE_TYPES = {"task", "event", "project", "goal", "case", "question", "trip"}
GENERIC_TAGS = {"в-аду", "проверить", "дневник", "заметка", "разное"}

MIN_CONFIDENCE = 0.75
MAX_LINKS = 5
LINK_THRESHOLD = 4.0
SETTLE_SECONDS = 20                    # файл, который только что записали или синхронизируют, пока не трогаем
BODY_CHARS = 1200
BLOCK_START, BLOCK_END = "<!-- sb:links -->", "<!-- /sb:links -->"
BLOCK_RE = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.S)

# Папки по умолчанию; если у тебя уже есть своя раскладка, learn_destinations() её подхватит.
DEFAULT_FOLDERS = {
    "task": "4-Дела/Дела и события", "event": "4-Дела/Дела и события", "interaction": "4-Дела/Дела и события",
    "project": "4-Дела/Проекты и цели", "goal": "4-Дела/Проекты и цели",
    "idea": "2-Знания/Идеи и выводы", "learning": "2-Знания/Идеи и выводы", "quote": "2-Знания/Идеи и выводы",
    "question": "2-Знания/Идеи и выводы", "decision": "2-Знания/Идеи и выводы",
    "media": "2-Знания/Медиа",
    "home": "5-Дом и жизнь/Дом", "document": "5-Дом и жизнь/Документы", "case": "5-Дом и жизнь/Документы",
    "health": "5-Дом и жизнь/Здоровье",
    "trip": "5-Дом и жизнь/Поездки и места", "place": "5-Дом и жизнь/Поездки и места",
    "product": "5-Дом и жизнь/Поездки и места",
    "journal": "6-Дневник",
}
STOP = set("""это как что или для при про над под без если чтобы когда тоже очень можно нужно надо есть был была были было
будет быть этот эта эти того тому также уже ещё еще только потом после перед между через сегодня завтра вчера
заметка заметки сделать сделал нужно хочу хотел позвонить купить посмотреть""".split())


# ---------- разбор свойств (формат у нас известен) ----------

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def split_front(text: str):
    """(строки свойств, номер закрывающей ---, все строки с переводами строк) или None."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":      # BOM из Блокнота не должен выбрасывать заметку
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], i, lines
    return None


def parse_front(lines: list[str]) -> dict:
    data: dict = {}
    key = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None and isinstance(data.get(key), list):
            data[key].append(_unquote(item.group(1)))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [_unquote(p) for p in re.split(r",\s*(?![^\[]*\]\])", inner) if p.strip()] if inner else []
        else:
            data[key] = _unquote(value)
    return data


def _as_list(value) -> list[str]:
    if value in (None, "", []):
        return []
    return [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


_LINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def unlink(text: str) -> str:
    m = _LINK.search(text)
    return (m.group(1) if m else text).strip()


def norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


# ---------- заметки ----------

@dataclass
class Note:
    path: Path
    rel: Path
    text: str
    front: dict
    body: str
    mtime: float
    tokens: set = field(default_factory=set)

    @property
    def folder(self) -> str:
        return self.rel.parts[0] if len(self.rel.parts) > 1 else ""

    @property
    def title(self) -> str:
        return str(self.front.get("title") or self.path.stem).strip()

    @property
    def type(self) -> str:
        return str(self.front.get("type") or "").strip()

    @property
    def stage(self) -> str:
        return str(self.front.get("stage") or "").strip()

    @property
    def private(self) -> bool:
        return str(self.front.get("private", "")).lower() in ("true", "yes", "да", "1")

    @property
    def people(self) -> set:
        return {norm(unlink(p)) for p in _as_list(self.front.get("people"))}

    @property
    def related(self) -> set:
        out = {norm(unlink(p)) for p in _as_list(self.front.get("related"))}
        for key in ("project", "for_whom", "contractor", "asked_to", "recommended_by", "introduced_by"):
            out |= {norm(unlink(p)) for p in _as_list(self.front.get(key))}
        return out

    @property
    def tags(self) -> set:
        return {norm(t).lstrip("#") for t in _as_list(self.front.get("tags"))} - GENERIC_TAGS

    @property
    def names(self) -> set:
        names = {norm(self.title), norm(self.path.stem)}
        names |= {norm(a) for a in _as_list(self.front.get("aliases"))}
        return {n for n in names if n}


def stems(text: str) -> set:
    out = set()
    for word in re.findall(r"[^\W\d_]{4,}", text.casefold()):
        if word not in STOP:
            out.add(word[:5])
    return out


def read_note(path: Path, vault: Path):
    try:
        with open(path, encoding="utf-8", newline="") as handle:      # newline="" сохраняет \r\n как есть
            text = handle.read()
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None
    parsed = split_front(text)
    if not parsed:
        return None
    front_lines, end, lines = parsed
    front = parse_front(front_lines)
    body = BLOCK_RE.sub("", "".join(lines[end + 1:]))
    note = Note(path=path, rel=path.relative_to(vault), text=text, front=front, body=body, mtime=mtime)
    base = " ".join([note.title, str(front.get("summary") or ""), " ".join(_as_list(front.get("tags"))),
                     re.sub(r"\[\[|\]\]|>", " ", body[:BODY_CHARS])])
    note.tokens = stems(base)
    return note


def scan(vault: Path) -> list[Note]:
    notes = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if rel.parts[0] in SKIP_DIRS or any(p.startswith(".") for p in rel.parts):
            continue
        if len(rel.parts) == 1:
            continue                                  # служебные файлы в корне («00 Старт», «Профиль»)
        note = read_note(path, vault)
        if note:
            notes.append(note)
    return notes


# ---------- связи ----------

def build_index(notes: list[Note]) -> dict:
    by_name: dict[str, list[Note]] = defaultdict(list)
    df: Counter = Counter()
    for n in notes:
        for name in n.names:
            by_name[name].append(n)
        df.update(n.tokens)
    return {"by_name": by_name, "df": df, "total": max(len(notes), 1)}


def link_text(target: Note) -> str:
    title = target.title.replace("|", "/").replace("]", ")").replace("[", "(")
    return f"[[{target.path.stem}|{title}]]" if norm(target.path.stem) != norm(title) else f"[[{target.path.stem}]]"


def related_notes(note: Note, notes: list[Note], index: dict) -> list[tuple[Note, float, list[str]]]:
    df, total = index["df"], index["total"]
    text_norm = norm(note.title + " " + note.body[:BODY_CHARS])
    scored = []
    for other in notes:
        if other.path == note.path or other.private or other.type == "person" and other.folder == PEOPLE_DIR:
            continue
        score, why = 0.0, []
        shared_people = note.people & other.people
        if shared_people:
            score += 3.0 * min(len(shared_people), 2)
            why.append("люди: " + ", ".join(sorted(shared_people)))
        shared_related = note.related & other.related
        if shared_related:
            score += 3.0 * min(len(shared_related), 2)
            why.append("связано: " + ", ".join(sorted(shared_related)))
        # прямое указание: «связанное» этой заметки — название или имя файла другой, и наоборот
        if (note.related & other.names) or (other.related & note.names):
            score += 4.0
            why.append("упомянуто в свойствах")
        shared_tags = note.tags & other.tags
        if shared_tags:
            score += 1.5 * min(len(shared_tags), 3)
            why.append("теги: " + ", ".join(sorted(shared_tags)))
        # название другой заметки встречается в тексте этой
        for name in other.names:
            if len(name) >= 5 and name in text_norm and name != norm(note.title):
                score += 4.0
                why.append("названо в тексте")
                break
        shared_words = sorted(
            (w for w in note.tokens & other.tokens if df[w] <= max(3, total * 0.08)),
            key=lambda w: df[w])
        if shared_words:
            score += sum(1.0 if df[w] <= 3 else 0.5 for w in shared_words[:6])
            why.append("общие слова: " + ", ".join(shared_words[:3]))
        if score >= LINK_THRESHOLD:
            scored.append((other, score, why))
    scored.sort(key=lambda r: (-r[1], r[0].title))
    return scored[:MAX_LINKS]


def render_block(found: list[tuple[Note, float, list[str]]], nl: str = "\n") -> str:
    if not found:
        return ""
    rows = [BLOCK_START, "## Связано", ""]
    for other, _, why in found:
        rows.append(f"- {link_text(other)} — {other.type or 'заметка'}; {why[0]}")
    rows.append(BLOCK_END)
    return nl.join(rows) + nl


def apply_block(text: str, block: str) -> str:
    """Подставляет блок «Связано» (или убирает, если пусто). Остальной текст не меняется."""
    nl = "\r\n" if "\r\n" in text else "\n"
    cleaned = BLOCK_RE.sub("", text).rstrip("\r\n")
    if not block:
        return cleaned + nl if BLOCK_RE.search(text) else text
    return cleaned + nl + nl + block


# ---------- папки ----------

def learn_destinations(notes: list[Note]) -> dict[str, str]:
    """Тип -> папка. Берёт папку, где пользователь уже держит >= 3 заметок типа (и это >= 60%)."""
    places: dict[str, Counter] = defaultdict(Counter)
    for n in notes:
        if not n.type or n.folder in (INBOX, PEOPLE_DIR, "") or n.stage == "inbox":
            continue
        places[n.type][str(n.rel.parent).replace("\\", "/")] += 1
    result = dict(DEFAULT_FOLDERS)
    for note_type, counter in places.items():
        folder, count = counter.most_common(1)[0]
        if note_type in NEVER_MOVE_TYPES:
            continue
        if count >= 3 and count / sum(counter.values()) >= 0.6:
            result[note_type] = folder
    return result


def should_file(note: Note) -> bool:
    if note.type in NEVER_MOVE_TYPES or not note.type:
        return False
    try:
        confidence = float(note.front.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    tags = {norm(t).lstrip("#") for t in _as_list(note.front.get("tags"))}
    return confidence >= MIN_CONFIDENCE and "проверить" not in tags


def destination(note: Note, folders: dict[str, str]) -> str | None:
    folder = folders.get(note.type)
    if not folder:
        return None
    if note.type == "journal" and folder == DEFAULT_FOLDERS["journal"]:
        month = str(note.front.get("created") or "")[:7]
        return f"{folder}/{month}" if re.fullmatch(r"\d{4}-\d{2}", month) else folder
    return folder


def set_property(text: str, key: str, value: str) -> str:
    parsed = split_front(text)
    if not parsed:
        return text
    front_lines, end, lines = parsed
    nl = "\r\n" if lines[end].endswith("\r\n") else "\n"
    row = f"{key}: {json.dumps(value, ensure_ascii=False)}{nl}"
    for i in range(1, end):
        if lines[i].startswith(key + ":"):
            lines[i] = row
            break
    else:
        lines.insert(end, row)
    return "".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp-org")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _free_path(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, counter = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    return target


# ---------- основной проход ----------

def organize(vault: Path, dry_run: bool = False, refresh_all: bool = False, now: float | None = None,
             settle: float = SETTLE_SECONDS) -> dict:
    now = time.time() if now is None else now
    notes = scan(vault)
    index = build_index([n for n in notes if not n.private])
    folders = learn_destinations(notes)
    report = {"linked": [], "filed": [], "archived": [], "kept": []}
    recent = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    for note in notes:
        in_inbox = note.folder == INBOX and note.stage == "inbox"
        ours = note.stage == "filed"
        fresh = ours and str(note.front.get("created") or "")[:10] >= _days_ago(recent, 30)
        if not (in_inbox or refresh_all or fresh or ours):
            continue
        if now - note.mtime < settle or note.private:
            continue
        text = note.text
        found = related_notes(note, notes, index) if (in_inbox or refresh_all or fresh) else []
        if in_inbox or refresh_all or fresh:
            new_text = apply_block(text, render_block(found, "\r\n" if "\r\n" in text else "\n"))
            if new_text != text:
                report["linked"].append(f"{note.title}: {len(found)}")
                text = new_text
        target_dir = None
        mark_filed = False
        if in_inbox:
            if should_file(note):
                dest = destination(note, folders)
                if dest:
                    target_dir = vault / Path(dest)
                    mark_filed = True               # stage: filed ставим уже ПОСЛЕ успешного переноса
            else:
                report["kept"].append(note.title)
        elif ours and note.type in ARCHIVE_TYPES and norm(str(note.front.get("status"))) in CLOSED:
            year = str(note.front.get("created") or "")[:4]
            target_dir = vault / ARCHIVE_DIR / (year if year.isdigit() else "")
        if dry_run:
            if target_dir:
                report["archived" if note.stage == "filed" else "filed"].append(
                    f"{note.title} -> {target_dir.relative_to(vault)}")
            continue
        try:
            if note.path.stat().st_mtime != note.mtime:
                report.setdefault("errors", []).append(f"{note.title}: изменена во время обработки, пропускаю")
                continue                            # правка в Obsidian между чтением и записью не должна потеряться
            if text != note.text:
                _atomic_write(note.path, text)
            if target_dir:
                target_dir.mkdir(parents=True, exist_ok=True)
                new_path = _free_path(target_dir, note.path.name)
                os.replace(note.path, new_path)
                if mark_filed:
                    _atomic_write(new_path, set_property(text, "stage", "filed"))
                report["archived" if note.stage == "filed" else "filed"].append(
                    f"{note.title} -> {new_path.parent.relative_to(vault)}")
        except OSError as exc:
            report.setdefault("errors", []).append(f"{note.title}: {type(exc).__name__}")
    if refresh_all and not dry_run:
        write_health_report(vault, notes, index)
    if not dry_run:
        ensure_dashboard(vault)
        report["folders"] = ensure_folders(vault)
    return report


def _days_ago(day: str, days: int) -> str:
    return datetime.fromtimestamp(datetime.strptime(day, "%Y-%m-%d").timestamp() - days * 86400).strftime("%Y-%m-%d")


# ---------- дашборд и отчёт о здоровье связей ----------

DASHBOARD = """# Разложено автоматически

Заметки, которые ИИ разложил сам (уверенность от 0.75). Если что-то лежит не там, перенеси файл куда нужно:
в следующий раз заметки этого типа поедут туда же. Просмотрел — поменяй `stage` на `processed`.

```dataview
TABLE type AS "Тип", summary AS "Суть", file.folder AS "Папка", created AS "Записано"
WHERE stage = "filed"
SORT created DESC
LIMIT 40
```
"""


def ensure_folders(vault: Path) -> list[str]:
    """Создаёт пустые папки раскладки заранее, чтобы они были видны в Obsidian. Возвращает созданные."""
    wanted = sorted(set(DEFAULT_FOLDERS.values()) - {"6-Дневник"} | {"6-Дневник", "7-Исходники", INBOX, PEOPLE_DIR, ARCHIVE_DIR})
    created = []
    for rel in wanted:
        path = vault / rel
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
                created.append(rel)
            except OSError:
                pass
    return created


def ensure_dashboard(vault: Path) -> None:
    folder = vault / "Дашборды"
    path = folder / "Разложено автоматически.md"
    if folder.is_dir() and not path.exists():
        try:
            path.write_text(DASHBOARD, encoding="utf-8", newline="\n")
        except OSError:
            pass


def write_health_report(vault: Path, notes: list[Note], index: dict) -> None:
    """Дашборды/Здоровье связей.md: битые ссылки, заметки-сироты, имена без карточек."""
    stems_all = {norm(n.path.stem) for n in notes} | {norm(n.title) for n in notes}
    inbound: Counter = Counter()
    broken: dict[str, set] = defaultdict(set)
    for n in notes:
        for target in _LINK.findall(n.text):
            key = norm(target)
            if key in stems_all:
                inbound[key] += 1
            else:
                broken[target.strip()].add(n.title)
    people_dir = vault / PEOPLE_DIR
    cards = {norm(p.stem) for p in people_dir.glob("*.md")} if people_dir.is_dir() else set()
    orphans = [n for n in notes if n.type not in ("person",) and not _LINK.search(n.text)
               and inbound[norm(n.path.stem)] == 0 and inbound[norm(n.title)] == 0 and not n.private]
    lines = ["# Здоровье связей", "", f"_Обновлено {datetime.now():%Y-%m-%d %H:%M}. Файл собирается автоматически._", "",
             f"Всего заметок: {len(notes)}. Без единой связи: {len(orphans)}. Битых ссылок: {len(broken)}.", ""]
    lines += ["## Заметки без связей", ""] + [f"- [[{o.path.stem}|{o.title}]] ({o.type or 'без типа'})" for o in orphans[:40]]
    lines += ["", "## Ссылки на несуществующие заметки", ""]
    for name, where in sorted(broken.items(), key=lambda kv: -len(kv[1]))[:40]:
        if norm(name) in cards:
            continue
        lines.append(f"- «{name}»: в {len(where)} заметках")
    lines.append("")
    folder = vault / "Дашборды"
    if folder.is_dir():
        try:
            (folder / "Здоровье связей.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Разложить новые заметки по папкам и связать их между собой.")
    parser.add_argument("vault", help="папка хранилища SecondBrain")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    parser.add_argument("--all", action="store_true", help="пересчитать связи у всех заметок и составить отчёт")
    args = parser.parse_args(argv)
    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"Папка не найдена: {vault}")
        return 1
    report = organize(vault, dry_run=args.dry_run, refresh_all=args.all)
    for key, title in (("filed", "разложено"), ("archived", "в архив"), ("linked", "связи обновлены"),
                       ("folders", "создана папка"), ("kept", "оставлено в 0-Inbox на проверку"), ("errors", "ошибки")):
        for row in report.get(key, []):
            print(f"  {title}: {row}")
    print("Готово." if not args.dry_run else "Это пробный запуск, ничего не изменено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
