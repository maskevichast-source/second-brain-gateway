"""Telegram-вход: бот принимает сообщения и кладёт готовые заметки в очередь.

Сервер не может писать в iCloud, поэтому заметки ждут в очереди (`outbox`), пока их не заберёт
ноутбук (tools/vault_sync.py) или телефон (шорткат). После подтверждения (`/ack`) они удаляются.
Ответы боту отдаются прямо в ответе на webhook, поэтому токену бота на сервере делать нечего.

Диалоги:
- уточнение: ИИ не понял запись, спрашивает, следующий ответ уходит в ту же запись;
- новый человек: упомянут незнакомый, бот спрашивает, кто это, ответ становится карточкой;
- интервью (/interview): бот по очереди расспрашивает о тебе и окружении.
"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from . import diary, interview
from .config import Settings
from .db import Database
from .textutil import one_line, split_private
from .snapshot import rows_from_markdown as snapshot_rows
from .vault import parse_note

logger = logging.getLogger(__name__)

HELP = (
    "Пришли мысль, ссылку или список дел, и я превращу это в заметку для Obsidian.\n\n"
    "• Начни сообщение с «!» или «секрет:», чтобы текст не уходил в ИИ.\n"
    "• Если я переспрошу, ответь одним сообщением или напиши /skip, чтобы сохранить как есть.\n"
    "• Если в записи новый человек, я спрошу, кто это.\n"
    "• /diary — вечерний дневник: расспрошу про день. Или сразу «дневник: текст». Время напоминания: /diary время 22:00.\n"
    "• /interview — расскажу о себе: я задам вопросы про семью, дом, работу, документы. Один блок: /interview дом, список: /interview блоки.\n"
    "• /ask вопрос — отвечу по твоим заметкам. /day — сводка дня, /week — обзор недели, "
    "/brief Имя — что знать перед встречей, /budget — бюджет из «Ады».\n"
    "• /health — проверка базы (дубли, зависшее). /merge — покажу дубли и что с чем слить. /remember — вспомню старые идеи и хотелки.\n"
    "• /push — что я присылаю сам: утренняя сводка, напоминания в срок, обзор недели (настройка и отключение).\n"
    "• /people — кого я знаю. /status — сколько заметок ждут выгрузки. /sync — состояние среза, /forget — стереть срез.\n\n"
    "Голосовые сообщения расшифровываю (если подключён сервис речи, README часть 10). Фото пока не принимаю: преврати в текст, на iPhone есть «Скопировать текст» с фото."
)

Processor = Callable[..., Awaitable[dict]]
_URL = re.compile(r"https?://", re.I)


def _wrap(text: str, wrapper) -> str:
    """Добавляет контекст к ответу, но сохраняет режим «без ИИ»: ответ с «!» остаётся приватным."""
    private, stripped = split_private(text)
    return "! " + wrapper(stripped) if private else wrapper(text)


def _reply(chat_id: int, text: str) -> dict:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}


def _summary(response: dict) -> str:
    files = response.get("files", [])
    if response.get("count", 1) == 0:
        return "🤷 Сохранять нечего: конкретных фактов не нашёл."
    if response.get("private"):
        head = "🔒 Сохранил без ИИ."
    elif response.get("fallback"):
        head = "⚠️ ИИ недоступен, сохранил как есть."
    else:
        head = f"✅ {response.get('message', 'Записал')}"
    if len(files) > 1:
        head = f"✅ Заметок: {len(files)}"
        head += "\n" + "\n".join(f"• {f['title']}" for f in files[:10])
    count = response.get("reminders_count", 0)
    if count:
        head += f"\n⏰ Напоминаний и событий для iPhone: {count} (создам, когда телефон заберёт очередь)"
    return head + "\nВ хранилище появится, когда ноутбук или телефон заберёт очередь."


def _learn_people(db: Database, response: dict) -> None:
    """Люди из карточек, которые только что создались, попадают в список известных вместе с ролью."""
    for f in response.get("files", []):
        if f.get("type") != "person":
            continue
        note = parse_note(f["markdown"], 1)
        role = str(note.get("relation") or note.get("relation_group") or "").strip()
        db.upsert_person(note.title, role)


async def handle_update(
    update: dict, *, settings: Settings, db: Database, process: Processor, secretary=None, clock=None, transcriber=None,
) -> dict | None:
    """Возвращает ответ для Telegram (метод sendMessage) или None, если отвечать не нужно."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return _callback(callback, settings, db, clock)
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    user_id = (message.get("from") or {}).get("id")
    if not isinstance(chat_id, int) or not isinstance(user_id, int):
        return None

    allowed = settings.telegram_allowed_ids
    if not allowed:
        return _reply(chat_id, f"Бот ещё не настроен. Твой Telegram ID: {user_id}. "
                               "Впиши его в переменную TELEGRAM_ALLOWED_USER_IDS на Railway.")
    if user_id not in allowed:
        return None

    update_id = update.get("update_id")
    dedupe_key = f"tgu-{update_id}" if isinstance(update_id, int) else None
    if dedupe_key:
        cached = db.get(dedupe_key, 1)
        if cached:
            return cached

    reply = await _answer(message, chat_id, update_id, settings=settings, db=db, process=process,
                          secretary=secretary, clock=clock, transcriber=transcriber)
    if dedupe_key and reply:
        db.save(capture_id=dedupe_key, revision=1, response=reply)
    return reply


