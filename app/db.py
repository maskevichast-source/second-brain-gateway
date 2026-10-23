"""SQLite: короткая память сервера для защиты от дублей и очередь для Telegram.

Если шорткат отправил одну запись дважды (например, из-за обрыва сети), второй запрос
получает тот же ответ, и токены ИИ не тратятся. Ответ хранится только RESPONSE_TTL_HOURS
(по умолчанию 24 часа) и затем удаляется. Тексты заметок на сервере не накапливаются:
навсегда они остаются только в хранилище Obsidian.

Исключение: заметки, пришедшие через Telegram, лежат в очереди `outbox`, пока их не заберёт
ноутбук или телефон (`/pending`, затем `/ack` удаляет). Если никто не забрал, они стираются
через OUTBOX_TTL_DAYS (45 дней). Также до 24 часов хранится текст, который ждёт уточнения.
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
    created_at TEXT NOT NULL,
    sources    TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS blobs (
    key        TEXT PRIMARY KEY,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path, ttl_hours: float = 24.0, outbox_ttl_days: float = 45.0):
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
            waiting = {row["name"] for row in conn.execute("PRAGMA table_info(tg_pending)")}
            if "sources" not in waiting:                         # исходники, ожидающие уточнения
                conn.execute("ALTER TABLE tg_pending ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
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
            removed += conn.execute("DELETE FROM blobs WHERE created_at < ?", (outbox_cutoff,)).rowcount
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

    def requeue_recent(self, hours: float) -> int:
        """Возвращает в очередь заметки из последних `hours` часов (ответы хранятся 24 часа), которые уже забрали и стёрли.
        Уже лежащие в очереди не дублируются (уникальна пара capture_id + имя файла)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=min(max(hours, 0.1), 24))).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT capture_id, revision, response_json FROM responses WHERE created_at >= ? ORDER BY created_at",
                (cutoff,)).fetchall()
        added = 0
        for row in rows:
            try:
                response = json.loads(row["response_json"])
            except ValueError:
                continue
            files = response.get("files") if isinstance(response, dict) else None
            if not files or (response.get("needs_review") and row["revision"] == 1):
                continue                      # служебные ответы Telegram и черновики с вопросом не трогаем
            added += self.enqueue(row["capture_id"], files)
        return added

    def enqueue_files(self, capture_id: str, planned: list[dict]) -> int:
        """Исходники: в очереди только квитанция (id файла в Telegram), сами байты сервер не хранит."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = 0
        with closing(self._connect()) as conn:
            for p in planned:
                meta = {k: p[k] for k in ("file_id", "blob", "mime", "size", "kind", "name") if k in p}
                added += conn.execute(
                    "INSERT OR IGNORE INTO outbox (capture_id, filename, folder, markdown, created_at, reminders, kind) "
                    "VALUES (?, ?, ?, ?, ?, '[]', 'file')",
                    (capture_id, p["filename"], p["folder"], json.dumps(meta, ensure_ascii=False), now),
                ).rowcount
            conn.commit()
        return added

    def put_blob(self, key: str, data: bytes) -> None:
        """Оригинал файла, присланного шорткатом: лежит в очереди до выгрузки (его нет в Telegram, чтобы скачать позже)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO blobs (key, data, created_at) VALUES (?, ?, ?)", (key, data, now))
            conn.commit()

    def get_blob(self, key: str) -> bytes | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT data FROM blobs WHERE key = ?", (key,)).fetchone()
        return bytes(row["data"]) if row else None

    def get_item(self, item_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT id, kind, filename, folder, markdown FROM outbox WHERE id = ?",
                               (int(item_id),)).fetchone()
        return dict(row) if row else None

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

    def pending(self, limit: int = 50, service: bool = False, files: bool = False) -> list[dict]:
        where = "" if service else "WHERE kind IN ('note', 'file')" if files else "WHERE kind = 'note'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, capture_id, filename, folder, markdown, created_at, reminders, kind FROM outbox "
                f"{where} ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_count(self, service: bool = False, files: bool = False) -> int:
        where = "" if service else "WHERE kind IN ('note', 'file')" if files else "WHERE kind = 'note'"
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
            marks = ",".join("?" * len(ids))
            for row in conn.execute(f"SELECT markdown FROM outbox WHERE kind = 'file' AND id IN ({marks})", ids).fetchall():
                try:
                    key = json.loads(row["markdown"]).get("blob")
                except (ValueError, AttributeError):
                    key = None
                if key:                                           # файл выгружен: освобождаем место на сервере
                    conn.execute("DELETE FROM blobs WHERE key = ?", (key,))
            removed = conn.execute(f"DELETE FROM outbox WHERE id IN ({marks})", ids).rowcount
            conn.commit()
            if removed:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        return removed

    # ---------- ожидание уточнения в Telegram ----------

    def set_waiting(self, chat_id: int, capture_id: str, content: str, sources: list | None = None) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tg_pending (chat_id, capture_id, content, created_at, sources) VALUES (?, ?, ?, ?, ?)",
                (chat_id, capture_id, content, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 json.dumps(sources or [], ensure_ascii=False)),
            )
            conn.commit()

    def get_waiting(self, chat_id: int) -> dict | None:
        cutoff = (datetime.now(timezone.utc) - self.ttl).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT capture_id, content, sources, created_at FROM tg_pending WHERE chat_id = ? AND created_at >= ?",
                (chat_id, cutoff),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["sources"] = json.loads(data.get("sources") or "[]")
        return data

    def stale_waiting(self, minutes: float) -> list[dict]:
        """Записи, которые ждут уточнения дольше minutes минут (их надо сохранить как есть, а не потерять)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT chat_id, capture_id, content, sources, created_at FROM tg_pending WHERE created_at < ?", (cutoff,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["sources"] = json.loads(data.get("sources") or "[]")
            out.append(data)
        return out

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

    @staticmethod
    def _snapshot_write(conn, rows: list[tuple[str, dict]], max_rows: int) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, front in rows:
            conn.execute("INSERT OR REPLACE INTO snapshot (key, front, updated_at) VALUES (?, ?, ?)",
                         (key, json.dumps(front, ensure_ascii=False), now))
        conn.execute(
            "DELETE FROM snapshot WHERE key IN (SELECT key FROM snapshot ORDER BY updated_at DESC, key DESC LIMIT -1 OFFSET ?)",
            (max_rows,))

    def snapshot_put(self, rows: list[tuple[str, dict]], max_rows: int = 20000) -> None:
        with closing(self._connect()) as conn:
            self._snapshot_write(conn, rows, max_rows)
            conn.commit()

    def snapshot_delete(self, key: str) -> int:
        """Убрать одну заметку из среза (например, карточку, отправленную в архив)."""
        with closing(self._connect()) as conn:
            count = conn.execute("DELETE FROM snapshot WHERE key = ?", (key,)).rowcount
            conn.commit()
        return count

    def forget_person(self, name: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM known_people WHERE name = ?", (name,))
            conn.commit()

    def snapshot_replace(self, rows: list[tuple[str, dict]], max_rows: int = 20000) -> None:
        """Полная замена среза одной транзакцией: при сбое посередине остаётся прежний срез, а не пустой."""
        with closing(self._connect()) as conn:
            try:
                conn.execute("DELETE FROM snapshot")
                conn.execute(
                    "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES ('snapshot_synced', ?, ?)",
                    ("1", datetime.now(timezone.utc).isoformat(timespec="seconds")))
                self._snapshot_write(conn, rows[:max_rows], max_rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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
            conn.execute("DELETE FROM responses")          # кэш ответов на 24 часа тоже содержит тексты записей
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
        """Профиль с ноутбука плюс короткие факты, которые владелец сказал боту командой /fact."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM profile WHERE key = 'context'").fetchone()
        base = row["value"] if row else ""
        try:
            names = [str(n) for n in json.loads(self.get_setting("owner_names", "") or "[]")]
        except ValueError:
            names = []
        if names:
            base = (f"Владелец бота и автор записей: {', '.join(names)}. Это «я» в записях, имя может стоять в любом порядке "
                    "(«Маскевич Владислав»). Карточку человека для владельца не создавай и про него как про нового знакомого не спрашивай.\n"
                    + base).strip()
        facts = self.get_facts()
        return (base + "\n" + "\n".join(f"- {f}" for f in facts)).strip() if facts else base

    def get_facts(self) -> list[str]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM profile WHERE key = 'facts'").fetchone()
        try:
            return [str(f) for f in json.loads(row["value"])] if row else []
        except ValueError:
            return []

    def add_fact(self, text: str, limit: int = 40) -> int:
        """Короткий факт о жизни владельца («Собаку зовут Янка»). Старые вытесняются, повторы не плодятся."""
        text = " ".join(text.split())[:200]
        facts = [f for f in self.get_facts() if f.lower() != text.lower()] + [text]
        facts = facts[-limit:]
        with closing(self._connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES ('facts', ?, ?)",
                         (json.dumps(facts, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        return len(facts)

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
            conn.execute("DELETE FROM profile WHERE key IN ('context', 'facts')")
            conn.execute("DELETE FROM known_people")
            conn.execute("DELETE FROM responses")          # в кэшированных ответах встречаются имена людей
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
        if kind == "interview":                    # место в интервью хранится отдельно: другие диалоги его не затрут
            self.set_setting("interview_progress", json.dumps({**data, "paused": False}, ensure_ascii=False))

    # сколько минут вопрос ждёт ответа, прежде чем бот перестанет принимать за него обычные сообщения
    STATE_IDLE_MINUTES = {"person": 30, "diary": 180, "interview": 180, "resched": 30, "contacts": 180, "purchase": 120}

    def get_state(self, chat_id: int) -> tuple[str, dict] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT kind, data, created_at FROM tg_state WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return None
        kind, data = row["kind"], json.loads(row["data"])
        limit = self.STATE_IDLE_MINUTES.get(kind)
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds() / 60
        except (TypeError, ValueError):
            age = 0
        if limit and age > limit:
            if kind == "person":                   # давно не отвечал про человека: вопрос снимаем, интервью возвращаем на паузе
                resume = data.get("resume")
                if resume:
                    self.set_state(chat_id, "interview", {**resume, "paused": True})
                    return self.get_state(chat_id)
                self.clear_state(chat_id)
                return None
            if kind == "interview" and not data.get("paused"):
                data = {**data, "paused": True}    # долгая тишина: обычные записи снова идут как записи
                self.set_state(chat_id, "interview", data)
                return ("interview", data)
            if kind in ("diary", "resched", "contacts", "purchase"):
                self.clear_state(chat_id)
                return None
        return (kind, data)

    def interview_progress(self) -> dict:
        raw = self.get_setting("interview_progress", "")
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def clear_interview_progress(self) -> None:
        self.set_setting("interview_progress", "")

    def clear_state(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM tg_state WHERE chat_id = ?", (chat_id,))
            conn.commit()
