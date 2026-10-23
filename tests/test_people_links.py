import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import people_links as pl
from app import review
from app.db import Database
from app.snapshot import to_notes

TZ = ZoneInfo("Asia/Almaty")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)


def note(title, typ, **extra):
    lines = ["---", "schema: 1", f'title: "{title}"', f'type: "{typ}"', 'created: 2026-10-20T10:00']
    for k, v in extra.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f'  - "{x}"' for x in v]
        else:
            lines.append(f'{k}: "{v}"')
    return "\n".join(lines + ["---", "", "x", ""])


def make_db(tmp_path, *fronts):
    db = Database(tmp_path / "data" / "brain.db")
    db.init()
    from app.snapshot import rows_from_markdown
    rows = []
    for md in fronts:
        rows += rows_from_markdown(md)
    db.snapshot_put(rows)
    return db


def test_strip_label_and_find_card(tmp_path):
    assert pl.strip_label("Eichholtz Рабочий") == "Eichholtz"
    assert pl.strip_label("Рабочий") == "Рабочий"
    assert pl.strip_label("Мама Домашний 2") == "Мама"
    db = make_db(tmp_path, note("Eichholtz", "person"), note("Серик", "person", aliases=["Серёга"]))
    items = db.snapshot_items()
    assert pl.find_card(items, "Eichholtz Рабочий")[1]["title"] == "Eichholtz"
    assert pl.find_card(items, "серёга")[1]["title"] == "Серик"
    assert pl.find_card(items, "Никто") is None


def test_mark_contact_moves_last_contact_and_queues_set(tmp_path):
    db = make_db(tmp_path, note("Серик", "person", last_contact="2026-09-01"))
    key, front = db.snapshot_items()[0]
    assert pl.mark_contact(db, key, front, date(2026, 10, 23), NOW) is True
    assert db.snapshot_items()[0][1]["last_contact"] == "2026-10-23"
    assert not pl.mark_contact(db, key, db.snapshot_items()[0][1], date(2026, 10, 22), NOW)   # старее: не трогаем


def test_auto_contacts_from_interaction(tmp_path):
    db = make_db(tmp_path, note("Серик", "person"))
    md = note("Звонок Серику", "interaction", people=["[[Серик]]"])
    assert pl.auto_contacts(db, [{"type": "interaction", "markdown": md}], NOW) == ["Серик"]
    assert db.snapshot_items()[0][1]["last_contact"] == "2026-10-20"
    assert pl.auto_contacts(db, [{"type": "task", "markdown": md}], NOW) == []


def test_link_project_only_for_matching_open_task(tmp_path):
    db = make_db(tmp_path, note("Ремонт кухни", "project", status="в работе"), note("Дача", "project", status="сделано"))
    items = db.snapshot_items()
    task = note("Купить шторы для ремонта кухни", "task")
    assert 'project: "[[Ремонт кухни]]"' in pl.link_project(task, items)
    assert pl.link_project(note("Позвонить на дачу", "task"), items) == note("Позвонить на дачу", "task")
    linked = note("Купить шторы для ремонта кухни", "task", project="[[Другой]]")
    assert pl.link_project(linked, items) == linked
    assert pl.link_project(note("Ремонт кухни встреча", "event"), items).count("project:") == 0


def test_followup_after_event_with_people(tmp_path):
    ev = note("Встреча с Сериком", "event", status="запланировано", event_at="2026-10-23T09:00", people=["[[Серик]]"])
    db = make_db(tmp_path, ev, note("Серик", "person"))
    items = db.snapshot_items()
    due = pl.due_followups(items, db, NOW)
    assert len(due) == 1 and "Как прошло" in due[0][1] and "Серик" in due[0][1]
    db.set_setting(due[0][2], "1")
    assert pl.due_followups(items, db, NOW) == []
    early = datetime(2026, 10, 23, 9, 40, tzinfo=TZ)                    # событие ещё идёт (до конца час + 30 минут)
    db2 = make_db(tmp_path / "b", ev)
    assert pl.due_followups(db2.snapshot_items(), db2, early) == []


def test_followup_skips_events_without_people_and_done(tmp_path):
    a = note("Билеты", "event", status="запланировано", event_at="2026-10-23T09:00")
    b = note("Встреча", "event", status="состоялось", event_at="2026-10-23T09:00", people=["[[Серик]]"])
    db = make_db(tmp_path, a, b)
    assert pl.due_followups(db.snapshot_items(), db, NOW) == []


def test_event_done_marks_participants(tmp_path):
    ev = note("Встреча с Сериком", "event", status="запланировано", event_at="2026-10-22T09:00", people=["[[Серик]]"])
    db = make_db(tmp_path, ev, note("Серик", "person"))
    key, front = next((k, f) for k, f in db.snapshot_items() if f["type"] == "event")
    assert pl.event_done(db, key, front, NOW) == ["Серик"]
    assert next(f for _, f in db.snapshot_items() if f["type"] == "person")["last_contact"] == "2026-10-22"


def test_brief_has_events_gifts_documents_and_next_contact(tmp_path):
    fronts = [
        note("Серик", "person", orbit="1", last_contact="2026-10-01"),
        note("Встреча с Сериком", "event", status="запланировано", event_at="2026-10-30T10:00", people=["[[Серик]]"]),
        note("Часы в подарок", "product", status="хочу", for_whom="[[Серик]]"),
        note("Договор с Сериком", "document", counterparty="Серик"),
    ]
    db = make_db(tmp_path, *fronts)
    notes = to_notes([f for _, f in db.snapshot_items()])
    text = review.build_brief(notes, "Серик", NOW)
    assert "Ближайшие события" in text and "Встреча с Сериком" in text
    assert "Купить или подарить" in text and "Часы в подарок" in text
    assert "Документы и дела" in text and "Договор с Сериком" in text
    assert "Следующий контакт" in text and "просрочен" in text


