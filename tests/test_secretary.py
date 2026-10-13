import asyncio

import httpx

from app import ada
from app.ask import build_ask_message, build_digest
from app.review import build_brief, build_week_review
from app.vault import load_notes
from .conftest import FIXED_NOW, StubAsk, auth, make_settings, vault_note

SUMMARY = {
    "month_label": "Октябрь 2026", "day": 12, "days_in_month": 31, "currency": "₸",
    "income": 900000, "expense": 450000, "balance": 450000,
    "prev_expense": 400000, "expense_delta_pct": 12.5,
    "top_categories": [{"name": "Продукты", "spent": 120000, "limit": 100000, "status": "превышен"}],
    "debts": {"lent_total": 50000, "borrowed_total": 0, "soon": [{"who": "Серик", "amount": 50000, "due": "2026-10-20"}]},
    "subscriptions": {"month_total": 15000, "upcoming": [{"name": "Netflix", "amount": 4000, "date": "2026-10-15"}]},
    "goals": [{"name": "Отпуск", "saved": 200000, "target": 800000}],
}


def corpus(*parts):
    return "\n=====NOTE=====\n".join(parts)


def test_format_summary_full():
    text = ada.format_summary(SUMMARY)
    assert "Октябрь 2026 (день 12 из 31)" in text
    assert "450 000 ₸" in text and "на 12% больше" in text
    assert "Продукты 120 000 ₸ из 100 000 ₸ (превышен)" in text
    assert "Серик: 50 000 ₸, срок 2026-10-20" in text
    assert "Netflix" in text and "Отпуск 200 000 ₸ из 800 000 ₸" in text


def test_format_summary_tolerates_garbage():
    assert ada.format_summary({}) == ""
    assert ada.format_summary("x") == ""
    assert ada.format_summary({"top_categories": [1, None], "debts": "?", "goals": ["x"]}) == ""


def test_fetch_summary_caches_and_survives_errors(tmp_path):
    ada.clear_cache()
    calls = []

    def handler(request):
        calls.append(request.headers.get("x-secretary-token"))
        return httpx.Response(200, json=SUMMARY)

    settings = make_settings(tmp_path, ada_summary_url="https://ada.invalid/api/secretary", ada_summary_token="tok")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            first = await ada.fetch_summary(settings, c)
            second = await ada.fetch_summary(settings, c)
        return first, second

    first, second = asyncio.run(run())
    assert first == second and "Бюджет" in first and calls == ["tok"]

    ada.clear_cache()

    async def fail():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
            return await ada.fetch_summary(settings, c)

    assert asyncio.run(fail()) == ""


def test_fetch_summary_not_configured(tmp_path):
    assert asyncio.run(ada.fetch_summary(make_settings(tmp_path))) == ""


def test_ask_message_contains_finance_block():
    text = build_ask_message("сколько потратили", ["[1] note"], FIXED_NOW, "Asia/Almaty", "Бюджет за октябрь")
    assert "<<<ФИНАНСЫ" in text and "ФИНАНСЫ>>>" in text
    assert "<<<ФИНАНСЫ" not in build_ask_message("q", ["[1] n"], FIXED_NOW, "Asia/Almaty")


def test_digest_appends_finance():
    assert "Бюджет за октябрь" in build_digest([], FIXED_NOW, ada_brief="Бюджет за октябрь:")


def _notes():
    raw = corpus(
        vault_note("Серик", type="person", relation="друг", last_contact="2026-08-01", birthday="10-20",
                   gift_ideas=["книга"], company="Казмунай"),
        vault_note("Созвон с Сериком", type="interaction", created="2026-10-01T10:00", people=["Серик"],
                   agreements="отправить смету"),
        vault_note("Отправить смету Серику", type="task", status="ждёт", people=["Серик"], due="2026-10-10"),
        vault_note("Договор аренды", type="document", status="действует", valid_until="2026-11-20"),
        vault_note("Дюна", type="media", status="хочу", created="2026-10-11T10:00"),
        vault_note("Пересмотреть тариф", type="decision", status="пересмотреть"),
        vault_note("Ссылка", type="bookmark", status="не-разобрано", created="2026-09-01T10:00", stage="inbox"),
    )
    notes, _ = load_notes(raw)
    return notes


def test_week_review_sections():
    text = build_week_review(_notes(), FIXED_NOW, "Бюджет за октябрь:")
    for piece in ("Обзор недели", "Просрочено", "Отправить смету Серику", "Жду ответа",
                  "Давно не общались", "Серик", "Договор аренды", "Список «хочу»", "Разобрать", "Бюджет за октябрь",
                  "пересмотреть"):
        assert piece.lower() in text.lower(), piece


def test_week_review_empty():
    assert "Срочного нет" in build_week_review([], FIXED_NOW)


def test_brief_collects_everything():
    text = build_brief(_notes(), "серик", FIXED_NOW)
    assert text.startswith("Брифинг: Серик")
    assert "Казмунай" in text and "книга" in text and "день рождения: 10-20" in text
    assert "отправить смету" in text and "жду ответа" in text


def test_brief_unknown_person():
    assert "ничего нет" in build_brief(_notes(), "Незнакомец", FIXED_NOW)


def test_brief_without_card_uses_mentions():
    notes, _ = load_notes(vault_note("Звонок", type="interaction", people=["Марат"]))
    assert build_brief(notes, "Марат", FIXED_NOW).startswith("Брифинг: Марат")


# ---------------- API ----------------

def test_digest_week_mode_and_brief_endpoints(make_client):
    client = make_client()
    body = {"corpus": corpus(*[vault_note("Серик", type="person", last_contact="2026-08-01")]), "mode": "week"}
    r = client.post("/digest", json=body, headers=auth())
    assert r.status_code == 200 and "Обзор недели" in r.json()["digest"]
    r = client.post("/brief", json={"corpus": body["corpus"], "person": "Серик"}, headers=auth())
    assert r.status_code == 200 and "Брифинг: Серик" in r.json()["brief"]
    assert client.post("/brief", json={"corpus": body["corpus"]}, headers=auth()).status_code == 422
    assert client.post("/brief", json={"person": "x"}).status_code == 401


def test_finance_endpoint_and_ask_gets_ada(make_client, monkeypatch):
    stub = StubAsk({"answer": "ок", "used": []})
    client = make_client(ask_llm=stub, ada_summary_url="https://ada.invalid/x", ada_summary_token="t")

    async def fake(settings, http_client=None):
        return "Бюджет за октябрь:"

    monkeypatch.setattr(ada, "fetch_summary", fake)
    r = client.get("/finance", headers=auth())
    assert r.json() == {"status": "success", "configured": True, "summary": "Бюджет за октябрь:"}
    assert client.get("/finance").status_code == 401
    r = client.post("/ask", json={"question": "сколько потратили?", "corpus": vault_note("Дюна", type="media")}, headers=auth())
    assert r.status_code == 200 and stub.calls[0]["ada_text"] == "Бюджет за октябрь:"


def test_finance_not_configured(make_client):
    r = make_client().get("/finance", headers=auth())
    assert r.json()["configured"] is False


def test_week_review_counts_diary_days():
    notes, _ = load_notes(corpus(
        vault_note("День 1", type="journal", created="2026-10-11T21:00"),
        vault_note("День 2", type="journal", created="2026-10-10T21:00"),
        vault_note("Давно", type="journal", created="2026-09-01T21:00")))
    assert "Дневник: записи за 2 из 7" in build_week_review(notes, FIXED_NOW)
