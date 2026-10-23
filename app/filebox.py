"""Файлы, которые человек присылал боту: «вышли мне файл билета».

Сервер файлы не хранит. Он помнит только короткий идентификатор файла в Telegram (file_id), название заметки и имя
файла. Когда нужно, бот просит Telegram прислать этот файл тому же человеку. Файл при этом уже лежит у Telegram:
человек сам его туда отправил. Идентификаторы живут в настройках, не больше MAX_ITEMS штук, новые вытесняют старые.
Приватные записи («!») не запоминаются. Стереть всё: /forget.
"""
from __future__ import annotations

import hashlib
import json
import re

from .textutil import one_line

SETTING = "filebox"
MAX_ITEMS = 400
SKIP_KINDS = {"голос", "аудио", "кружок"}

_SEND_VERB = r"(?:вышли|пришли|отправь|скинь|покажи|дай|достань|найди|принеси)"      # только повелительное: «выслать…» это дело
_DOC_NOUN = (r"файл\w*|документ\w*|скан\w*|фото\w*|картинк\w*|pdf|пдф|билет\w*|договор\w*|гаранти\w*|чек\w*|квитанци\w*|"
             r"инструкци\w*|справк\w*|страховк\w*|паспорт\w*|полис\w*|талон\w*|акт\w*|счёт\w*|счет\w*|скриншот\w*|вложени\w*")
_REQUEST = re.compile(rf"(?is)^(?:а\s+|и\s+|ну\s+|пожалуйста,?\s+|слушай,?\s+)*{_SEND_VERB}\b(?=.*\b(?:{_DOC_NOUN})\b)")
_STOP = {"мне", "пожалуйста", "файл", "файлы", "документ", "скан", "фото", "картинку", "вложение", "это", "тот", "тот", "ещё",
         "еще", "на", "по", "от", "для", "с", "и", "в", "из", "где", "мой", "моя", "мою", "мои", "последний", "последнюю"}
ONLY_FILE = re.compile(r"(?is)[\s,.;:—-]*(?:только|просто)\s+(?:файл|сохрани\s+файл|запомни\s+файл)\W*$")


def is_request(text: str) -> bool:
    text = text.strip()
    return bool(text) and len(text) <= 160 and "\n" not in text and not text.startswith("/") and bool(_REQUEST.match(text))


def _load(db) -> list[dict]:
    try:
        data = json.loads(db.get_setting(SETTING, "") or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _key(file_id: str) -> str:
    return hashlib.sha1(file_id.encode("utf-8")).hexdigest()[:12]


def remember(db, sources: list[dict], title: str, today: str) -> int:
    """Запоминает идентификаторы файлов вместе с названием заметки. Возвращает, сколько файлов добавлено."""
    items = _load(db)
    known = {i.get("id") for i in items}
    added = 0
    for src in sources:
        if src.get("kind") in SKIP_KINDS or not src.get("file_id") or src["file_id"] in known:
            continue
        items.append({"id": src["file_id"], "kind": src.get("kind", "файл"), "name": one_line(str(src.get("name", "")), 80),
                      "title": one_line(title, 120), "at": today})
        added += 1
    if added:
        db.set_setting(SETTING, json.dumps(items[-MAX_ITEMS:], ensure_ascii=False))
    return added


def forget(db) -> None:
    db.set_setting(SETTING, "")


def _stems(text: str) -> set[str]:
    return {w[:4] for w in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")) if len(w) >= 3}


def search(db, query: str, limit: int = 5) -> list[dict]:
    """Файлы, названия которых совпадают с запросом по корням слов; при равенстве выше самые свежие."""
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", query.lower().replace("ё", "е")) if w not in _STOP and len(w) >= 3]
    wanted = {w[:4] for w in words}
    scored = []
    for pos, item in enumerate(_load(db)):
        have = _stems(f"{item.get('title', '')} {item.get('name', '')}")
        score = len(wanted & have)
        if score:
            scored.append((score, pos, item))
    scored.sort(key=lambda r: (-r[0], -r[1]))
    return [item for _, _, item in scored[:limit]]


def find_by_key(db, key: str) -> dict | None:
    return next((i for i in _load(db) if _key(i.get("id", "")) == key), None)


def send_method(chat_id: int, item: dict) -> dict:
    caption = one_line(item.get("title") or item.get("name") or "", 200)
    if item.get("kind") == "фото":
        return {"method": "sendPhoto", "chat_id": chat_id, "photo": item["id"], "caption": caption}
    return {"method": "sendDocument", "chat_id": chat_id, "document": item["id"], "caption": caption}


def pick_buttons(items: list[dict]) -> list:
    return [[(f"📎 {one_line(i.get('title') or i.get('name') or 'файл', 40)}", f"fb:{_key(i['id'])}")] for i in items]
