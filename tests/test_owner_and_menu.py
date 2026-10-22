import asyncio
from datetime import date

import httpx

from app import ask, contacts_review, owner, review, webhook
from types import SimpleNamespace
from app.snapshot import to_notes

from .conftest import StubLLM, auth, note, result
from .test_telegram import ME, WEBHOOK, send, tg, update  # noqa: F401


def teardown_function(_):
    owner.set_names([])


def test_owner_matches_any_word_order_and_ignores_suffixes():
    owner.set_names(["Владислав Маскевич", "Влад"])
    assert owner.is_owner("Маскевич Владислав")
    assert owner.is_owner("владислав маскевич — профиль")
    assert owner.is_owner("Влад")
    assert not owner.is_owner("Владислав Петров") and not owner.is_owner("Маскевич Айдар")
    owner.set_names([])
    assert not owner.is_owner("Маскевич Владислав")


def test_owner_is_not_asked_about_or_reminded():
    owner.set_names(["Владислав Маскевич"])
    cards = [("a" * 40, {"title": "Маскевич Владислав", "type": "person", "phones": "+7 701", "birthday": "2026-10-25"}),
             ("b" * 40, {"title": "Арман", "type": "person", "phones": "+7 702"})]
    assert contacts_review.build_queue(cards, set()) == ["b" * 12]
    notes = to_notes([f for _, f in cards])
    items = ask.collect_items(notes, date(2026, 10, 24))
    assert not [i for i in items if i.label == "день рождения"]            # свой день рождения не напоминаем
    cards_with_cadence = to_notes([{"title": "Маскевич Владислав", "type": "person", "orbit": "0"}])
    assert "Маскевич" not in review.build_radar(cards_with_cadence, date(2026, 10, 24))


def test_telegram_account_name_becomes_owner_and_me_command_overrides(tg):
    client = tg()
    payload = update("/me", uid=60)
    payload["message"]["from"].update({"first_name": "Владислав", "last_name": "Маскевич"})
    assert "Владислав Маскевич" in send(client, payload).json()["text"]
    assert owner.is_owner("Маскевич Владислав")
    reply = send(client, update("/me Владислав Маскевич, Влад", uid=61)).json()["text"]
    assert "Влад" in reply and owner.is_owner("Влад")


def test_owner_line_goes_into_llm_context(tg):
    llm = StubLLM(result(note(type="note", title="Заметка", body="x", confidence=0.9)))
    client = tg(llm)
    send(client, update("/me Владислав Маскевич", uid=62))
    send(client, update("записать мысль", uid=63))
    assert llm.calls and "Владелец бота" in llm.calls[-1]["context"] and "Маскевич" in llm.calls[-1]["context"]


def test_command_menu_is_set_once_and_covers_all_commands():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("getMyCommands"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    settings = SimpleNamespace(telegram_bot_token="123:abc")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await webhook.ensure_commands(settings, client)
    assert asyncio.run(run()) == "set" and calls == ["getMyCommands", "setMyCommands"]
    names = {c for c, _ in webhook.COMMANDS}
    assert {"ask", "day", "contacts", "me", "week", "health", "help"} <= names
    assert all(c == c.lower() and len(c) <= 32 and len(d) <= 256 for c, d in webhook.COMMANDS)
    assert asyncio.run(webhook.ensure_commands(SimpleNamespace(telegram_bot_token=""))) == "skipped"
