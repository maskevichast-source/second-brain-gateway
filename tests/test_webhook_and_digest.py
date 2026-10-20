import asyncio
import json
import time
from datetime import date, datetime

import httpx

from app import ask, webhook
from app.vault import VaultNote
from .conftest import make_settings


def _settings(tmp_path, **kw):
    return make_settings(tmp_path, telegram_bot_token="123:ABC", telegram_secret="s" * 20,
                         public_url="https://sb.example.app", **kw)


def _client(state):
    def handler(request):
        path = request.url.path
        if path.endswith("/getWebhookInfo"):
            return httpx.Response(200, json={"ok": True, "result": state["info"]})
        if path.endswith("/setWebhook"):
            state["set"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_webhook_is_set_again_when_missing_or_wrong(tmp_path):
    for info in ({"url": ""}, {"url": "https://old.example/telegram/webhook"}):
        state = {"info": info}
        assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), _client(state))) == "set"
        assert state["set"]["url"] == "https://sb.example.app/telegram/webhook"
        assert state["set"]["secret_token"] == "s" * 20 and "callback_query" in state["set"]["allowed_updates"]


def test_webhook_left_alone_when_correct_and_skipped_without_config(tmp_path):
    state = {"info": {"url": "https://sb.example.app/telegram/webhook"}}
    assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), _client(state))) == "ok" and "set" not in state
    state = {"info": {"url": "https://sb.example.app/telegram/webhook", "last_error_message": "Wrong response"}}
    assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), _client(state))) == "set"   # была ошибка доставки
    url = "https://sb.example.app/telegram/webhook"
    fresh = {"info": {"url": url, "last_error_message": "Wrong response", "last_error_date": int(time.time()) - 600}}
    assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), _client(fresh))) == "set"
    stale = {"info": {"url": url, "last_error_message": "Wrong response", "last_error_date": int(time.time()) - 5 * 3600}}
    assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), _client(stale))) == "ok" and "set" not in stale
    bare = make_settings(tmp_path)
    assert asyncio.run(webhook.ensure_webhook(bare)) == "skipped" and not webhook.configured(bare)


def test_webhook_failure_is_reported_not_raised(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    assert asyncio.run(webhook.ensure_webhook(_settings(tmp_path), client)) == "failed"


def note(title, type_, **front):
    return VaultNote(index=1, front={"title": title, "type": type_, **front})


def test_digest_shows_task_once_when_due_and_remind_are_the_same_day():
    today = date(2026, 10, 17)
    notes = [note("Позвонить маме в 21:00", "task", status="открыто", due="2026-10-17", remind_at="2026-10-17T21:00")]
    items = ask.collect_items(notes, today)
    assert len(items) == 1 and items[0].label == "дело"
    text = ask.build_digest(notes, datetime(2026, 10, 17, 8, 30))
    assert text.count("Позвонить маме") == 1


def test_remind_only_task_and_other_day_remind_are_kept():
    today = date(2026, 10, 17)
    only = [note("Только напоминание", "task", status="открыто", remind_at="2026-10-17T21:00")]
    assert [i.label for i in ask.collect_items(only, today)] == ["напоминание"]
    both = [note("Срок позже", "task", status="открыто", due="2026-10-20", remind_at="2026-10-17T09:00")]
    assert len(ask.collect_items(both, today)) == 2


def test_digest_dedupes_same_birthday_from_card_and_duplicate_note():
    today = date(2026, 10, 17)
    notes = [note("Владислав Маскевич", "person", birthday="10-29"), note("Владислав Маскевич", "person", birthday="10-29")]
    assert len([i for i in ask.collect_items(notes, today) if i.label == "день рождения"]) == 1
