"""SQLite: журнал всех захватов. Нужен для идемпотентности (повторный запрос с тем же
capture_id вернёт тот же ответ, не тратя токены нейросети) и как страховка:
исходный текст заметки сохраняется на сервере, даже если что-то пошло не так."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    capture_id    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    client        TEXT NOT NULL,
    input_type    TEXT NOT NULL,
    content       TEXT NOT NULL,
    clarification TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL,
    PRIMARY KEY (capture_id, revision)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()

    def get(self, capture_id: str, revision: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT response_json FROM captures WHERE capture_id = ? AND revision = ?",
                (capture_id, revision),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def save(
        self,
        *,
        capture_id: str,
        revision: int,
        client: str,
        input_type: str,
        content: str,
        clarification: str,
        response: dict,
    ) -> None:
        """INSERT OR IGNORE: если параллельный дубль уже записался, первый ответ остаётся главным."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO captures "
                "(capture_id, revision, created_at, client, input_type, content, clarification, response_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    revision,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    client,
                    input_type,
                    content,
                    clarification,
                    json.dumps(response, ensure_ascii=False),
                ),
            )
            conn.commit()