def test_radar_buttons_for_overdue_people(tmp_path):
    db = make_db(tmp_path, note("Серик", "person", orbit="1", last_contact="2026-08-01"))
    names: list[str] = []
    notes = to_notes([f for _, f in db.snapshot_items()])
    review.build_radar(notes, NOW.date(), names_out=names)
    assert names == ["Серик"]
    buttons = pl.radar_buttons(db.snapshot_items(), names)
    assert buttons[0][0][1].startswith("pc:") and buttons[0][1][1].startswith("pq:")


# ---------- через Telegram ----------

from .conftest import StubLLM, result  # noqa: E402
from .conftest import note as llm_note  # noqa: E402
from .test_telegram import ME, send, tg, update  # noqa: E402,F401


def _press(client, data, uid):
    return send(client, {"update_id": uid, "callback_query": {"id": str(uid), "from": {"id": ME}, "data": data,
                "message": {"message_id": 5, "chat": {"id": ME}}}}).json()


def _db(tmp_path):
    return Database(tmp_path / "data" / "brain.db")


def test_person_buttons_contact_and_cadence(tg, tmp_path):
    client = tg(StubLLM(result(llm_note(type="note", title="x", body="x", confidence=0.9))))
    db = _db(tmp_path)
    from app.snapshot import rows_from_markdown
    db.snapshot_put(rows_from_markdown(note("Серик", "person", orbit="1")))
    key = db.snapshot_items()[0][0][:12]
    assert "связались сегодня" in _press(client, f"pc:{key}", 90)["text"]
    assert db.snapshot_items()[0][1]["last_contact"]
    assert "раз в год" in _press(client, f"pq:{key}", 91)["text"]
    assert db.snapshot_items()[0][1]["cadence_days"] == 365


def test_add_fact_button_saves_with_known_card_data(tg, tmp_path):
    llm = StubLLM(result(llm_note(type="person", title="Серик", body="x", confidence=0.9)))
    client = tg(llm)
    db = _db(tmp_path)
    from app.snapshot import rows_from_markdown
    db.snapshot_put(rows_from_markdown(note("Серик", "person", company="Банк")))
    key = db.snapshot_items()[0][0][:12]
    assert "Что добавить про Серик" in _press(client, f"pf:{key}", 92)["text"]
    reply = send(client, update("играет в теннис", uid=93)).json()
    assert "добавил в карточку" in reply["text"]
    assert llm.calls and "Банк" in str(llm.calls[-1].get("content") or llm.calls[-1])


def test_event_followup_buttons_close_event_and_mark_contact(tg, tmp_path):
    client = tg(StubLLM(result(llm_note(type="note", title="x", body="x", confidence=0.9))))
    db = _db(tmp_path)
    from app.snapshot import rows_from_markdown
    db.snapshot_put(rows_from_markdown(note("Серик", "person")) + rows_from_markdown(
        note("Встреча с Сериком", "event", status="запланировано", event_at="2026-10-10T09:00", people=["[[Серик]]"])))
    event_key = next(k for k, f in db.snapshot_items() if f["type"] == "event")[:12]
    assert "состоялось" in _press(client, f"eo:{event_key}", 94)["text"]
    items = dict((f["type"], f) for _, f in db.snapshot_items())
    assert items["event"]["status"] == "состоялось" and items["person"]["last_contact"] == "2026-10-10"


# ---------- вопросы без команды ----------

def test_is_question_routes_short_retrieval_questions_only():
    from app.telegram import is_question
    for q in ("когда щелкунчик?", "Что я обещал Серику?", "когда там вылет в Алмату?", "Где мой паспорт", "напомни когда щелкунчик",
              "а сколько осталось платить по Forte?", "есть ли гарантия на пылесос"):
        assert is_question(q), q
    for t in ("Что-то надо купить молоко", "купить шторы", "напомни купить молоко", "что такое\nвторая строка",
              "Переслано от Серик: когда встреча", "https://example.com когда", "Я обещал Серику скинуть контакт?", "!что я принимаю",
              "когда" + " очень длинно" * 30):
        assert not is_question(t), t


def test_free_question_goes_to_ask_and_save_fallback(tg, tmp_path):
    from app.snapshot import rows_from_markdown

    answers = iter([{"answer": "Щелкунчик 3 ноября.", "used": [1]}, {"answer": "Про стулья ничего нет.", "used": []}])

    async def ask_stub(settings, **kwargs):
        return next(answers)

    client = tg(StubLLM(result(llm_note(type="note", title="Мысль про стулья", body="x", confidence=0.9))), ask_llm=ask_stub)
    _db(tmp_path).snapshot_put(rows_from_markdown(note("Щелкунчик", "event", event_at="2026-11-03T19:00")))
    first = send(client, update("когда щелкунчик?", uid=200)).json()["text"]
    assert "3 ноября" in first and "Источники" in first and "сохрани" not in first
    second = send(client, update("где та самая мысль про стулья?", uid=201)).json()["text"]
    assert "ответь «сохрани»" in second
    saved = send(client, update("сохрани", uid=202)).json()["text"]
    assert "Мысль про стулья" in saved or "Записал" in saved