async def _save(db: Database, process: Processor, *, capture_id: str, content: str, revision: int,
                clarifications: str = "") -> dict:
    response = await process(capture_id=capture_id, client="telegram", content=content,
                             revision=revision, clarifications=clarifications)
    if not response.get("needs_review") or revision != 1:
        db.enqueue(capture_id, response["files"])
        _learn_people(db, response)
    return response


def _ask_person(chat_id: int, db: Database, names: list[str], prefix: str) -> str:
    """Запоминает новых людей и возвращает текст вопроса про первого из них."""
    for name in names:
        db.upsert_person(name)               # чтобы не переспрашивать про того же человека снова
    current, rest = names[0], names[1:4]
    db.set_state(chat_id, "person", {"current": current, "rest": rest})
    return (f"{prefix}\n\nКто такой(ая) {current}? Напиши роль и что знаешь (телефон, где работает, "
            "день рождения) одним сообщением или /skip.")


async def _answer_text(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database, process: Processor,
                       secretary=None, clock=None) -> dict:
    now = clock() if clock else _now(settings)
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return _reply(chat_id, "Принимаю текст, ссылки, голосовые, пересылки и фото/файлы с подписью. " + HELP.split("\n\n")[-1])

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    m = re.match(r"(?is)^дневник\s*[:.,\-—]\s*(.+)$", text)
    if m and not command:
        return await _diary_save(chat_id, update_id, {0: m.group(1).strip()}, settings, db, process, now=now)
    if command in ("/start", "/help"):
        return _reply(chat_id, HELP)
    if command == "/push":
        return _reply(chat_id, _push_command(text, db, settings))
    if command in ("/ask", "/day", "/week", "/brief", "/budget", "/health", "/remember", "/merge"):
        arg = text[len(text.split()[0]):].strip()
        if secretary is None:
            return _reply(chat_id, "Эта команда пока недоступна.")
        if command in ("/health", "/remember", "/merge"):
            arg = ""
        if command == "/ask" and not arg:
            return _reply(chat_id, "Напиши вопрос после команды, например: /ask что я обещал Серику?")
        if command == "/brief" and not arg:
            return _reply(chat_id, "С кем встреча? Например: /brief Серик")
        return _reply(chat_id, await secretary(command[1:], arg[:1000]))
    if command in ("/diary", "/дневник"):
        return await _diary_command(chat_id, update_id, text, settings, db, process, now)
    if command == "/forget":
        removed = db.snapshot_clear()
        return _reply(chat_id, f"Стёр срез хранилища на сервере ({removed} записей). Новые записи снова попадут в него; "
                               "полную копию вернёт шорткат «Синхронизировать».")
    if command == "/sync":
        count, synced = db.snapshot_info()
        when = f"последняя полная синхронизация: {synced[:16].replace('T', ' ')} UTC" if synced else "полной синхронизации ещё не было"
        return _reply(chat_id, f"В срезе заметок: {count}; {when}.")
    if command == "/status":
        return _reply(chat_id, f"Ждут выгрузки в хранилище: {db.pending_count()}")
    if command == "/people":
        known = db.known_people()
        if not known:
            return _reply(chat_id, "Пока никого не знаю. Люди появятся из записей, интервью и профиля с ноутбука.")
        rows = [f"• {p['name']}" + (f" — {p['role']}" if p["role"] else "") for p in known[:60]]
        return _reply(chat_id, f"Знаю людей: {len(known)}\n" + "\n".join(rows))
    if command == "/stop":
        state = db.get_state(chat_id)
        if state and state[0] == "interview":
            db.set_state(chat_id, "interview", {**state[1], "paused": True})
            return _reply(chat_id, "Остановил. /interview продолжит с того же места.")
        db.clear_state(chat_id)
        return _reply(chat_id, "Ок, остановил.")
    if command == "/interview":
        db.clear_waiting(chat_id)
        return _interview_start(chat_id, db, " ".join(text.split()[1:]))

    state = db.get_state(chat_id)
    if state and state[0] == "interview" and state[1].get("paused"):
        state = None                          # интервью на паузе: сообщения обрабатываются как обычные записи
    if state and state[0] == "diary" and command in ("", "/skip", "/done"):
        return await _diary_step(chat_id, update_id, text, command, state[1], settings, db, process, now)
    # Ответ на вопрос про нового человека или интервью. Длинное сообщение или ссылка считаются новой записью.
    if state and not (command == "" and (len(text) > 400 or _URL.search(text))) and command in ("", "/skip"):
        kind, data = state
        if kind == "interview":
            return await _interview_step(chat_id, update_id, text, command, data, db, process, settings, now)
        if kind == "person":
            return await _person_step(chat_id, update_id, text, command, data, db, process, settings)
    if state and command == "" and state[0] == "person":
        db.clear_state(chat_id)               # ушёл в новую запись: вопрос про человека снимаем (интервью остаётся)

    waiting = db.get_waiting(chat_id)
    if command == "/skip":
        if not waiting:
            return _reply(chat_id, "Сейчас нечего пропускать.")
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], "", 2
    elif command:
        return _reply(chat_id, "Не знаю такой команды. " + HELP.split("\n\n")[0])
    elif waiting:
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], text, 2
    else:
        capture_id, content, clarifications, revision = f"tg-{update_id}", text, "", 1

    if len(content) > settings.max_content_chars or len(clarifications) > settings.max_content_chars:
        return _reply(chat_id, f"Слишком длинно: максимум {settings.max_content_chars} символов.")

    response = await _save(db, process, capture_id=capture_id, content=content, revision=revision,
                           clarifications=clarifications)
    if revision == 2:
        db.clear_waiting(chat_id)
    if response.get("needs_review") and revision == 1:
        db.set_waiting(chat_id, capture_id, content)
        questions = "\n".join(f"• {q}" for q in response.get("review_questions", []))
        return _reply(chat_id, f"Нужно уточнение:\n{questions}\n\nОтветь одним сообщением или /skip.")
    summary = _summary(response)
    new = [n for n in response.get("new_people", []) if not response.get("private")]
    if new:
        return _reply(chat_id, _ask_person(chat_id, db, new[:4], summary))
    return _reply(chat_id, summary)


