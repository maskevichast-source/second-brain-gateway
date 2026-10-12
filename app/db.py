"""SQLite: короткая память сервера для защиты от дублей и очередь для Telegram.

Если шорткат отправил одну запись дважды (например, из-за обрыва сети), второй запрос
получает тот же ответ, и токены ИИ не тратятся. Ответ хранится только RESPONSE_TTL_HOURS
(по умолчанию 24 часа) и затем удаляется. Тексты заметок на сервере не накапливаются:
навсегда они остаются только в хранилище Obsidian.

Исключение: заметки, пришедшие через Telegram, лежат в очереди `outbox`, пока их не заберёт
ноутбук или телефон (`/pending`, затем `/ack` удаляет). Если никто не забрал, они стираются
через OUTBOX_TTL_DAYS (14 дней). Также до 24 часов хранится текст, который ждёт уточнения.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    capture_id    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    response_json TEXT NOT NULL,
    PRIMARY KEY (capture_id, revision)
);
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    folder      TEXT NOT NULL,
    markdown    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (capture_id, filename)
);
CREATE TABLE IF NOT EXISTS tg_pending (
    chat_id    INTEGER PRIMARY KEY,
    capture_id TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path, ttl_hours: float = 24.0, outbox_ttl_days: float = 14.0):
        self.path = Path(path)
        self.ttl = timedelta(hours=ttl_hours)
        self.outbox_ttl = timedelta(days=outbox_ttl_days)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            self._drop_legacy(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        self.purge_expired(vacuum=True)

    def _drop_legacy(self, conn: sqlite3.Connection) -> None:
        """Версия 1 хранила полные тексты заметок навсегда (таблица captures). Удаляем её вместе с текстами."""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'captures'"
        ).fetchone()
        if exists:
            conn.execute("DROP TABLE captures")
            conn.commit()
            conn.execute("VACUUM")
            logger.info("старая таблица с текстами заметок удалена")

    def purge_expired(self, vacuum: bool = False) -> None:
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            removed = conn.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,)).rowcount
            removed += conn.execute("DELETE FROM tg_pending WHERE created_at < ?", (cutoff,)).rowcount
            outbox_cutoff = (datetime.now(timezone.utc) - self.outbox_ttl).isoformat(timespec="seconds")
            removed += conn.execute("DELETE FROM outbox WHERE created_at < ?", (outbox_cutoff,)).rowcount
            conn.commit()
            if removed or vacuum:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if removed:
                    conn.execute("VACUUM")

    def get(self, capture_id: str, revision: int) -> dict | None:
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT response_json FROM responses "
                "WHERE capture_id = ? AND revision = ? AND created_at >= ?",
                (capture_id, revision, cutoff),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def save(self, *, capture_id: str, revision: int, response: dict) -> None:
        """INSERT OR IGNORE: если параллельный дубль уже записался, первый ответ остаётся главным."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO responses (capture_id, revision, created_at, response_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    capture_id,
                    revision,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    json.dumps(response, ensure_ascii=False),
                ),
            )
            conn.commit()
        self.purge_expired()

    # ---------- очередь заметок из Telegram ----------

    def enqueue(self, capture_id: str, files: list[dict]) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = 0
        with closing(self._connect()) as conn:
            for f in files:
                added += conn.execute(
                    "INSERT OR IGNORE INTO outbox (capture_id, filename, folder, markdown, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (capture_id, f["filename"], f["folder"], f["markdown"], now),
                ).rowcount
            conn.commit()
        return added

    def pending(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, capture_id, filename, folder, markdown, created_at FROM outbox ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_count(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def ack(self, ids: list[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with closing(self._connect()) as conn:
            removed = conn.execute(
                f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids
            ).rowcount
            conn.commit()
            if removed:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        return removed

    # ---------- ожидание уточнения в Telegram ----------

    def set_waiting(self, chat_id: int, capture_id: str, content: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tg_pending (chat_id, capture_id, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, capture_id, content, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()

    def get_waiting(self, chat_id: int) -> dict | None:
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT capture_id, content FROM tg_pending WHERE chat_id = ? AND created_at >= ?",
                (chat_id, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def clear_waiting(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM tg_pending WHERE chat_id = ?", (chat_id,))
            conn.commit()
