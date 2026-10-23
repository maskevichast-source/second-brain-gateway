"""Самонастройка вебхука Telegram.

После смены токена у @BotFather (или если вебхук слетел) Telegram перестаёт присылать сообщения на сервер, и бот
молчит. Поэтому при старте (и раз в час) сервис сам сверяет вебхук со своим адресом и при расхождении ставит
его заново. Нужны TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET и адрес: PUBLIC_URL или RAILWAY_PUBLIC_DOMAIN
(Railway подставляет его сам). Токен в логи не попадает.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import monitor
from .config import Settings

logger = logging.getLogger(__name__)
PATH = "/telegram/webhook"
ALLOWED = ["message", "callback_query"]


COMMANDS = [
    ("ask", "Спросить по моим заметкам"), ("day", "Сводка дня"), ("tasks", "Дела и сроки"),
    ("week", "Обзор недели"), ("person", "Карточка человека целиком"), ("who", "Что знать перед встречей с человеком"), ("brief", "Брифинг по человеку"),
    ("promises", "Кто что кому обещал"), ("radar", "С кем давно не общались"), ("budget", "Бюджет из Ады"),
    ("health", "Проверка базы"), ("remember", "Вспомнить старые идеи"), ("merge", "Дубли карточек"),
    ("contacts", "Разобрать карточки людей"), ("people", "Кого я знаю"), ("diary", "Вечерний дневник"),
    ("interview", "Интервью о себе"), ("fact", "Запомнить короткий факт"), ("me", "Кто я (имя владельца)"),
    ("push", "Что я присылаю сам"), ("done", "Дело сделано"), ("cancel", "Отменить дело"),
    ("snooze", "Отложить дело"), ("skip", "Сохранить как есть"), ("stop", "Остановить диалог"),
    ("import", "Импорт"), ("sync", "Состояние среза"), ("status", "Состояние сервиса"),
    ("backup", "Резервная копия"), ("requeue", "Вернуть заметки в очередь"), ("forget", "Стереть срез на сервере"),
    ("help", "Все возможности"),
]


async def ensure_commands(settings: Settings, client: httpx.AsyncClient | None = None) -> str:
    """Меню команд по «/» в чате: ставится при старте и раз в час. 'ok' | 'set' | 'skipped' | 'failed'."""
    if not settings.telegram_bot_token:
        return "skipped"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=20)
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    want = [{"command": c, "description": d} for c, d in COMMANDS]
    try:
        have = (await client.post(f"{base}/getMyCommands", json={})).json()
        if have.get("ok") and have.get("result") == want:
            return "ok"
        reply = (await client.post(f"{base}/setMyCommands", json={"commands": want})).json()
        if not reply.get("ok"):
            raise ValueError(str(reply.get("description") or "setMyCommands"))
        return "set"
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("commands: не удалось поставить меню (%s)", type(exc).__name__)
        monitor.failure("telegram", exc)
        return "failed"
    finally:
        if owns:
            await client.aclose()


def expected_url(settings: Settings) -> str:
    return f"{settings.public_url}{PATH}" if settings.public_url else ""


def configured(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_secret and settings.public_url)


FRESH_ERROR_SECONDS = 2 * 3600


def _has_fresh_error(info: dict) -> bool:
    """Telegram хранит last_error_message после любой прошлой ошибки и сам её не стирает: без проверки возраста
    сервис переставлял бы вебхук каждый час впустую. Свежей считается ошибка не старше двух часов (дата неизвестна —
    считаем свежей)."""
    if not info.get("last_error_message"):
        return False
    stamp = info.get("last_error_date")
    try:
        return time.time() - float(stamp) <= FRESH_ERROR_SECONDS
    except (TypeError, ValueError):
        return True


async def ensure_webhook(settings: Settings, client: httpx.AsyncClient | None = None) -> str:
    """'ok' (уже стоит), 'set' (поставил заново), 'skipped' (нечем настраивать) или 'failed'."""
    if not configured(settings):
        return "skipped"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=20)
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    want = expected_url(settings)
    try:
        info = (await client.get(f"{base}/getWebhookInfo")).json()
        if not info.get("ok"):
            raise ValueError(str(info.get("description") or "getWebhookInfo"))
        current = info.get("result") or {}
        if current.get("url") == want and not _has_fresh_error(current):
            return "ok"
        reply = (await client.post(f"{base}/setWebhook", json={
            "url": want, "secret_token": settings.telegram_secret, "allowed_updates": ALLOWED,
            "drop_pending_updates": False})).json()
        if not reply.get("ok"):
            raise ValueError(str(reply.get("description") or "setWebhook"))
        logger.warning("webhook: был %r, поставил заново %s", current.get("url") or "пусто", want)
        return "set"
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("webhook: не удалось сверить (%s)", type(exc).__name__)
        monitor.failure("telegram", exc)
        return "failed"
    finally:
        if owns:
            await client.aclose()