def _forward_name(message: dict) -> str:
    origin = message.get("forward_origin") or {}
    if origin:
        user = origin.get("sender_user") or {}
        name = (" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
                or origin.get("sender_user_name") or (origin.get("chat") or {}).get("title") or "")
    else:  # старый формат Bot API
        user = message.get("forward_from") or {}
        name = (" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
                or message.get("forward_sender_name") or (message.get("forward_from_chat") or {}).get("title") or "")
    return name.strip() or ("кого-то" if origin or message.get("forward_date") else "")


def _decorate(message: dict) -> dict:
    """Пересылки и вложения превращаются в обычный текст записи (без вопросов и без распознавания картинок)."""
    text = (message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/"):
        return message
    kind = ("фото" if message.get("photo") else "файл" if message.get("document") else
            "видео" if message.get("video") else "")
    if kind:
        doc = message.get("document") or message.get("video") or {}
        label = f"{kind} «{doc['file_name']}»" if doc.get("file_name") else kind
        text = f"{text}\n(вложение в Telegram: {label})" if text else f"Вложение в Telegram: {label}"
    if message.get("forward_origin") or message.get("forward_date") or message.get("forward_from"):
        who = _forward_name(message)
        text = f"Переслано от {who}: {text}" if text else f"Переслано от {who}"
    return {**message, "text": text, "caption": None} if text else message


async def _answer(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database, process: Processor,
                  secretary=None, clock=None, transcriber=None) -> dict:
    """Голосовые превращаются в текст и идут дальше как обычные сообщения."""
    message = _decorate(message)
    voice = message.get("voice") or message.get("audio") or message.get("video_note")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not voice or text:
        return await _answer_text(message, chat_id, update_id, settings=settings, db=db, process=process,
                                  secretary=secretary, clock=clock)
    if transcriber is None:
        return _reply(chat_id, "Голосовые пока не включены: нужен ключ сервиса распознавания речи (STT_API_KEY, README, часть 10). "
                               "Пока можно продиктовать текст клавиатурой iPhone (значок микрофона).")
    if int(voice.get("duration") or 0) > 300:
        return _reply(chat_id, "Голосовое длиннее 5 минут: раздели его на части.")
    transcript = await transcriber(voice.get("file_id", ""))
    if not transcript:
        return _reply(chat_id, "Не смог расшифровать голосовое. Попробуй ещё раз или напиши текстом.")
    reply = await _answer_text({**message, "text": transcript}, chat_id, update_id, settings=settings, db=db,
                               process=process, secretary=secretary, clock=clock)
    echo = f"🎙 «{one_line(transcript, 400)}»\n\n"
    return {**reply, "text": (echo + str(reply.get("text", "")))[:4000]}


# ---------- новый человек ----------

async def _person_step(chat_id, update_id, text, command, data, db, process, settings) -> dict:
    current, rest = data["current"], data.get("rest", [])
    prefix = ""
    if command != "/skip":
        content = _wrap(text, lambda t: f"Данные о человеке по имени {current} (карточка нового знакомого, тип person): {t}")
        response = await _save(db, process, capture_id=f"tgp-{update_id}", content=content, revision=2)
        prefix = f"✅ Карточка: {current}"
        if response.get("private"):
            prefix = f"🔒 {current}: сохранил без ИИ"
    else:
        prefix = f"Ок, пропустил {current}."
    if rest:
        return _reply(chat_id, _ask_person(chat_id, db, rest, prefix))
    db.clear_state(chat_id)
    return _reply(chat_id, prefix)


# ---------- интервью ----------

def _queue(data: dict) -> list[int]:
    queue = data.get("queue")
    if isinstance(queue, list) and queue and all(isinstance(i, int) and 0 <= i < len(interview.QUESTIONS) for i in queue):
        return queue
    return list(range(len(interview.QUESTIONS)))


def _interview_start(chat_id: int, db: Database, arg: str = "") -> dict:
    state = db.get_state(chat_id)
    old = state[1] if state and state[0] == "interview" else {}
    arg = arg.strip().lower()
    if arg in ("блоки", "темы", "список"):
        return _reply(chat_id, interview.blocks_text())
    if arg:
        if arg in ("все", "всё", "заново", "сначала"):
            data = {"i": 0, "saved": old.get("saved", 0)}
        else:
            block = interview.find_block(arg)
            if not block:
                return _reply(chat_id, "Не знаю такого блока.\n\n" + interview.blocks_text())
            data = {"i": 0, "saved": old.get("saved", 0), "queue": interview.block_indices(block)}
    else:
        data = dict(old) if old else {"i": 0, "saved": 0}
    queue = _queue(data)
    position = int(data.get("i", 0))
    if position >= len(queue):
        position = 0
    data.update({"i": position, "paused": False})
    db.set_state(chat_id, "interview", data)
    intro = interview.INTRO.format(total=len(queue)) if position == 0 else "Продолжаем интервью."
    return _reply(chat_id, f"{intro}\n\n{interview.question_text(queue[position], position, len(queue))}")


async def _interview_step(chat_id, update_id, text, command, data, db, process, settings, now=None) -> dict:
    queue = _queue(data)
    position, saved = int(data.get("i", 0)), int(data.get("saved", 0))
    index = queue[min(position, len(queue) - 1)]
    qa = list(data.get("qa") or [])                       # [блок, вопрос, ответ] для сводной заметки блока
    note = ""
    if command != "/skip":
        is_private, plain = split_private(text)
        if interview.is_non_answer(plain):
            note = "👌 Понял, пропускаю (в базу не пишу).\n"
        else:
            content = _wrap(text, lambda t: interview.answer_as_content(index, t))
            if len(content) > settings.max_content_chars:
                return _reply(chat_id, f"Слишком длинно: максимум {settings.max_content_chars} символов. Раздели на части.")
            response = await _save(db, process, capture_id=f"tgi-{update_id}", content=content, revision=2)
            count = response.get("count", 0)
            saved += count
            if not is_private:                            # приватные ответы не копим даже в сводной заметке блока
                qa.append([interview.QUESTIONS[index][0], interview.QUESTIONS[index][1], plain.strip()])
            titles = [f["title"] for f in response.get("files", [])][:6]
            note = ("✅ Записал: " + "; ".join(titles) + "\n") if titles else "📝 Ответ учтён, новых отдельных заметок нет.\n"
    position += 1
    finished = position >= len(queue)
    next_block = None if finished else interview.QUESTIONS[queue[position]][0]
    # когда блок закончился, собираем его сырые ответы в одну заметку
    current_block = interview.QUESTIONS[index][0]
    if (finished or next_block != current_block) and any(row[0] == current_block for row in qa):
        rows = [(q, a) for blk, q, a in qa if blk == current_block]
        qa = [row for row in qa if row[0] != current_block]
        file = interview.block_file(current_block, rows, now or _now(settings), f"tgib-{update_id}")
        db.enqueue(f"tgib-{update_id}", [file])
        db.snapshot_put(snapshot_rows(file["markdown"]), settings.snapshot_max_rows)
        note += f"📚 Блок «{current_block}» собран в одну заметку.\n"
    if finished:
        db.clear_state(chat_id)
        return _reply(chat_id, f"{note}Интервью закончено. Всего заметок: {saved}. "
                               "Когда телефон или ноутбук заберёт очередь, они появятся в хранилище. "
                               "Другие блоки: /interview блоки.")
    new = {"i": position, "saved": saved, "qa": qa}
    if "queue" in data:
        new["queue"] = queue
    db.set_state(chat_id, "interview", new)
    return _reply(chat_id, f"{note}\n{interview.question_text(queue[position], position, len(queue))}")


# ---------- дневник ----------

async def _diary_command(chat_id, update_id, text, settings, db, process, now) -> dict:
    arg = " ".join(text.split()[1:]).strip()
    low = arg.lower()
    if low in ("выкл", "off", "выключить"):
        db.set_setting("diary_on", "0")
        return _reply(chat_id, "Вечернее напоминание выключено. Включить: /diary вкл.")
    if low in ("вкл", "on", "включить"):
        db.set_setting("diary_on", "1")
        return _reply(chat_id, _diary_status(db, settings))
    if low.startswith("время") or low.startswith("time"):
        value = diary.parse_time(low.split(maxsplit=1)[1]) if len(low.split(maxsplit=1)) == 2 else None
        if not value:
            return _reply(chat_id, "Напиши время так: /diary время 21:30")
        db.set_setting("diary_time", value)
        db.set_setting("diary_on", "1")
        return _reply(chat_id, _diary_status(db, settings))
    if low in ("статус", "status"):
        return _reply(chat_id, _diary_status(db, settings))
    if arg:
        return await _diary_save(chat_id, update_id, {0: arg}, settings, db, process, now=now)
    day = diary.entry_date(now)
    db.set_state(chat_id, "diary", {"i": 0, "answers": {}, "day": day.isoformat(), "private": False})
    return _reply(chat_id, f"{diary.INTRO}\n\n{diary.question_text(0)}")


def _now(settings: Settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.timezone))


