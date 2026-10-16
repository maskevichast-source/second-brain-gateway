"""Резервная копия данных шлюза: профиль, настройки бота и срез хранилища одним JSON-файлом.

Тексты заметок в копию не попадают (их на сервере нет), приватные заметки в срез не попадают по построению.
Секретов (токенов, ключей) в копии нет: они живут только в переменных окружения.
"""
from __future__ import annotations

import json
from datetime import datetime

from .db import Database

FORMAT = "second-brain-gateway-backup"


def build_backup(db: Database, now: datetime) -> tuple[str, bytes]:
    count, synced = db.snapshot_info()
    data = {
        "format": FORMAT, "version": 1, "created": now.isoformat(timespec="seconds"),
        "profile": {"context": db.get_context(), "people": db.known_people()},
        "settings": db.settings_all(),
        "snapshot_last_full_sync": synced,
        "snapshot": db.snapshot_fronts(),
        "snapshot_count": count,
    }
    return f"second-brain-backup-{now:%Y-%m-%d}.json", json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
