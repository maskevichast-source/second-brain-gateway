"""SQLite: короткая память сервера для защиты от дублей.

Если шорткат отправил одну запись дважды (например, из-за обрыва сети), второй запрос
получает тот же ответ, и токены ИИ не тратятся. Ответ хранится только RESPONSE_TTL_HOURS
(по умолчанию 24 часа) и затем удаляется. Тексты заметок на сервере не накапливаются:
навсегда они остаются только в хранилище Obsidian.
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
"""


class Database:
    def __init__(self, path: Path, ttl_hours: float = 24.0):
        self.path = Path(path)
        self.ttl = timedelta(hours=ttl_hours)

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