def _diary_status(db: Database, settings: Settings) -> str:
    on = db.get_setting("diary_on", "1") == "1"
    at = db.get_setting("diary_time", "") or diary.DEFAULT_TIME
    if not on:
        return "Вечернее напоминание выключено. Включить: /diary вкл."
    tail = "" if settings.telegram_bot_token else (
        "\n⚠️ Сам написать первым я смогу, когда на сервере появится токен бота (TELEGRAM_BOT_TOKEN, README часть 5). "
        "Пока можно поставить повторяющееся напоминание в iPhone (README, часть 9).")
    return f"Напоминание про дневник: каждый вечер в {at} (если записи за день ещё нет).{tail}"


async def _diary_step(chat_id, update_id, text, command, data, settings, db, process, now) -> dict:
    index = int(data.get("i", 0))
    answers = {int(k): v for k, v in (data.get("answers") or {}).items()}
    private = bool(data.get("private"))
    if command == "":
        is_private, stripped = split_private(text)
        private = private or is_private
        answers[index] = stripped if is_private else text
    if command == "/done" or index + 1 >= len(diary.QUESTIONS):
        return await _diary_save(chat_id, update_id, answers, settings, db, process,
                                 day=data.get("day"), private=private, now=now)
    db.set_state(chat_id, "diary", {"i": index + 1, "answers": answers, "day": data.get("day"), "private": private})
    return _reply(chat_id, diary.question_text(index + 1))


