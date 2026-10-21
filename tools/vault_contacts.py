#!/usr/bin/env python3
"""Импорт контактов (файл .vcf) в карточки людей хранилища. Всё происходит на ноутбуке, в сеть ничего не уходит.

Как получить файл: icloud.com → Контакты → выделить всех (Ctrl+A) → шестерёнка внизу слева → «Экспорт vCard».

    python vault_contacts.py "C:\\\\путь\\\\Контакты.vcf" "C:\\\\путь\\\\SecondBrain" --dry-run
    python vault_contacts.py "C:\\\\путь\\\\Контакты.vcf" "C:\\\\путь\\\\SecondBrain"

Создаёт 1-Люди/Имя.md для каждого контакта (телефоны, почта, компания, должность, день рождения, адрес).
Существующие карточки только дополняются: заполненное не затирается.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import vault_people as vp


def unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)            # продолжение строки в vCard начинается с пробела
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:                              # QUOTED-PRINTABLE: мягкий перенос — «=» в конце строки
        if out and out[-1].endswith("=") and "QUOTED-PRINTABLE" in out[-1].upper():
            out[-1] = out[-1][:-1] + line
        else:
            out.append(line)
    return out


def unescape(value: str) -> str:
    """Снимает экранирование vCard за один проход: «\\\\n» остаётся обратным слэшем и буквой n, а не переводом строки."""
    table = {"n": " ", "N": " ", ",": ",", ";": ";", "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: table.get(m.group(1), m.group(0)), value).strip()


def _decode_qp(head: str, value: str) -> str:
    """vCard 2.1: ENCODING=QUOTED-PRINTABLE;CHARSET=WINDOWS-1251 → обычный текст."""
    import quopri
    charset = re.search(r"CHARSET=([\w-]+)", head, re.I)
    raw = quopri.decodestring(value.encode("latin-1", "ignore"))
    for enc in ([charset.group(1)] if charset else []) + ["utf-8", "cp1251"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def parse_vcards(text: str) -> list[dict[str, list[str]]]:
    cards, current = [], None
    for line in unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VCARD"):
            current = {}
        elif upper.startswith("END:VCARD"):
            if current is not None:
                cards.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name = head.split(";")[0].split(".")[-1].upper()      # «item1.TEL;type=CELL» -> TEL
            value = _decode_qp(head, value) if "QUOTED-PRINTABLE" in head.upper() else value
            value = unescape(value)
            if value:
                current.setdefault(name, []).append(value)
    return cards


def normalize_birthday(value: str) -> str:
    value = value.strip()
    m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", value)
    if m:
        year = m.group(1)
        return f"{m.group(2)}-{m.group(3)}" if year in ("0000", "1604", "1900") else f"{year}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"--(\d{2})-?(\d{2})", value)
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def card_to_facts(card: dict[str, list[str]]) -> tuple[str, dict[str, list[str]]] | None:
    name = (card.get("FN") or [""])[0]
    if not name and card.get("N"):
        parts = card["N"][0].split(";")
        name = " ".join(p for p in (parts[1] if len(parts) > 1 else "", parts[0]) if p)
    name = vp.safe_name(name)
    if not name:
        return None
    facts: dict[str, list[str]] = {}
    phones = [re.sub(r"\s+", " ", p) for p in card.get("TEL", [])]
    if phones:
        facts["phones"] = list(dict.fromkeys(phones))
    if card.get("EMAIL"):
        facts["email"] = [card["EMAIL"][0]]
    if card.get("ORG"):
        facts["company"] = [card["ORG"][0].replace(";", ", ").strip(", ")]
    if card.get("TITLE"):
        facts["role"] = [card["TITLE"][0]]
    if card.get("BDAY"):
        birthday = normalize_birthday(card["BDAY"][0])
        if birthday:
            facts["birthday"] = [birthday]
    if card.get("NICKNAME"):
        facts["aliases"] = [n.strip() for n in card["NICKNAME"][0].split(",") if n.strip()]
    if card.get("ADR"):
        address = ", ".join(p for p in card["ADR"][0].split(";") if p.strip())
        if address:
            facts["address"] = [address]
    if card.get("URL"):
        facts["website"] = [card["URL"][0]]
    return name, facts


def parse_rev(value: str) -> datetime | None:
    """Дата последнего изменения контакта из строки REV (20250131T101500Z или 2025-01-31T10:15:00Z)."""
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", value.strip())
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def _tokens(name: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[\w]+", name.lower().replace("ё", "е")) if len(w) > 1)


def telegram_active(path: Path, since: datetime) -> list[frozenset[str]]:
    """Имена из личных чатов Telegram, где было хоть одно сообщение после даты (экспорт result.json из Telegram Desktop)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    chats = (data.get("chats") or {}).get("list") or data.get("chats") or []
    active = []
    for chat in chats if isinstance(chats, list) else []:
        if chat.get("type") not in ("personal_chat", None) or not chat.get("name"):
            continue
        last = None
        for msg in chat.get("messages") or []:
            try:
                moment = datetime.fromisoformat(str(msg.get("date")))
            except ValueError:
                continue
            last = moment if last is None or moment > last else last
        if last and last >= since:
            active.append(_tokens(chat["name"]))
    return [t for t in active if t]


