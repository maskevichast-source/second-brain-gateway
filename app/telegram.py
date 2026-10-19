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

import asyncio
import logging
import re
from typing import Awaitable, Callable

from . import actions as actions_module
from . import diary, interview
from . import when as when_module
from . import sources as sources_module
from .config import Settings
from .db import Database
from .textutil import one_line, split_private
from .snapshot import rows_from_markdown as snapshot_rows
from .vault import parse_note

logger = logging.getLogger(__name__)

HELP = (
    "Пришли мысль, ссылку или список дел, и я превращу это в заметку для Obsidian.\n\n"
    "• Начни сообщение с «!» или «секрет:», чтобы текст не уходил в ИИ.\n"
    "• Если я переспрошу, ответь одним сообщением или напиши /skip (или «не знаю», «как есть»), чтобы сохранить как есть. Не ответишь за 30 минут — сохраню сам.\n"
    "• Если в записи новый человек, я спрошу, кто это.\n"
    "• /diary — вечерний дневник: расспрошу про день. Или сразу «дневник: текст». Время напоминания: /diary время 22:00.\n"
    "• /interview — расскажу о себе: я задам вопросы про семью, дом, работу, документы. Один блок: /interview дом, список: /interview блоки. Посреди интервью можно писать «повтори», «назад», «сколько осталось», «стоп»; /interview 22 — с вопроса 22.\n"
    "• /ask вопрос — отвечу по твоим заметкам. /day — сводка дня, /week — обзор недели, "
    "/who Имя (или /brief) — что знать перед встречей, /promises — кто что кому обещал, /radar — с кем давно не общались, /budget — бюджет из «Ады».\n"
    "• /health — проверка базы (дубли, зависшее). /merge — покажу дубли: что оставить, что объединить; /merge 1 — файл-подсказка для ручного слияния. /remember — вспомню старые идеи и хотелки.\n"
    "• /push — что я присылаю сам: утренняя сводка, напоминания в срок, обзор недели (настройка и отключение).\n"
    "• /fact текст — короткий факт для меня, например «Собаку зовут Янка» (имена, клички, места): учту в записях и в расшифровке голосовых.\n"
    "• /requeue 6 — если заметки забрали, но они не сохранились: верну в очередь заметки за последние 6 часов (не дольше 24).\n"
    "• /import дом — перенесу из «Ады» список «Дом»: техника, растения, фильтры и когда за ними ухаживать.\n"
    "• /people — кого я знаю. /status — очередь, срез, сбои. /sync — состояние среза, /forget — стереть срез, /backup — файл с копией профиля и среза.\n\n"
    "Голосовые сообщения расшифровываю (если подключён сервис речи, README часть 10). PDF с текстом читаю и записываю как обычную запись. "
    "Фото и сканы сохраняю с подписью; текст с картинки распознаю, только если включено (VISION_MODEL), иначе честно скажу, что не распознан."
)

Processor = Callable[..., Awaitable[dict]]
WAITING_PATIENCE_MIN = 30                  # столько минут бот ждёт ответ на уточнение, потом сохраняет запись как есть
_LOCKS: dict[int, asyncio.Lock] = {}       # сообщения одного чата обрабатываются по очереди: место в диалоге не «гоняется»
_TASKISH = re.compile(r"^(купи|купить|заказ|позвони|позвонить|напомни|оплати|оплатить|записаться|забрать|отправить|"
                      r"сделать|проверить|завтра|сегодня|послезавтра|не забыть)", re.I)
_SKIP_PHRASE = re.compile(r"^(пропусти|пропустим|сохрани как есть|как есть|оставь как есть|не надо|не нужно)\W*$", re.I)
_URL = re.compile(r"https?://", re.I)


def _wrap(text: str, wrapper) -> str:
    """Добавляет контекст к ответу, но сохраняет режим «без ИИ»: ответ с «!» остаётся приватным."""
    private, stripped = split_private(text)
    return "! " + wrapper(stripped) if private else wrapper(text)