async def _diary_save(chat_id, update_id, answers, settings, db, process, *, now, day=None, private=False) -> dict:
    from datetime import date
    db.clear_state(chat_id)
    if not any(v.strip() for v in answers.values()):
        return _reply(chat_id, "Пусто, ничего не записал. /diary начнёт заново.")
    when = date.fromisoformat(day) if day else diary.entry_date(now)
    content = diary.compile_entry(when, answers, private)
    if len(content) > settings.max_content_chars:
        return _reply(chat_id, f"Слишком длинно: максимум {settings.max_content_chars} символов. Раздели на части.")
    response = await _save(db, process, capture_id=f"tgd-{update_id}", content=content, revision=2)
    db.set_setting("diary_last_sent", now.date().isoformat())      # сегодня напоминать уже не нужно
    return _reply(chat_id, "📓 Дневник записан. " + _summary(response))


# ---------- сообщения, которые бот присылает сам: настройка и кнопки ----------

def _push_command(text: str, db: Database, settings: Settings) -> str:
    from . import scheduler

    parts = text.lower().split()[1:]
    cfg = scheduler.push_config(db)
    if parts[:1] == ["утро"] and len(parts) >= 2:
        if parts[1] in ("выкл", "off"):
            db.set_setting("push_morning_on", "0")
        elif parts[1] in ("вкл", "on"):
            db.set_setting("push_morning_on", "1")
        else:
            value = diary.parse_time(parts[1])
            if not value:
                return "Время пиши так: /push утро 08:30"
            db.set_setting("push_morning_time", value)
            db.set_setting("push_morning_on", "1")
    elif parts[:1] == ["неделя"] and len(parts) >= 2 and parts[1] in ("выкл", "вкл", "on", "off"):
        db.set_setting("push_week_on", "1" if parts[1] in ("вкл", "on") else "0")
    elif parts[:1] == ["напоминания"] and len(parts) >= 2 and parts[1] in ("выкл", "вкл", "on", "off"):
        db.set_setting("push_rem_on", "1" if parts[1] in ("вкл", "on") else "0")
    elif parts:
        return ("Команды: /push утро 08:30, /push утро выкл, /push неделя выкл, /push напоминания выкл. "
                "Дневник отдельно: /diary время 21:00, /diary выкл.")
    cfg = scheduler.push_config(db)
    d_on, d_at = scheduler.get_diary_config(db)
    yes = lambda v: "вкл" if v else "выкл"  # noqa: E731
    tail = "" if settings.telegram_bot_token else "\n⚠️ Пока на сервере нет токена бота, сам я писать не могу."
    return ("Что я присылаю сам:\n"
            f"• утренняя сводка: {yes(cfg['morning_on'])}, {cfg['morning_time']}\n"
            f"• напоминания в срок (с кнопками «через час»): {yes(cfg['rem_on'])}\n"
            f"• обзор недели (воскресенье, 19:00): {yes(cfg['week_on'])}\n"
            f"• дневник вечером: {yes(d_on)}, {d_at}" + tail)


def _callback(callback: dict, settings: Settings, db: Database, clock) -> dict | None:
    from datetime import timedelta

    user_id = (callback.get("from") or {}).get("id")
    if user_id not in settings.telegram_allowed_ids:
        return None
    data = str(callback.get("data") or "")
    action, _, key = data.partition(":")
    now = clock() if clock else _now(settings)
    if not re.fullmatch(r"[0-9a-f]{12}", key):
        toast = "Не понял кнопку"
    elif action == "ok":
        db.set_setting(f"rem_{key}", "done")
        toast = "Ок, больше не напомню"
    elif action == "snz1":
        db.set_setting(f"rem_{key}", (now + timedelta(hours=1)).replace(tzinfo=None, microsecond=0).isoformat(timespec="minutes"))
        toast = "Напомню через час"
    elif action == "snz24":
        tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=None)
        db.set_setting(f"rem_{key}", tomorrow.isoformat(timespec="minutes"))
        toast = "Напомню завтра в 9:00"
    else:
        toast = "Не понял кнопку"
    return {"method": "answerCallbackQuery", "callback_query_id": callback.get("id", ""), "text": toast}