def is_recent(card: dict[str, list[str]], name: str, since: datetime | None, tg: list[frozenset[str]]) -> bool:
    if since is None:
        return True
    rev = parse_rev((card.get("REV") or [""])[0])
    if rev and rev >= since:
        return True
    mine = _tokens(name)
    return bool(mine) and any(mine <= chat or (chat <= mine and len(chat) > 1) for chat in tg)


def _phone_digits(facts: dict[str, list[str]]) -> set[str]:
    return {d for d in (re.sub(r"\D", "", p)[-10:] for p in facts.get("phones", [])) if d}


def _distinct_name(name: str, digits: set[str], seen: dict[str, set[str]], folder: Path) -> str | None:
    """Тёзки с разными телефонами получают разные карточки («Айдар», «Айдар (2)»); повтор того же человека — None."""
    candidate, n = name, 1
    while True:
        known = set(seen.get(candidate, set()))
        path = folder / f"{candidate}.md"
        if path.exists():
            try:
                front = vp.parse_front(vp.split_front(path.read_text(encoding="utf-8"))[0])
                known |= {d for d in (re.sub(r"\D", "", p)[-10:] for p in vp.as_list(front.get("phones"))) if d}
            except (OSError, UnicodeDecodeError, TypeError):
                pass
        if candidate not in seen and not path.exists():
            return candidate
        if not known or not digits or known & digits:
            return None if candidate in seen else candidate     # те же телефоны (или нечем различить): тот же человек
        n += 1
        candidate = f"{name} ({n})"


def run(vcf: Path, vault: Path, dry_run: bool = False, now: datetime | None = None,
        since: datetime | None = None, tg: list[frozenset[str]] | None = None) -> dict[str, list[str]]:
    now = now or datetime.now()
    tg = tg or []
    report = {"created": [], "updated": [], "skipped": 0}
    folder = vault / vp.PEOPLE_DIR
    seen: dict[str, set[str]] = {}               # имя -> цифры телефонов уже обработанных контактов
    try:
        raw_text = vcf.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw_text = vcf.read_text(encoding="cp1251", errors="replace")
    for card in parse_vcards(raw_text):
        parsed = card_to_facts(card)
        if not parsed:
            continue
        name, facts = parsed
        if not is_recent(card, name, since, tg):
            report["skipped"] += 1
            continue
        digits = _phone_digits(facts)
        name = _distinct_name(name, digits, seen, folder)
        if name is None:
            continue                              # тот же человек второй раз в файле
        seen.setdefault(name, set()).update(digits)
        path = folder / f"{name}.md"
        if path.exists():
            new_text, changed = vp.merge_into_card(path.read_text(encoding="utf-8"), facts)
            if changed:
                report["updated"].append(f"{name}: {', '.join(changed)}")
                if not dry_run:
                    vp._write(path, new_text)
        else:
            report["created"].append(name)
            if not dry_run:
                folder.mkdir(parents=True, exist_ok=True)
                vp._write(path, vp.new_card(name, facts, now))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Импорт контактов из .vcf в карточки людей.")
    parser.add_argument("vcf", help="файл .vcf")
    parser.add_argument("vault", help="папка хранилища SecondBrain")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=0, help="только контакты, менявшиеся за последние N дней (например 365)")
    parser.add_argument("--telegram", help="result.json из экспорта Telegram Desktop: добавить тех, с кем была переписка за этот срок")
    args = parser.parse_args(argv)
    vcf, vault = Path(args.vcf), Path(args.vault)
    if not vcf.is_file():
        print(f"Файл не найден: {vcf}")
        return 1
    if not vault.is_dir():
        print(f"Папка не найдена: {vault}")
        return 1
    since = datetime.now() - timedelta(days=args.days) if args.days > 0 else None
    tg = []
    if args.telegram:
        try:
            tg = telegram_active(Path(args.telegram), since or datetime.now() - timedelta(days=365))
        except (OSError, ValueError) as exc:
            print(f"Не удалось прочитать экспорт Telegram: {exc}")
            return 1
    report = run(vcf, vault, args.dry_run, since=since, tg=tg)
    prefix = "Было бы: " if args.dry_run else ""
    print(f"{prefix}новых карточек: {len(report['created'])}, дополнено: {len(report['updated'])}, пропущено старых: {report['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
