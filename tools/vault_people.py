#!/usr/bin/env python3
"""Карточки людей для хранилища «Второй Мозг». Запускается на ноутбуке, ничего не отправляет в сеть.

Что делает:
1. Находит всех людей, названных в заметках (свойство `people`) и в заметках типа person.
2. Для каждого создаёт карточку 1-Люди/Имя.md, если её ещё нет.
3. Переносит в карточку данные из заметок типа person (телефоны, почта, день рождения и т.д.):
   пустые поля заполняет, списки дополняет. Уже заполненное никогда не затирает.
4. Тело карточки (всё ниже свойств) не трогает, кроме создания новой карточки.

Только стандартная библиотека Python 3.9+. Запуск:
    python vault_people.py "C:\\путь\\к\\SecondBrain"            # создать и обновить
    python vault_people.py "C:\\путь\\к\\SecondBrain" --dry-run   # только показать, что будет
    python vault_people.py "C:\\путь\\к\\SecondBrain" --tidy      # план: склеить дубли «Имя» и «Имя — описание»
    python vault_people.py "C:\\путь\\к\\SecondBrain" --tidy --apply
    python vault_people.py "C:\\путь\\к\\SecondBrain" --push https://ТВОЙ-АДРЕС   # ещё и отправить профиль и имена людей на сервер
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PEOPLE_DIR = "1-Люди"
SKIP_DIRS = {"7-Исходники", "Шаблоны", "Дашборды", "3-Архив", ".obsidian", ".trash"}

SCALARS = (
    "first_name", "last_name", "patronymic", "relation", "relation_group", "how_met", "met_date",
    "introduced_by", "last_contact", "contact_goal", "email", "website", "address", "city", "country",
    "timezone", "company", "role", "industry", "birthday", "family", "preferences", "notes",
)
LISTS = ("aliases", "phones", "messengers", "social", "skills", "can_help_with", "interests",
         "gift_ideas", "languages")

CARD_BODY = """
# {name}

## Связанные дела
```dataview
TABLE due AS "Срок", status AS "Статус", summary AS "Суть"
WHERE type = "task" AND contains(people, this.file.link) AND status != "сделано" AND status != "отменено"
SORT due ASC
```

## Контакты и разговоры
```dataview
TABLE kind AS "Вид", topic AS "О чём", agreements AS "Договорились", follow_up AS "Что дальше", created AS "Когда"
WHERE type = "interaction" AND contains(people, this.file.link)
SORT created DESC
```

## Всё, где упомянут(а)
```dataview
TABLE type AS "Тип", summary AS "Суть", created AS "Записано"
WHERE contains(people, this.file.link) AND file.name != this.file.name AND type != "task" AND type != "interaction"
SORT created DESC
```

## Идеи подарков
```dataview
TABLE price AS "Цена", summary AS "Суть"
WHERE type = "product" AND contains(for_whom, this.file.name)
```

