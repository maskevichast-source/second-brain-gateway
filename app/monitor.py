"""Мониторинг шлюза: структурные логи ошибок и редкое сообщение владельцу.

В лог и в сообщение попадают только источник сбоя и имя типа исключения. Текст исключения не пишется:
в нём могут оказаться адреса с токеном бота или кусок заметки. Если сбоев в течение часа набралось
три и больше, владельцу в личку уходит одно сообщение, затем следующее не раньше чем через час.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, deque

logger = logging.getLogger("second_brain.monitor")

WINDOW_SECONDS = 3600.0
REPEAT_THRESHOLD = 3
SOURCES = {
    "webhook": "обработчик Telegram", "scheduler": "планировщик", "telegram": "отправка в Telegram",
    "deepseek": "DeepSeek", "stt": "распознавание речи", "ada": "Ада", "vision": "распознавание картинок",
}


class Monitor:
    def __init__(self) -> None:
        self.db = None
        self.send = None
        self.chats: tuple = ()
        self.events: deque = deque(maxlen=200)

    def configure(self, db, send=None, chats=()) -> None:
        self.db, self.send, self.chats = db, send, tuple(chats)
        self.events.clear()

    def failure(self, source: str, exc: BaseException | None = None, now: float | None = None) -> None:
        """Записывает сбой: структурная строка в лог и событие в памяти (после рестарта счёт идёт заново)."""
        name = type(exc).__name__ if exc is not None else "Error"
        stamp = time.time() if now is None else now
        self.events.append((stamp, source, name))
        logger.error("gateway_error %s", json.dumps({"event": "gateway_error", "source": source, "error": name},
                                                    ensure_ascii=False))

    def recent(self, now: float | None = None) -> list[tuple]:
        edge = (time.time() if now is None else now) - WINDOW_SECONDS
        return [e for e in self.events if e[0] >= edge]

    def alert_text(self, recent: list[tuple]) -> str:
        by_source = Counter(src for _, src, _ in recent)
        parts = ", ".join(f"{SOURCES.get(s, s)}: {n}" for s, n in by_source.most_common())
        kinds = ", ".join(sorted({name for _, _, name in recent})[:5])
        return (f"⚠️ Шлюз «Второй Мозг»: за последний час повторяются сбои ({len(recent)}). {parts}. "
                f"Типы ошибок: {kinds}. Записи не теряются: без ИИ они сохраняются как есть. "
                "Подробности в логах Railway. Следующее такое сообщение будет не раньше чем через час.")

    async def flush(self, now: float | None = None) -> bool:
        """Отправляет предупреждение, если оно нужно и прошёл час с прошлого. Возвращает True, если отправлено."""
        stamp = time.time() if now is None else now
        recent = self.recent(stamp)
        if len(recent) < REPEAT_THRESHOLD or self.db is None or self.send is None or not self.chats:
            return False
        try:
            last = float(self.db.get_setting("err_last_alert", "0") or 0)
        except ValueError:
            last = 0.0
        if stamp - last < WINDOW_SECONDS:
            return False
        self.db.set_setting("err_last_alert", str(stamp))             # сначала отметка: сбой отправки не даст спама
        text = self.alert_text(recent)
        for chat_id in self.chats:
            try:
                await self.send(chat_id, text)
            except Exception as exc:  # noqa: BLE001 — монитор сам не должен ронять сервис
                logger.error("gateway_error %s", json.dumps({"event": "alert_failed", "error": type(exc).__name__}))
        return True


MONITOR = Monitor()


def failure(source: str, exc: BaseException | None = None) -> None:
    MONITOR.failure(source, exc)
