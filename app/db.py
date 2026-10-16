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
    reminders   TEXT NOT NULL DEFAULT '[]',
    kind        TEXT NOT NULL DEFAULT 'note',
    UNIQUE (capture_id, filename)
);
CREATE TABLE IF NOT EXISTS profile (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_people (
    name       TEXT PRIMARY KEY,
    role       TEXT NOT NULL DEFAULT '',
    aliases    TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_state (
    chat_id    INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    key        TEXT PRIMARY KEY,
    front      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(outbox)")}
            if "reminders" not in columns:                       # база создана версией до напоминаний
                conn.execute("ALTER TABLE outbox ADD COLUMN reminders TEXT NOT NULL DEFAULT '[]'")
            if "kind" not in columns:                            # служебные записи («закрыть дело»)
                conn.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'note'")
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
            state_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
            removed += conn.execute("DELETE FROM tg_state WHERE created_at < ?", (state_cutoff,)).rowcount
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
                    "INSERT OR IGNORE INTO outbox (capture_id, filename, folder, markdown, created_at, reminders) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (capture_id, f["filename"], f["folder"], f["markdown"], now,
                     json.dumps(f.get("reminders", []), ensure_ascii=False)),
                ).rowcount
            conn.commit()
        return added

    def enqueue_service(self, capture_id: str, filename: str, payload: dict) -> int:
        """Служебная запись для ноутбука (JSON в поле markdown). Телефону и старым клиентам не отдаётся."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            added = conn.execute(
                "INSERT OR IGNORE INTO outbox (capture_id, filename, folder, markdown, created_at, reminders, kind) "
                "VALUES (?, ?, '', ?, ?, '[]', 'close')",
                (capture_id, filename, json.dumps(payload, ensure_ascii=False), now),
            ).rowcount
            conn.commit()
        return added

    def pending(self, limit: int = 50, service: bool = False) -> list[dict]:
        where = "" if service else "WHERE kind = 'note'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, capture_id, filename, folder, markdown, created_at, reminders, kind FROM outbox "
                f"{where} ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_count(self, service: bool = False) -> int:
        where = "" if service else "WHERE kind = 'note'"
        with closing(self._connect()) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM outbox {where}").fetchone()[0]

    def service_count(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM outbox WHERE kind != 'note'").fetchone()[0]

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

    # ---------- простые настройки бота (время напоминания и т.п.) ----------

    def get_setting(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM profile WHERE key = ?", (f"cfg_{key}",)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES (?, ?, ?)",
                         (f"cfg_{key}", value, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()

    def settings_all(self) -> dict:
        """Настройки бота (время сводок и т.п.) без служебных отметок напоминаний."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT key, value FROM profile WHERE key LIKE 'cfg\\_%' ESCAPE '\\' "
                                "AND key NOT LIKE 'cfg\\_rem\\_%' ESCAPE '\\' ORDER BY key").fetchall()
        return {row["key"][4:]: row["value"] for row in rows}

    # ---------- срез хранилища (короткие свойства заметок, без текстов) ----------

    def snapshot_put(self, rows: list[tuple[str, dict]], max_rows: int = 20000) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            for key, front in rows:
                conn.execute("INSERT OR REPLACE INTO snapshot (key, front, updated_at) VALUES (?, ?, ?)",
                             (key, json.dumps(front, ensure_ascii=False), now))
            conn.execute(
                "DELETE FROM snapshot WHERE key IN (SELECT key FROM snapshot ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                (max_rows,))
            conn.commit()

    def snapshot_replace(self, rows: list[tuple[str, dict]], max_rows: int = 20000) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM snapshot")
            conn.execute(
                "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES ('snapshot_synced', ?, ?)",
                ("1", datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        self.snapshot_put(rows[:max_rows], max_rows)

    def snapshot_fronts(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT front FROM snapshot ORDER BY updated_at, key").fetchall()
        return [json.loads(row["front"]) for row in rows]

    def snapshot_items(self) -> list[tuple[str, dict]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT key, front FROM snapshot ORDER BY updated_at, key").fetchall()
        return [(row["key"], json.loads(row["front"])) for row in rows]

    def snapshot_info(self) -> tuple[int, str]:
        with closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
            row = conn.execute("SELECT updated_at FROM profile WHERE key = 'snapshot_synced'").fetchone()
        return count, (row["updated_at"] if row else "")

    def snapshot_last_update(self) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(updated_at) FROM snapshot").fetchone()
        return row[0] or ""

    def snapshot_stamp(self) -> tuple:
        """Дешёвая «отметка версии» среза: меняется при добавлении, замене и удалении строк."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*), MAX(updated_at), COALESCE(SUM(LENGTH(front)), 0) FROM snapshot").fetchone()
        return tuple(row)

    def snapshot_clear(self) -> int:
        with closing(self._connect()) as conn:
            count = conn.execute("DELETE FROM snapshot").rowcount
            conn.execute("DELETE FROM profile WHERE key = 'snapshot_synced'")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        return count

    # ---------- профиль и известные люди (постоянно, но только имена, роли и короткий контекст) ----------

    def set_context(self, text: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES ('context', ?, ?)",
                (text, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()

    def get_context(self) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM profile WHERE key = 'context'").fetchone()
        return row["value"] if row else ""

    def known_people(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT name, role, aliases FROM known_people ORDER BY name").fetchall()
        return [{"name": r["name"], "role": r["role"], "aliases": json.loads(r["aliases"])} for r in rows]

    def replace_people(self, people: list[dict]) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM known_people")
            for p in people:
                conn.execute(
                    "INSERT OR REPLACE INTO known_people (name, role, aliases, updated_at) VALUES (?, ?, ?, ?)",
                    (p["name"], p.get("role", ""), json.dumps(p.get("aliases", []), ensure_ascii=False), stamp),
                )
            conn.commit()

    def upsert_person(self, name: str, role: str = "") -> None:
        """Добавляет человека; уже известной роли пустой не затирает."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO known_people (name, role, aliases, updated_at) VALUES (?, ?, '[]', ?) "
                "ON CONFLICT(name) DO UPDATE SET role = CASE WHEN excluded.role != '' THEN excluded.role ELSE role END, "
                "updated_at = excluded.updated_at",
                (name, role, stamp),
            )
            conn.commit()

    def clear_profile(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM profile WHERE key = 'context'")
            conn.execute("DELETE FROM known_people")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")

    # ---------- состояние диалога в Telegram (новый человек, интервью) ----------

    def set_state(self, chat_id: int, kind: str, data: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tg_state (chat_id, kind, data, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, kind, json.dumps(data, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()

    def get_state(self, chat_id: int) -> tuple[str, dict] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT kind, data FROM tg_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return (row["kind"], json.loads(row["data"])) if row else None

    def clear_state(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM tg_state WHERE chat_id = ?", (chat_id,))
            conn.commit()