## Заметки от руки
"""

_LINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


# ---------- маленький разбор свойств (формат у нас известен) ----------

def split_front(text: str) -> tuple[list[str], str] | None:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return json.loads(value) if value[0] == '"' and _is_json(value) else inner
    return value


def _is_json(value: str) -> bool:
    try:
        json.loads(value)
        return True
    except ValueError:
        return False


def parse_front(lines: list[str]) -> dict[str, object]:
    data: dict[str, object] = {}
    key = None
    for line in lines:
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None and isinstance(data.get(key), list):
            data[key].append(_unquote(item.group(1)))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).split(" #")[0].strip()
        if raw == "":
            data[key] = []                      # возможно, дальше пойдёт блок списка
        elif raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            data[key] = [_unquote(p) for p in re.split(r",\s*(?![^\[]*\]\])", inner) if p.strip()] if inner else []
        else:
            data[key] = _unquote(raw)
    return data


def as_list(value: object) -> list[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    return [str(i).strip() for i in items if str(i).strip()]


def unlink(text: str) -> str:
    m = _LINK.fullmatch(text.strip())
    return m.group(1).strip() if m else re.sub(r"\[\[|\]\]", "", text).strip()


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", name)
    return re.sub(r"\s+", " ", cleaned).strip(" .")[:80]


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------- обход хранилища ----------

def read_notes(vault: Path):
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if rel.parts[0] in SKIP_DIRS:
            continue
        try:
            parts = split_front(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if parts:
            yield path, parse_front(parts[0])


def collect(vault: Path):
    names: dict[str, set[str]] = {}        # имя -> псевдонимы
    facts: dict[str, dict[str, list[str]]] = {}
    cards: set[str] = set()
    for path, front in read_notes(vault):
        in_people = path.relative_to(vault).parts[0] == PEOPLE_DIR
        if str(front.get("type")) == "person":
            title = safe_name(split_title(str(front.get("title") or path.stem))[0])
            if not title:
                continue
            if in_people:
                cards.add(title)
                names.setdefault(title, set())
                continue
            names.setdefault(title, set())
            bucket = facts.setdefault(title, {})
            for key in SCALARS:
                value = str(front.get(key) or "").strip()
                if value and value != "[]":
                    bucket.setdefault(key, [])
                    if value not in bucket[key]:
                        bucket[key].append(value)
            for key in LISTS:
                for value in as_list(front.get(key)):
                    bucket.setdefault(key, [])
                    if value not in bucket[key]:
                        bucket[key].append(value)
        for raw in as_list(front.get("people")):
            name = safe_name(unlink(raw))
            if name:
                names.setdefault(name, set())
    return names, facts, cards


_DASH = re.compile(r"\s+[—–-]\s+")
_PLAIN_DESC = {"профиль", "карточка", "заметка"}


def split_title(title: str) -> tuple[str, str]:
    """'Ануар — друг, работает в комитете' -> ('Ануар', 'друг, работает в комитете')."""
    parts = _DASH.split(title.strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.casefold().replace("ё", "е")).strip()


# ---------- запись карточек ----------

def new_card(name: str, fact: dict[str, list[str]], now: datetime) -> str:
    lines = ["---", "schema: 1", f"title: {q(name)}", 'summary: "Карточка человека"',
             f"created: {now:%Y-%m-%dT%H:%M}", 'type: "person"', 'status: "актуален"',
             'stage: "processed"', 'area: "окружение"']
    for key in SCALARS:
        if fact.get(key):
            lines.append(f"{key}: {q(fact[key][0])}")
    for key in LISTS:
        if fact.get(key):
            lines.append(f"{key}:")
            lines += [f"  - {q(v)}" for v in fact[key]]
    lines += ["tags:", "  - люди", 'source: "агент"', "---"]
    return "\n".join(lines) + "\n" + CARD_BODY.format(name=name)


def merge_into_card(text: str, fact: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Возвращает (новый текст, что изменено). Заполненные поля не трогает."""
    parts = split_front(text)
    if not parts or not fact:
        return text, []
    front_lines, body = parts
    current = parse_front(front_lines)
    changed: list[str] = []

    def span(key: str):
        for i, line in enumerate(front_lines):
            if re.match(rf"^{re.escape(key)}:", line):
                j = i + 1
                while j < len(front_lines) and re.match(r"^\s*-\s", front_lines[j]):
                    j += 1
                return i, j
        return None

    for key in SCALARS:
        if key not in fact or str(current.get(key) or "").strip() not in ("", "[]"):
            continue
        new = f"{key}: {q(fact[key][0])}"
        found = span(key)
        if found:
            front_lines[found[0]:found[1]] = [new]
        else:
            front_lines.append(new)
        changed.append(key)
    for key in LISTS:
        if key not in fact:
            continue
        have = as_list(current.get(key))
        extra = [v for v in fact[key] if v not in have]
        if not extra:
            continue
        block = [f"{key}:"] + [f"  - {q(v)}" for v in have + extra]
        found = span(key)
        if found:
            front_lines[found[0]:found[1]] = block
        else:
            front_lines.extend(block)
        changed.append(key)
    if not changed:
        return text, []
    return "---\n" + "\n".join(front_lines) + "\n---\n" + body, changed


def run(vault: Path, dry_run: bool = False, now: datetime | None = None) -> dict[str, list[str]]:
    now = now or datetime.now()
    names, facts, existing = collect(vault)
    report = {"created": [], "updated": []}
    folder = vault / PEOPLE_DIR
    for name in sorted(names):
        path = folder / f"{name}.md"
        fact = facts.get(name, {})
        if path.exists():
            text = path.read_text(encoding="utf-8")
            new_text, changed = merge_into_card(text, fact)
            if changed:
                report["updated"].append(f"{name}: {', '.join(changed)}")
                if not dry_run:
                    path.write_text(new_text, encoding="utf-8")
        elif name in existing:
            continue      # карточка лежит в другой папке, второй не заводим
        else:
            report["created"].append(name)
            if not dry_run:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(new_card(name, fact, now), encoding="utf-8")
    return report