def _reply(chat_id: int, text: str, buttons: list | None = None) -> dict:
    reply = {"method": "sendMessage", "chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        reply["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    return reply


WHEN_HELP = "Напиши, когда: «через 2 часа», «в 18:30», «завтра утром», «в пятницу», «25 октября»."


def _reschedule_reply(db: Database, picked: list, target, now) -> str:
    when, has_time = target
    lines = []
    for key, title in picked:
        code, name = actions_module.reschedule(db, key, when, has_time, now)
        name = name or title
        said = when_module.human(when, has_time, now)
        lines.append({"ok": f"⏰ Перенёс: {name} — {said}." + (" Напомню в Telegram." if has_time else ""),
                      "already": f"Уже закрыто: {name}", "unsupported": f"Для «{name}» перенос не поддерживается",
                      "missing": f"Не нашёл дело «{title}» (срез обновился)"}[code])
    return "\n".join(lines)


def _snooze_action(chat_id: int, rest: str, db: Database, now) -> dict | None:
    """«отложи 3 на 2 часа» переносит сразу; «отложи 3» показывает варианты, подходящие ко времени суток."""
    m = None if re.fullmatch(r"[\d\s,;и.]+", rest) else re.match(r"^(\d+(?:[\s,;и]+\d+)*)\s+(\S.*)$", rest)
    if m:
        target = when_module.parse_when(m.group(2), now)
        if not target:
            return _reply(chat_id, "Не понял, на когда. " + WHEN_HELP)
        picked, problem = actions_module.resolve(db, m.group(1), now)
        if problem:
            return _reply(chat_id, problem)
        return _reply(chat_id, _reschedule_reply(db, picked, target, now))
    picked, problem = actions_module.resolve(db, rest, now)
    if problem:
        return _reply(chat_id, problem)
    if not picked:
        return None
    if len(picked) > 1:
        return _reply(chat_id, "Выбери одно дело, например: отложи 3. Или сразу с временем: отложи 1 и 3 на завтра.")
    shown = actions_module.panel(db, picked[0][0], now)
    if not shown:
        return _reply(chat_id, f"Не нашёл дело «{picked[0][1]}» (срез обновился)")
    return _reply(chat_id, *shown)


def _task_action(chat_id: int, action: str, rest: str, db: Database, now) -> dict | None:
    """«сделано 1 и 3», «отмена купить молоко», «отложи 2». None: это не про дела, сообщение идёт как обычная запись."""
    if action == "snooze":
        return _snooze_action(chat_id, rest, db, now)
    picked, problem = actions_module.resolve(db, rest, now)
    if problem:
        return _reply(chat_id, problem)
    if not picked:
        return None
    lines = []
    for key, title in picked:
        code, name = actions_module.apply(db, key, action, now)
        name = name or title
        lines.append({"ok": f"{actions_module.TOAST[action]}: {name}", "already": f"Уже закрыто: {name}",
                      "unsupported": f"Для «{name}» это не поддерживается", "missing": f"Не нашёл дело «{title}» (срез обновился)"}[code])
    return _reply(chat_id, "\n".join(lines) + "\n\nВ хранилище изменение попадёт, когда ноутбук заберёт очередь. Обновить список: /tasks")


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
    backup_sender=None, file_fetcher=None, vision=None,
) -> dict | None:
    """Возвращает ответ для Telegram (метод sendMessage) или None, если отвечать не нужно."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        reply = _callback(callback, settings, db, clock)
        if reply and reply.get("method") != "answerCallbackQuery":
            await _ack_callback(settings, callback)
        return reply
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
    async with _LOCKS.setdefault(chat_id, asyncio.Lock()):
        if dedupe_key:
            cached = db.get(dedupe_key, 1)
            if cached:
                return cached
        reply = await _answer(message, chat_id, update_id, settings=settings, db=db, process=process,
                              secretary=secretary, clock=clock, transcriber=transcriber, backup_sender=backup_sender,
                              file_fetcher=file_fetcher, vision=vision)
        if dedupe_key and reply:
            db.save(capture_id=dedupe_key, revision=1, response=reply)
        return reply


async def flush_stale_waiting(db: Database, process: Processor, notify=None, minutes: float = WAITING_PATIENCE_MIN) -> int:
    """Запись, на уточнение которой не ответили, сохраняется как есть: молчание не должно терять заметки."""
    saved = 0
    for row in db.stale_waiting(minutes):
        async with _LOCKS.setdefault(row["chat_id"], asyncio.Lock()):
            current = db.get_waiting(row["chat_id"])
            if not current or current["capture_id"] != row["capture_id"]:
                continue                                  # пока ждали очередь, пользователь успел ответить
            sources_module.CURRENT.set(row["sources"])
            response = await _save(db, process, capture_id=row["capture_id"], content=row["content"], revision=2)
            db.clear_waiting(row["chat_id"])
            saved += 1
            if notify:
                title = (response.get("files") or [{}])[0].get("title", "запись")
                await notify(row["chat_id"], f"⏳ Не дождался уточнения и сохранил «{title}» как есть. "
                                             "Поправить можно в Obsidian.")
    return saved


async def _save(db: Database, process: Processor, *, capture_id: str, content: str, revision: int,
                clarifications: str = "") -> dict:
    response = await process(capture_id=capture_id, client="telegram", content=content,
                             revision=revision, clarifications=clarifications)
    if not response.get("needs_review") or revision != 1:
        sources = sources_module.CURRENT.get()
        files = response["files"]
        if sources and files:
            planned = sources_module.plan(sources, capture_id, files[0]["filename"][:10])
            for f in files:
                f["markdown"] = sources_module.attach(f["markdown"], planned)
            db.enqueue_files(capture_id, planned)
        db.enqueue(capture_id, files)
        _learn_people(db, response)
    return response


def _leave_person(chat_id: int, db: Database, resume: dict | None, paused: bool = False) -> None:
    """Снимает вопрос про человека и возвращает прерванное интервью на то же место."""
    if resume:
        db.set_state(chat_id, "interview", {**resume, "paused": paused or resume.get("paused", False)})
    else:
        db.clear_state(chat_id)


def _ask_person(chat_id: int, db: Database, names: list[str], prefix: str, batch: bool | None = None) -> str:
    """Запоминает новых людей и возвращает вопрос: про одного или сразу про всех новых из записи."""
    for name in names:
        db.upsert_person(name)               # чтобы не переспрашивать про того же человека снова
    batch = len(names) > 1 if batch is None else batch
    current, rest = names[0], names[1:4]
    state = db.get_state(chat_id)
    resume = None                            # интервью не должно теряться, пока идёт вопрос про человека
    if state and state[0] == "interview":
        resume = state[1]
    elif state and state[0] == "person":
        resume = state[1].get("resume")
    data = {"current": current, "rest": rest}
    if batch and rest:
        data["batch"] = True
    if resume:
        data["resume"] = resume
    db.set_state(chat_id, "person", data)
    if batch and rest:
        return (f"{prefix}\n\nНовые люди: {', '.join(names[:4])}. Напиши про каждого роль и что знаешь (телефон, где работает, "
                "день рождения) одним сообщением, можно только про некоторых. /skip — пропустить всех.")
    return (f"{prefix}\n\nКто такой(ая) {current}? Напиши роль и что знаешь (телефон, где работает, "
            "день рождения) одним сообщением или /skip.")


def _stem(name: str) -> str:
    return re.sub(r"[^а-яa-z0-9]", "", name.lower().replace("ё", "е"))[:4]


async def _answer_text(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database, process: Processor,
                       secretary=None, clock=None, backup_sender=None) -> dict:
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
    command = {"/кто": "/who", "/радар": "/radar", "/обещания": "/promises", "/долги": "/promises",
               "/дела": "/tasks", "/отмена": "/cancel", "/отложи": "/snooze", "/импорт": "/import"}.get(command, command)
    words = text.split()
    if command in ("/done", "/cancel", "/snooze") and len(words) > 1:           # «/done 2», «/cancel купить молоко»
        diary_state = db.get_state(chat_id)
        if not (command == "/done" and diary_state and diary_state[0] == "diary"):
            done = _task_action(chat_id, command[1:], " ".join(words[1:]), db, now)
            return done or _reply(chat_id, "Не нашёл такое дело. Список: /tasks")
    if not command and len(text) <= 80:               # живая речь вместо команд: «давай дальше интервью», «стоп»
        low = text.lower()
        if "интервью" in low and re.search(r"продолж|дальше|давай|начн|запуст|вернись|вернём|вернемся|вернёмся", low):
            command, text = "/interview", "/interview"
        elif re.match(r"^(стоп|хватит|пауза|остановись|останови|закончим|заканчиваем)\W*$", low):
            current = db.get_state(chat_id)
            if current and current[0] in ("interview", "person", "diary"):
                command, text = "/stop", "/stop"
    if command in ("/ask", "/day", "/tasks", "/import", "/week", "/brief", "/who", "/radar", "/promises", "/budget", "/health", "/remember", "/merge"):
        arg = text[len(text.split()[0]):].strip()
        if secretary is None:
            return _reply(chat_id, "Эта команда пока недоступна.")
        if command in ("/health", "/remember", "/radar", "/promises"):
            arg = ""
        if command == "/merge":
            arg = arg.split()[0] if arg.split() and arg.split()[0].isdigit() else ""
        if command == "/ask" and not arg:
            return _reply(chat_id, "Напиши вопрос после команды, например: /ask что я обещал Серику?")
        if command in ("/brief", "/who") and not arg:
            return _reply(chat_id, "О ком? Например: /who Серик")
        result = await secretary(command[1:], arg[:1000])
        return _reply(chat_id, *result) if isinstance(result, tuple) else _reply(chat_id, result)
    if command == "/done" and len(words) == 1:
        current = db.get_state(chat_id)
        if current and current[0] in ("interview", "person"):
            command, text = "/stop", "/stop"
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
    if command == "/fact":
        fact = text[len(text.split()[0]):].strip()
        if not fact:
            return _reply(chat_id, "Напиши факт после команды, например: /fact Собаку зовут Янка. Я учту это в записях и при расшифровке голосовых.")
        total = db.add_fact(fact)
        return _reply(chat_id, f"Запомнил: {fact[:200]}. Фактов у меня: {total}. Они уходят только в разбор твоих записей, в заметки не пишутся.")
    if command == "/requeue":
        parts = text.split()
        hours = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 6.0
        added = db.requeue_recent(hours)
        return _reply(chat_id, f"Вернул в очередь заметок: {added} (за последние {hours:g} ч). Запусти шорткат или ноутбук, "
                               "они заберут их. Повторно не вызывай: заметки, которые уже ждут в очереди, не дублируются, "
                               "а уже забранные вернутся ещё раз.")
    if command == "/backup":
        return await _backup_command(chat_id, db, now, backup_sender)
    if command == "/status":
        return _reply(chat_id, _status_text(db))
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
        if state and state[0] == "person" and state[1].get("resume"):
            _leave_person(chat_id, db, state[1]["resume"], paused=True)
            return _reply(chat_id, "Остановил. /interview продолжит с того же места.")
        db.clear_state(chat_id)
        return _reply(chat_id, "Ок, остановил.")
    if command == "/interview":
        db.clear_waiting(chat_id)
        return _interview_start(chat_id, db, " ".join(text.split()[1:]))

    state = db.get_state(chat_id)
    if state and state[0] == "interview" and state[1].get("paused"):
        state = None                          # интервью на паузе: сообщения обрабатываются как обычные записи
    if state and state[0] == "resched":                    # бот спросил «на когда перенести?»
        if command == "/skip":
            db.clear_state(chat_id)
            return _reply(chat_id, "Ок, не переношу.")
        if command == "":
            target = when_module.parse_when(text, now)
            if target:
                db.clear_state(chat_id)
                return _reply(chat_id, _reschedule_reply(db, [(state[1]["key"], state[1].get("title", ""))], target, now))
            if len(text) > 60 or _TASKISH.match(text):
                db.clear_state(chat_id)                    # это уже новая запись, а не ответ про время
                state = None
            else:
                return _reply(chat_id, "Не понял, на когда. " + WHEN_HELP + " Или /skip.")
    if command == "" and not state:                       # «сделано 1», «отмена купить молоко»: управление делами словами
        phrase = actions_module.parse_phrase(text)
        if phrase:
            handled = _task_action(chat_id, phrase[0], phrase[1], db, now)
            if handled:
                return handled
    if state and state[0] == "diary" and command in ("", "/skip", "/done"):
        return await _diary_step(chat_id, update_id, text, command, state[1], settings, db, process, now)
    # Ответ на вопрос про нового человека или интервью. Длинное сообщение или ссылка считаются новой записью.
    in_interview = bool(state and state[0] == "interview")   # в интервью длинный ответ остаётся ответом, а не новой записью
    new_note = bool(state and state[0] == "person" and command == "" and (
        text.startswith("Переслано от") or "вложение в Telegram" in text.lower() or
        (len(text) < 120 and _TASKISH.match(text))))          # пересылка, файл или явное дело: это новая запись, а не ответ про человека
    if state and state[0] == "interview" and command == "" and len(text) <= 60:
        meta = _interview_meta(chat_id, text, state[1], db)
        if meta:
            return meta
    if state and not new_note and not (command == "" and not in_interview and (len(text) > 400 or _URL.search(text))) \
            and command in ("", "/skip"):
        kind, data = state
        if kind == "interview":
            return await _interview_step(chat_id, update_id, text, command, data, db, process, settings, now)
        if kind == "person":
            return await _person_step(chat_id, update_id, text, command, data, db, process, settings)
    if state and command == "" and state[0] == "person":
        _leave_person(chat_id, db, state[1].get("resume"))   # ушёл в новую запись: вопрос про человека снимаем, интервью остаётся

    waiting = db.get_waiting(chat_id)
    expired = ""
    if waiting and command == "":
        if _minutes_ago(waiting.get("created_at")) > WAITING_PATIENCE_MIN:
            mine = sources_module.CURRENT.get()                           # вложения нового сообщения
            sources_module.CURRENT.set(waiting.get("sources") or [])      # не дождались ответа: старую запись сохраняем как есть
            old = await _save(db, process, capture_id=waiting["capture_id"], content=waiting["content"], revision=2)
            db.clear_waiting(chat_id)
            title = (old.get("files") or [{}])[0].get("title", "запись")
            expired = f"⏳ Прошлую запись «{title}» сохранил как есть: уточнения не дождался.\n\n"
            sources_module.CURRENT.set(mine)
            waiting = None
        elif interview.is_non_answer(split_private(text)[1]) or _SKIP_PHRASE.match(text):
            command = "/skip"                                              # «не знаю», «как есть» = сохранить без уточнений
    if command == "/skip":
        if not waiting:
            return _reply(chat_id, "Сейчас нечего пропускать.")
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], "", 2
        if waiting.get("sources"):
            sources_module.CURRENT.set(waiting["sources"])
    elif command:
        return _reply(chat_id, "Не знаю такой команды. " + HELP.split("\n\n")[0])
    elif waiting:
        capture_id, content, clarifications, revision = waiting["capture_id"], waiting["content"], text, 2
        if waiting.get("sources"):
            sources_module.CURRENT.set(waiting["sources"])
    else:
        capture_id, content, clarifications, revision = f"tg-{update_id}", text, "", 1

    if len(content) > settings.max_content_chars or len(clarifications) > settings.max_content_chars:
        return _reply(chat_id, f"Слишком длинно: максимум {settings.max_content_chars} символов.")

    response = await _save(db, process, capture_id=capture_id, content=content, revision=revision,
                           clarifications=clarifications)
    if revision == 2:
        db.clear_waiting(chat_id)
    if response.get("needs_review") and revision == 1:
        db.set_waiting(chat_id, capture_id, content, sources_module.CURRENT.get())
        questions = "\n".join(f"• {q}" for q in response.get("review_questions", []))
        return _reply(chat_id, f"{expired}Нужно уточнение:\n{questions}\n\nОтветь одним сообщением или /skip.")
    summary = expired + _summary(response)
    new = [n for n in response.get("new_people", []) if not response.get("private")]
    if new:
        return _reply(chat_id, _ask_person(chat_id, db, new[:4], summary))
    return _reply(chat_id, summary)


def _minutes_ago(stamp: str | None) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 60
    except (TypeError, ValueError):
        return 0.0


def _status_text(db: Database) -> str:
    from . import monitor

    count, synced = db.snapshot_info()
    last = db.snapshot_last_update()
    stamp = lambda v: v[:16].replace("T", " ") + " UTC" if v else "ещё не было"  # noqa: E731
    rows = [f"Ждут выгрузки в хранилище: {db.pending_count()}",
            f"Служебных записей в очереди (закрыть дело): {db.service_count()}",
            f"В срезе заметок: {count}",
            f"Последний успешный срез: полная синхронизация {stamp(synced)}; последняя запись в срез {stamp(last)}"]
    failures = len(monitor.MONITOR.recent())
    rows.append(f"Сбоев за последний час: {failures}" if failures else "Сбоев за последний час нет")
    return "\n".join(rows)


async def _backup_command(chat_id: int, db: Database, now, backup_sender) -> dict:
    from . import backup

    if backup_sender is None:
        return _reply(chat_id, "Для /backup на сервере нужен токен бота (TELEGRAM_BOT_TOKEN, README часть 5): "
                               "файл я присылаю в личку.")
    filename, data = backup.build_backup(db, now)
    caption = "Резервная копия: профиль, настройки и срез хранилища. Текстов заметок и секретов в файле нет."
    if await backup_sender(chat_id, filename, data, caption):
        return _reply(chat_id, f"Отправил файл {filename} ({len(data) // 1024 + 1} КБ).")
    return _reply(chat_id, "Не получилось отправить файл, попробуй позже.")


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


MAX_NOTICE = 3900


async def _attachment(message: dict, settings: Settings, fetcher, vision) -> tuple[dict, str]:
    """PDF с текстовым слоем превращается в текст записи; картинки по возможности распознаются.

    Возвращает (сообщение для дальнейшей обработки, честная пометка для ответа пользователю).
    """
    from . import files

    doc = message.get("document") or {}
    caption = (message.get("caption") or message.get("text") or "").strip()
    if caption.startswith("/"):
        return message, ""
    private, plain = split_private(caption)
    name = str(doc.get("file_name") or "")
    mime = str(doc.get("mime_type") or "")
    if doc and (mime == "application/pdf" or name.lower().endswith(".pdf")):
        if fetcher is None:
            return message, "ℹ️ Читать PDF не могу: на сервере нет токена бота. Сохранил запись с подписью и пометкой."
        data = await fetcher(doc.get("file_id", ""))
        if data is None:
            return message, "⚠️ Не смог скачать PDF (слишком большой или Telegram не ответил). Сохранил запись с подписью и пометкой."
        text = files.extract_pdf_text(data)
        if not text:
            return message, "⚠️ Текст из PDF не извлёкся (скан без текстового слоя, пароль или битый файл). Сохранил запись с подписью и пометкой."
        head = (plain + "\n\n" if plain else "") + f"Источник: PDF: {name or 'без имени'}\n\n"
        room = max(500, settings.max_content_chars - len(head) - 60)
        cut = text[:min(room, files.MAX_PDF_CHARS)]
        truncated = len(text) > len(cut)
        content = head + cut + ("\n\n(текст PDF обрезан)" if truncated else "")
        content = "! " + content if private else content
        notice = f"📄 Прочитал PDF «{name or 'без имени'}»: {len(cut)} симв." + (", остальное обрезал." if truncated else ".")
        return {**message, "text": content, "caption": None, "document": None}, notice
    is_image = bool(message.get("photo")) or mime.startswith("image/")
    if not is_image:
        return message, ""
    sorry = "🖼 Текст с картинки не распознан: сохранил подпись и пометку о вложении."
    if private:
        return message, sorry + " (В режиме «!» картинки никуда не отправляются.)"
    if vision is None or fetcher is None:
        return message, sorry
    photos = message.get("photo") or []
    file_id = (photos[-1] if photos else doc).get("file_id", "")
    data = await fetcher(file_id)
    text = await vision(data, mime or "image/jpeg") if data else None
    if not text:
        return message, sorry
    label = "фото" if photos else f"файл «{name}»" if name else "картинка"
    content = (caption + "\n" if caption else "") + (
        f"(вложение в Telegram: {label})\nТекст с картинки (распознан ИИ, возможны ошибки): {text}")
    return {**message, "text": content, "caption": None, "photo": None, "document": None}, \
        "🖼 Текст с картинки распознан ИИ: проверь его в заметке."


async def _answer(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database, process: Processor,
                  secretary=None, clock=None, transcriber=None, backup_sender=None, file_fetcher=None,
                  vision=None) -> dict:
    """Вложения читаются (PDF, картинки), затем обычная обработка; к ответу добавляется честная пометка."""
    sources, too_big = sources_module.collect(message)
    sources_module.CURRENT.set(sources if not (message.get("text") or message.get("caption") or "").strip().startswith("/") else [])
    message, notice = await _attachment(message, settings, file_fetcher, vision)
    if sources_module.CURRENT.get():
        notice = (notice + "\n" if notice else "") + "📎 Оригинал сохраню в 7-Исходники, когда ноутбук заберёт очередь."
    if too_big:
        notice = (notice + "\n" if notice else "") + (
            f"⚠️ Файл «{too_big[0]}» больше 20 МБ: Telegram не отдаёт такие файлы боту, оригинал не сохранён. "
            "Закинь его в хранилище вручную.")
    reply = await _answer_core(message, chat_id, update_id, settings=settings, db=db, process=process,
                               secretary=secretary, clock=clock, transcriber=transcriber, backup_sender=backup_sender)
    if notice and reply.get("method") == "sendMessage":
        reply = {**reply, "text": (str(reply.get("text", "")) + "\n" + notice)[:4000]}
    return reply


async def _answer_core(message: dict, chat_id: int, update_id, *, settings: Settings, db: Database,
                       process: Processor, secretary=None, clock=None, transcriber=None, backup_sender=None) -> dict:
    """Голосовые превращаются в текст и идут дальше как обычные сообщения."""
    message = _decorate(message)
    voice = message.get("voice") or message.get("audio") or message.get("video_note")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not voice or text:
        return await _answer_text(message, chat_id, update_id, settings=settings, db=db, process=process,
                                  secretary=secretary, clock=clock, backup_sender=backup_sender)
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
    shown = one_line(transcript, 2500)
    echo = f"🎙 «{shown}»" + ("… (в заметку записано целиком)" if len(transcript) > len(shown) else "") + "\n\n"
    body = str(reply.get("text", ""))
    return {**reply, "text": (echo + body)[:4000] if len(echo) + len(body) <= 4000 else (echo[:1800] + "\n\n" + body)[:4000]}


# ---------- новый человек ----------

async def _person_step(chat_id, update_id, text, command, data, db, process, settings) -> dict:
    current, rest = data["current"], data.get("rest", [])
    batch = bool(data.get("batch"))
    names = [current] + list(rest)
    resume = data.get("resume")
    plain = split_private(text)[1]
    skip = command == "/skip" or interview.is_non_answer(plain)
    skip_all = skip and (batch or bool(re.search(r"\bвс(е|ех)\b|\ball\b", text.lower())))
    left: list[str] = []
    if skip:
        prefix = "Ок, пропустил: " + ", ".join(names) + "." if skip_all else f"Ок, пропустил {current}."
        left = [] if skip_all else list(rest)
    elif batch:
        content = _wrap(text, lambda t: f"Данные о людях по именам {', '.join(names)} (карточки новых знакомых, тип person): {t}")
        response = await _save(db, process, capture_id=f"tgp-{update_id}", content=content, revision=2)
        low = plain.lower().replace("ё", "е")
        left = [n for n in names if _stem(n) and _stem(n) not in low]       # про кого в ответе ни слова: спросим отдельно
        prefix = "🔒 сохранил без ИИ" if response.get("private") else \
            "✅ Карточки: " + ", ".join(n for n in names if n not in left)
    else:
        content = _wrap(text, lambda t: f"Данные о человеке по имени {current} (карточка нового знакомого, тип person): {t}")
        response = await _save(db, process, capture_id=f"tgp-{update_id}", content=content, revision=2)
        prefix = f"🔒 {current}: сохранил без ИИ" if response.get("private") else f"✅ Карточка: {current}"
        left = list(rest)
    if left:
        return _reply(chat_id, _ask_person(chat_id, db, left, prefix, batch=False))
    _leave_person(chat_id, db, resume)
    if resume:
        queue = _queue(resume)
        position = min(int(resume.get("i", 0)), len(queue) - 1)
        prefix += "\n\nПродолжаем интервью.\n" + interview.question_text(queue[position], position, len(queue))
    return _reply(chat_id, prefix)


# ---------- интервью ----------

def _queue(data: dict) -> list[int]:
    queue = data.get("queue")
    if isinstance(queue, list) and queue and all(isinstance(i, int) and 0 <= i < len(interview.QUESTIONS) for i in queue):
        return queue
    return list(range(len(interview.QUESTIONS)))


def _interview_meta(chat_id: int, text: str, data: dict, db: Database) -> dict | None:
    """Живые просьбы посреди интервью («повтори», «назад», «сколько осталось») — не ответы на вопрос."""
    low = text.lower().strip(" .!?")
    queue = _queue(data)
    position = min(int(data.get("i", 0)), len(queue) - 1)
    if re.match(r"^(повтори|повторите|ещё раз|еще раз|что за вопрос|какой вопрос|какой сейчас вопрос|напомни вопрос)", low):
        return _reply(chat_id, interview.question_text(queue[position], position, len(queue)))
    if re.match(r"^(назад|вернись назад|предыдущий( вопрос)?|верни предыдущий( вопрос)?)$", low):
        position = max(0, position - 1)
        db.set_state(chat_id, "interview", {**data, "i": position})
        return _reply(chat_id, "Вернулся назад.\n\n" + interview.question_text(queue[position], position, len(queue)))
    if re.search(r"сколько (ещё |еще )?(осталось|вопросов|до конца)", low):
        left = len(queue) - position
        return _reply(chat_id, f"Сейчас вопрос {position + 1} из {len(queue)}, осталось {left}. "
                               "Можно остановиться: /stop, продолжить потом: /interview.")
    return None


def _interview_start(chat_id: int, db: Database, arg: str = "") -> dict:
    state = db.get_state(chat_id)
    old = state[1] if state and state[0] == "interview" else {}
    if state and state[0] == "person" and state[1].get("resume"):
        old = state[1]["resume"]
    old = old or db.interview_progress()
    arg = arg.strip().lower()
    if arg in ("блоки", "темы", "список"):
        return _reply(chat_id, interview.blocks_text())
    if arg.isdigit():                        # /interview 22: перейти к вопросу с этим номером
        data = {k: v for k, v in old.items() if k != "paused"} or {"i": 0, "saved": 0}
        total = len(_queue(data))
        data["i"] = max(0, min(int(arg) - 1, total - 1))
    elif arg:
        if arg in ("все", "всё", "заново", "сначала"):
            data = {"i": 0, "saved": old.get("saved", 0)}
        else:
            block = interview.find_block(arg)
            if not block:
                return _reply(chat_id, "Не знаю такого блока.\n\n" + interview.blocks_text())
            data = {"i": 0, "saved": old.get("saved", 0), "queue": interview.block_indices(block)}
    else:
        data = dict(old or db.interview_progress() or {"i": 0, "saved": 0})   # место хранится и вне диалога: дневник его не стирает
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
        db.clear_interview_progress()
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


DONE_STATUS = {"task": "сделано", "event": "состоялось"}     # статусы схемы, которые понимают сводки


def _close_in_vault(db: Database, key: str) -> bool:
    """Кнопка «Сделано»: срез помечается закрытым, ноутбуку уходит служебная запись «закрыть дело»."""
    from . import scheduler

    found = scheduler.find_reminder_note(db.snapshot_items(), key)
    if not found:
        return False
    row_key, front = found
    status = DONE_STATUS.get(str(front.get("type")), "готово")
    db.snapshot_put([(row_key, {**front, "status": status})])
    db.enqueue_service(f"close-{key}", f"close-{key}.json", {
        "action": "close", "title": str(front.get("title", "")), "created": str(front.get("created", "")),
        "type": str(front.get("type", "")), "status": status})
    return True


def _callback(callback: dict, settings: Settings, db: Database, clock) -> dict | None:
    from datetime import timedelta

    user_id = (callback.get("from") or {}).get("id")
    if user_id not in settings.telegram_allowed_ids:
        return None
    data = str(callback.get("data") or "")
    action, _, rest = data.partition(":")
    key, _, extra = rest.partition(":")
    now = clock() if clock else _now(settings)
    message = callback.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id") or user_id, message.get("message_id")

    def toast(text: str) -> dict:
        return {"method": "answerCallbackQuery", "callback_query_id": callback.get("id", ""), "text": text}

    def edit(text: str, buttons: list | None = None) -> dict:
        """Сообщение со сводкой меняется на месте: чат не засоряется. Без message_id остаётся короткая подсказка."""
        if not message_id:
            return toast(text.split("\n")[0][:190])
        reply = {"method": "editMessageText", "chat_id": chat_id, "message_id": message_id,
                 "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            reply["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        return reply

    def back_to_list(line: str) -> dict:
        listing, buttons = actions_module.render_day(db, now)
        return edit(f"{line}\n\n{listing}", buttons)

    if not re.fullmatch(r"[0-9a-f]{12}", key):
        return toast("Не понял кнопку")
    if action == "ok":
        db.set_setting(f"rem_{key}", "done")
        text = "Ок, больше не напомню"
        if _close_in_vault(db, key):
            text = "Готово, больше не напомню. Отмечу сделанным в хранилище при выгрузке"
        return toast(text)
    if action == "bk":
        listing, buttons = actions_module.render_day(db, now)
        return edit(listing, buttons)
    if action == "it":
        shown = actions_module.panel(db, key, now)
        return edit(*shown) if shown else toast("Не нашёл дело: срез обновился. Открой /tasks")
    if action == "sx":
        found = actions_module.panel(db, key, now)
        title = found[0].split("\n")[0] if found else ""
        if not found:
            return toast("Не нашёл дело: срез обновился. Открой /tasks")
        db.set_state(chat_id, "resched", {"key": key, "title": title})
        return _reply(chat_id, f"На когда перенести «{title}»? {WHEN_HELP} Или /skip.")
    if action == "sn":
        target = when_module.snooze_time(extra, now)
        if not target:
            return toast("Не понял кнопку")
        code, title = actions_module.reschedule(db, key, target[0], target[1], now)
        said = when_module.human(target[0], target[1], now)
        if code == "ok":
            return back_to_list(f"⏰ {title}: {said}" + (". Напомню в Telegram." if target[1] else "."))
        return toast("Уже закрыто" if code == "already" else "Не нашёл дело: срез обновился. Открой /tasks")
    if action in ("dn", "cx", "sz"):
        kind = {"dn": "done", "cx": "cancel", "sz": "snooze"}[action]
        code, title = actions_module.apply(db, key, kind, now)
        if code == "ok":
            return back_to_list(f"{actions_module.TOAST[kind]}: {title[:80]}") if message_id else \
                toast(f"{actions_module.TOAST[kind]}: {title[:50]}")
        return toast({"already": f"Уже закрыто: {title[:50]}", "missing": "Не нашёл дело: срез обновился. Открой /tasks",
                      "unsupported": "Для этого дела не поддерживается"}[code])
    if action == "snz1":
        db.set_setting(f"rem_{key}", (now + timedelta(hours=1)).replace(tzinfo=None, microsecond=0).isoformat(timespec="minutes"))
        return toast("Напомню через час")
    if action == "snz24":
        tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=None)
        db.set_setting(f"rem_{key}", tomorrow.isoformat(timespec="minutes"))
        return toast("Напомню завтра в 9:00")
    return toast("Не понял кнопку")


async def _ack_callback(settings: Settings, callback: dict) -> None:
    """Убирает «часики» на кнопке: ответ на нажатие уходит отдельным запросом, а само сообщение правится через webhook."""
    if not settings.telegram_bot_token or not callback.get("id"):
        return
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery",
                              json={"callback_query_id": callback["id"]})
    except httpx.HTTPError:
        pass
