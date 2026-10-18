import asyncio

import httpx

from app import ada, ada_home
from app.db import Database
from app.snapshot import rows_from_markdown
from .conftest import FIXED_NOW, make_settings

RECS = [
    {"care_id": "1", "kind": "care", "item": "Ecovacs Deebot", "task": "Очистить щётки", "interval_days": 14,
     "last_done": "2026-10-01", "next_due": "2026-10-15", "note": ""},
    {"care_id": "2", "kind": "care", "item": "Замиокулькас", "task": "Полить", "interval_days": 10,
     "last_done": "", "next_due": "2026-10-20", "note": "не заливать"},
    {"care_id": "3", "kind": "care", "item": "Фильтр воды", "task": "Заменить картридж", "interval_days": 90,
     "last_done": "", "next_due": "", "note": ""},
]


def make(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    return db, make_settings(tmp_path, ada_summary_url="https://ada.invalid/api/secretary", ada_summary_token="tok")


def test_classify_and_schedule():
    assert ada_home.classify("Замиокулькас") == "растение"
    assert ada_home.classify("Ecovacs Deebot") == "техника"
    assert ada_home.classify("Фильтр воды") == "уход"
    assert ada_home.schedule_text(14) == "каждые 14 дней" and ada_home.schedule_text("x") == ""


def test_care_file_is_valid_home_note():
    f = ada_home.care_file(RECS[0], FIXED_NOW)
    (_, front), = rows_from_markdown(f["markdown"])
    assert front["type"] == "home" and front["schedule"] == "каждые 14 дней"
    assert str(front["next_due"]) == "2026-10-15" and front["status"] == "открыто"


def test_import_is_idempotent_and_force(tmp_path, monkeypatch):
    db, settings = make(tmp_path)

    async def fake(_settings, http_client=None):
        return RECS
    monkeypatch.setattr(ada, "fetch_homecare", fake)
    first = asyncio.run(ada_home.import_home(settings, db, FIXED_NOW))
    assert "Перенёс" in first
    items = db.snapshot_items()
    assert len(items) == 4          # три задачи и общая заметка
    again = asyncio.run(ada_home.import_home(settings, db, FIXED_NOW))
    assert "уже перенесено" in again and len(db.snapshot_items()) == 4
    forced = asyncio.run(ada_home.import_home(settings, db, FIXED_NOW, force=True))
    assert "Перенёс" in forced and len(db.snapshot_items()) == 4


def test_import_reports_unavailable_and_empty(tmp_path, monkeypatch):
    db, settings = make(tmp_path)

    async def none(_s, http_client=None):
        return None

    async def empty(_s, http_client=None):
        return []
    monkeypatch.setattr(ada, "fetch_homecare", none)
    assert "Не достучался" in asyncio.run(ada_home.import_home(settings, db, FIXED_NOW))
    monkeypatch.setattr(ada, "fetch_homecare", empty)
    assert "пусто" in asyncio.run(ada_home.import_home(settings, db, FIXED_NOW))
    assert "не подключена" in asyncio.run(ada_home.import_home(make_settings(tmp_path), db, FIXED_NOW))


def test_fetch_homecare_parses_and_survives_errors(tmp_path):
    settings = make_settings(tmp_path, ada_summary_url="https://ada.invalid/api/secretary", ada_summary_token="tok")
    seen = []

    def ok(request):
        seen.append((str(request.url), request.headers["x-secretary-token"]))
        return httpx.Response(200, json={"items": RECS + ["мусор"]})

    async def run(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await ada.fetch_homecare(settings, c)
    assert len(asyncio.run(run(ok))) == 3
    assert seen == [("https://ada.invalid/api/homecare", "tok")]
    assert asyncio.run(run(lambda r: httpx.Response(500))) is None