# ---------- наведение порядка в 1-Люди ----------

DUP_DIR = "3-Архив/Люди-дубли"
_DATAVIEW = re.compile(r"```dataview.*?```", re.S)


def _facts_from_front(front: dict, desc: str) -> dict[str, list[str]]:
    fact: dict[str, list[str]] = {}
    for key in SCALARS:
        value = str(front.get(key) or "").strip()
        if value and value != "[]":
            fact[key] = [value]
    for key in LISTS:
        values = as_list(front.get(key))
        if values:
            fact[key] = values
    if desc and _norm_name(desc) not in _PLAIN_DESC:
        fact.setdefault("relation", [desc.split(",")[0].strip()])
        fact.setdefault("notes", [desc])
    return fact


def _useful_body(body: str) -> str:
    text = _DATAVIEW.sub("", body or "").strip()
    text = re.sub(r"^#\s+.*\n?", "", text).strip()          # заголовок карточки
    text = re.sub(r"^##\s+(Связанные дела|Контакты и разговоры|Всё, где упомянут\(а\))\s*$", "", text, flags=re.M).strip()
    return text


def _set_title(text: str, name: str) -> str:
    parts = split_front(text)
    if not parts:
        return text
    front_lines, body = parts
    for i, line in enumerate(front_lines):
        if line.startswith("title:"):
            front_lines[i] = f"title: {q(name)}"
            break
    return "---\n" + "\n".join(front_lines) + "\n---\n" + body


def _unique(folder: Path, name: str) -> Path:
    path, n = folder / name, 2
    while path.exists():
        path = folder / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    return path


def tidy(vault: Path, apply: bool = False) -> dict[str, list[str]]:
    """Склеивает «Имя» и «Имя — описание» в одну карточку. Дубли не удаляются, а уезжают в 3-Архив/Люди-дубли.

    Без apply только описывает, что будет сделано. Похожие, но не совпадающие имена (опечатки) только перечисляются.
    """
    report = {"merged": [], "renamed": [], "moved": [], "similar": [], "foreign": []}
    folder = vault / PEOPLE_DIR
    if not folder.is_dir():
        return report
    groups: dict[str, list[dict]] = {}
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parts = split_front(text)
        if not parts:
            continue
        front, body = parse_front(parts[0]), parts[1]
        if str(front.get("type") or "") != "person":
            report["foreign"].append(path.name)
            continue
        raw_title = str(front.get("title") or path.stem)
        name, desc = split_title(raw_title)
        name = safe_name(name)
        if not name:
            continue
        groups.setdefault(_norm_name(name), []).append(
            {"path": path, "text": text, "front": front, "body": body, "name": name, "desc": desc,
             "plain": not desc and safe_name(path.stem) == name})
    for entries in groups.values():
        if len(entries) == 1 and not entries[0]["desc"] and entries[0]["plain"]:
            continue
        primary = next((e for e in entries if e["plain"]), None) or next((e for e in entries if not e["desc"]), None)
        rest = [e for e in entries if e is not primary]
        target_name = (primary or entries[0])["name"]
        if primary is None:                                  # все с описаниями: первая становится карточкой
            primary, rest = entries[0], entries[1:]
            primary = {**primary, "text": _set_title(primary["text"], target_name), "rename": True}
            fact = _facts_from_front(primary["front"], primary["desc"])
            new_text, _ = merge_into_card(primary["text"], fact)
            primary["text"] = new_text
        text = primary["text"]
        for dup in rest:
            fact = _facts_from_front(dup["front"], dup["desc"])
            text, _ = merge_into_card(text, fact)
            extra = _useful_body(dup["body"])
            if extra and extra not in text:
                text = text.rstrip("\n") + f"\n\n## Из заметки «{dup['path'].stem}»\n\n{extra}\n"
        if primary.get("rename"):
            report["renamed"].append(f"{primary['path'].stem} -> {target_name}")
        for dup in rest:
            report["merged"].append(f"{dup['path'].stem} -> {target_name}")
        if apply:
            target = folder / f"{target_name}.md"
            if primary.get("rename") and target.exists() and target != primary["path"]:
                target = _unique(folder, f"{target_name}.md")
            target.write_text(text, encoding="utf-8", newline="\n")
            if primary["path"] != target:
                primary["path"].unlink()
            archive = vault / Path(DUP_DIR)
            for dup in rest:
                if dup["path"] == target:
                    continue
                archive.mkdir(parents=True, exist_ok=True)
                dup["path"].replace(_unique(archive, dup["path"].name))
    names = sorted({e["name"] for es in groups.values() for e in es})
    import difflib
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _norm_name(a) != _norm_name(b) and " " in a and " " in b \
                    and difflib.SequenceMatcher(None, _norm_name(a), _norm_name(b)).ratio() >= 0.88:
                report["similar"].append(f"{a} / {b}")
    return report


