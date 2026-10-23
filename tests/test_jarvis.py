import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from app import ada, jarvis, news, scheduler
from app.db import Database
from .conftest import StubLLM, auth, make_settings, note, result
from .test_telegram import ME, send, tg, update  # noqa: F401

TZ = ZoneInfo("Asia/Almaty")


def at(h, m=0, d=23):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


class Sender:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return True


DATA = {"life": {"big_purchases": [
    {"id": "T1", "date": "2026-10-15", "amount": 100000, "category": "Дом и быт", "subcategory": "Товары для дома",
     "merchant": "Не указан", "comment": "Покупка ковра в новую квартиру"},
    {"id": "T2", "date": "2026-10-14", "amount": 480000, "category": "Дом и быт", "subcategory": "Ремонт и стройматериалы",
     "merchant": "Есимбек Е.", "comment": "Оплата окончания ремонтных работ"}],
    "income": {"salary": {"date": "2026-10-22", "amount": 900000}}}}


def test_advice_depends_on_kind_of_purchase():
    assert jarvis.advice(DATA["life"]["big_purchases"][1])[0] == "service"
    assert "акт выполненных работ" in jarvis.advice(DATA["life"]["big_purchases"][1])[1]
    assert jarvis.advice({"category": "Электроника и техника"})[0] == "tech"
    assert jarvis.advice({"category": "Дом и быт", "comment": "кровать и диван"})[0] == "furniture"
    assert jarvis.advice({"category": "Прочее"})[0] == "other"


def test_alerts_once_with_nudges_and_quiet_hours(tmp_path):
    db = Database(tmp_path / "j.db")
    db.init()
    first = jarvis.due_alerts(DATA, db, at(11))
    ids = [a[0] for a in first]
    assert ids == ["buy:T1", "buy:T2", "inc:salary:2026-10-22"]
    jarvis.after_sent(db, "inc:salary:2026-10-22", at(11))
    assert "inc:salary:2026-10-22" not in [a[0] for a in jarvis.due_alerts(DATA, db, at(12))]     # доход один раз
    jarvis.after_sent(db, "buy:T1", at(11))
    assert "buy:T1" not in [a[0] for a in jarvis.due_alerts(DATA, db, at(12))]                    # напомним позже, не сразу
    assert "buy:T1" in [a[0] for a in jarvis.due_alerts(DATA, db, at(12, d=27))]                  # через 3 дня снова
    for _ in range(3):
        jarvis.after_sent(db, "buy:T1", at(11))
    assert "buy:T1" not in [a[0] for a in jarvis.due_alerts(DATA, db, at(12, d=30))]              # после трёх напоминаний хватит
    assert jarvis.is_quiet(at(23)) and jarvis.is_quiet(at(7)) and not jarvis.is_quiet(at(10))


def test_tick_sends_purchase_with_buttons_and_respects_night(tmp_path):
    db = Database(tmp_path / "j2.db")
    db.init()
    db.snapshot_put([("k" * 40, {"title": "Что-то", "type": "note"})])
    settings = make_settings(tmp_path, telegram_allowed_ids=frozenset({ME}), ada_summary_url="https://ada.invalid/api/secretary",
                             ada_summary_token="t" * 24)

    async def fake_raw(_settings, http_client=None):
        return DATA
    ada.fetch_raw, original = fake_raw, ada.fetch_raw
    scheduler.ada_module.fetch_raw = fake_raw
    try:
        night = Sender()
        asyncio.run(scheduler.tick(db, settings, at(23, 30), night))
        assert not [s for s in night.sent if "Крупная покупка" in s[1]]
        day = Sender()
        asyncio.run(scheduler.tick(db, settings, at(11), day))
        texts = [s[1] for s in day.sent]
        assert any("Крупная покупка" in t for t in texts) and len([t for t in texts if "Крупная покупка" in t]) == 2
        buttons = next(s[2] for s in day.sent if "Крупная покупка" in s[1])
        assert [b[1].split(":")[0] for row in buttons for b in row] == ["pd", "pw", "pl", "px"]
    finally:
        scheduler.ada_module.fetch_raw = original


def test_reminder_buttons_adapt_to_time_of_day():
    morning = scheduler.reminder_buttons("a" * 12, "b" * 12, at(9))
    evening = scheduler.reminder_buttons("a" * 12, "b" * 12, at(18))
    assert any("snzev" in b[1] for row in morning for b in row) and not any("snz3d" in b[1] for row in morning for b in row)
    assert any("snz3d" in b[1] for row in evening for b in row)
    flat = [b[1] for row in morning for b in row]
    assert "ok:" + "a" * 12 in flat and "cx:" + "b" * 12 in flat and "it:" + "b" * 12 in flat


def test_purchase_buttons_flow_docs_and_warranty(tg, tmp_path):
    client = tg(StubLLM(result(note(type="document", title="Гарантия", body="x", confidence=0.9))))
    db = Database(tmp_path / "data" / "brain.db")
    jarvis._save(db, jarvis.META, {"abc123abc123": {"id": "buy:T1", "title": "ковёр", "date": "2026-10-15", "amount": 100000, "merchant": ""}})

    def press(data, uid):
        return send(client, {"update_id": uid, "callback_query": {"id": str(uid), "from": {"id": ME}, "data": data,
                    "message": {"message_id": 5, "chat": {"id": ME}}}}).json()
    assert "чека" in press("pd:abc123abc123", 70)["text"]
    sent = send(client, update("вот чек на ковёр", uid=71)).json()
    assert "Сохранил к покупке" in sent["text"]
    assert client.get("/pending", headers=auth()).json()["count"] == 1
    assert "закрыли" in send(client, update("готово", uid=72)).json()["text"]
    press("pw:abc123abc123", 73)
    assert "Не понял срок" in send(client, update("потом скажу", uid=74)).json()["text"]
    assert "Гарантия до" in send(client, update("2 года", uid=75)).json()["text"]
    assert "buy:T1" in jarvis._load(db, jarvis.SEEN, [])


def test_news_feed_parsing_first_run_and_dedupe(tmp_path):
    rss = """<rss><channel>
      <item><title>Security incident: customer secrets may have been exposed</title><link>https://s/1</link>
            <guid>g1</guid><pubDate>Wed, 21 Oct 2026 07:24:00 GMT</pubDate><description>Rotate your API keys</description></item>
      <item><title>Elevated build times</title><link>https://s/2</link><guid>g2</guid><pubDate>Mon, 01 Jun 2026 07:24:00 GMT</pubDate></item>
    </channel></rss>"""
    items = news.parse_feed(rss, "Railway")
    assert [i.critical for i in items] == [True, False] and items[0].uid == "Railway|g1"
    assert news.parse_feed("<not xml", "X") == []
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Outage</title><id>a1</id><updated>2026-10-22T10:00:00Z</updated><link href="https://s/a"/></entry></feed>'
    assert news.parse_feed(atom, "GitHub")[0].link == "https://s/a"

    db = Database(tmp_path / "n.db")
    db.init()
    client_ok = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=rss)))
    now = datetime(2026, 10, 21, 20, 0, tzinfo=TZ)

    async def run(now_):
        return await news.fetch_new(db, [("Railway", "https://x/feed")], now_, client_ok)
    first = asyncio.run(run(now))
    assert [i.uid for i in first] == ["Railway|g1"]                      # старое (июнь) молча запомнили, свежее показали
    assert asyncio.run(run(now)) == []                                   # повторов нет
    assert "🔴 Безопасность" in news.format_item(first[0])
    broken = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert asyncio.run(news.fetch_new(db, [("Railway", "https://x/feed")], now, broken)) == []   # сбой ленты молча