def _name_variants(front: dict, name: str) -> list[str]:
    """Псевдонимы человека: свои из карточки плюс «Имя Фамилия» и «Фамилия Имя» из полей имени."""
    first, last = str(front.get("first_name") or "").strip(), str(front.get("last_name") or "").strip()
    found = as_list(front.get("aliases"))
    full = str(front.get("name") or "").strip()
    if full:
        found.append(full)
    if first and last:
        found += [f"{first} {last}", f"{last} {first}"]
    out: list[str] = []
    for item in found:
        if _norm_name(item) != _norm_name(name) and item not in out:
            out.append(item)
    return out


def build_profile(vault: Path) -> dict:
    """Собирает то, что уходит на сервер: текст файла «Профиль.md» и список людей (имя, роль, псевдонимы)."""
    context = ""
    profile = vault / "Профиль.md"
    if profile.exists():
        text = profile.read_text(encoding="utf-8").replace("\r\n", "\n")
        parts = split_front(text)
        if parts:
            text = parts[1]
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() not in ("", "-", "- …", "…")]
        context = "\n".join(lines).strip()
    people = []
    folder = vault / PEOPLE_DIR
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            parts = split_front(path.read_text(encoding="utf-8"))
            front = parse_front(parts[0]) if parts else {}
            role = str(front.get("relation") or front.get("relation_group") or "").strip()
            if role == "[]":
                role = ""
            name = split_title(str(front.get("title") or path.stem))[0] or path.stem
            people.append({"name": name, "role": role, "aliases": _name_variants(front, name)})
    return {"context": context, "people": people}


def push_profile(url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + "/profile", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"x-api-key": token, "content-type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Создать и обновить карточки людей в хранилище.")
    parser.add_argument("vault", help="папка хранилища SecondBrain")
    parser.add_argument("--dry-run", action="store_true", help="ничего не писать, только показать")
    parser.add_argument("--tidy", action="store_true",
                        help="склеить дубли «Имя» и «Имя — описание» (без --apply только покажет план)")
    parser.add_argument("--apply", action="store_true", help="вместе с --tidy: действительно выполнить")
    parser.add_argument("--push", metavar="URL", help="после обновления отправить профиль и список людей на сервер")
    parser.add_argument("--token", default=os.environ.get("SECOND_BRAIN_TOKEN", ""))
    args = parser.parse_args(argv)
    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"Папка не найдена: {vault}")
        return 1
    if args.tidy:
        plan = tidy(vault, apply=args.apply)
        print(("Сделано:" if args.apply else "План (ничего не изменено, добавь --apply):"))
        for key, title in (("merged", "склеено"), ("renamed", "переименовано"), ("similar", "похожие имена, проверь руками"),
                           ("foreign", "не карточка человека, лежит в 1-Люди")):
            for row in plan[key]:
                print(f"  {title}: {row}")
        if not any(plan.values()):
            print("  Дублей не найдено.")
        return 0
    report = run(vault, args.dry_run)
    prefix = "Было бы: " if args.dry_run else ""
    print(f"{prefix}новых карточек: {len(report['created'])}, дополнено: {len(report['updated'])}")
    for name in report["created"]:
        print(f"  + {name}")
    for line in report["updated"]:
        print(f"  ~ {line}")
    if args.push and not args.dry_run:
        if not args.token:
            print("Для --push нужен токен: переменная SECOND_BRAIN_TOKEN или параметр --token")
            return 1
        payload = build_profile(vault)
        if len(payload["context"]) > 8000:
            print("Профиль длиннее 8000 символов: сервер возьмёт только начало. Сократи файл «Профиль.md».")
        answer = push_profile(args.push, args.token, payload)
        print(f"Отправлено на сервер: людей {answer.get('people')}, символов профиля {answer.get('context_chars')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
