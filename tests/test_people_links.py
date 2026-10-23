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

from .conftest import StubLLM, auth, result  # noqa: E402
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


# ---------- «вышли файл» ----------

def test_file_request_detection():
    from app import filebox
    for q in ("Вышли мне файл билета", "пришли договор с мебельщиками", "отправь скан паспорта", "скинь чек на пылесос", "покажи билет"):
        assert filebox.is_request(q), q
    for t in ("Выслать файл билета Диане завтра", "когда щелкунчик?", "купить билет", "скинь контакт Серика", "/ask вышли файл"):
        assert not filebox.is_request(t), t


def _doc_message(uid, caption, file_id, name="ticket.pdf"):
    return {"update_id": uid, "message": {"message_id": uid, "from": {"id": ME, "first_name": "Влад"}, "chat": {"id": ME, "type": "private"},
            "date": 0, "caption": caption, "document": {"file_id": file_id, "file_name": name, "mime_type": "application/octet-stream", "file_size": 1000}}}


def test_remember_file_only_and_send_it_back(tg, tmp_path):
    client = tg(StubLLM(result(llm_note(type="task", title="Не должно появиться", body="x", confidence=0.9))))
    reply = send(client, _doc_message(300, "Билет Щелкунчик 5 декабря, только файл", "FILE-ID-1")).json()
    assert "Запомнил файл" in reply["text"] and "Заметку не создавал" in reply["text"]
    assert client.get("/pending", headers=auth()).json()["count"] == 0          # заметка не создавалась
    sent = send(client, update("Вышли мне файл билета на щелкунчик", uid=301)).json()
    assert sent["method"] == "sendDocument" and sent["document"] == "FILE-ID-1" and "Щелкунчик" in sent["caption"]
    none = send(client, update("вышли договор с мебельщиками", uid=302)).json()
    assert none["method"] == "sendMessage" and "ничего похожего" in none["text"]            # не превращается в дело


def test_file_request_with_several_matches_offers_buttons(tg, tmp_path):
    client = tg(StubLLM(result(llm_note(type="note", title="x", body="x", confidence=0.9))))
    send(client, _doc_message(310, "Билет Щелкунчик, только файл", "F-A"))
    send(client, _doc_message(311, "Билет Алмата вылет, только файл", "F-B", name="flight.pdf"))
    reply = send(client, update("пришли файл билета", uid=312)).json()
    assert reply["method"] == "sendMessage" and "несколько" in reply["text"]
    data = [b["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    got = send(client, {"update_id": 313, "callback_query": {"id": "313", "from": {"id": ME}, "data": data[0],
               "message": {"message_id": 5, "chat": {"id": ME}}}}).json()
    assert got["method"] == "sendDocument" and got["document"] in ("F-A", "F-B")


def test_private_files_are_not_remembered(tg, tmp_path):
    from app import filebox
    client = tg(StubLLM(result(llm_note(type="note", title="x", body="x", confidence=0.9))))
    send(client, _doc_message(320, "! личный документ", "F-PRIV"))
    assert filebox.search(_db(tmp_path), "личный документ") == []


def test_file_sent_with_a_note_is_remembered_by_note_title(tg, tmp_path):
    client = tg(StubLLM(result(llm_note(type="document", title="Билет на Щелкунчик", body="Билет", confidence=0.9))))
    send(client, _doc_message(330, "Билет на Щелкунчик", "F-NOTE"))
    sent = send(client, update("вышли билет на щелкунчик", uid=331)).json()
    assert sent["method"] == "sendDocument" and sent["document"] == "F-NOTE"


# ---------- повторы ----------

def test_identical_event_and_task_are_not_saved_twice(tg, tmp_path):
    from app.snapshot import rows_from_markdown
    ev = result(llm_note(type="event", status="запланировано", title="Щелкунчик", body="x", fields={"event_at": "2026-12-05T18:00"}))
    client = tg(StubLLM(ev))
    _db(tmp_path).snapshot_put(rows_from_markdown(note("Щелкунчик в театре", "event", status="запланировано", event_at="2026-12-05T18:00")))
    first = send(client, update("щелкунчик 5 декабря в 18:00", uid=400)).json()["text"]
    assert "Щелкунчик" in first
    other_day = result(llm_note(type="event", status="запланировано", title="Щелкунчик в театре", body="x", fields={"event_at": "2026-12-06T18:00"}))
    client2 = tg(StubLLM(other_day))
    assert "уже записано" not in send(client2, update("щелкунчик 6 декабря", uid=401)).json()["text"]


def test_repeat_check_skips_notes_with_sources_and_private(tmp_path):
    from app import dedupe
    from app.snapshot import rows_from_markdown
    items = rows_from_markdown(note("Приём у врача", "event", status="запланировано", event_at="2026-11-02T10:00"))
    new = {"filename": "a.md", "markdown": note("Приём у врача", "event", status="запланировано", event_at="2026-11-02T10:00").replace("2026-10-20T10:00", "2026-10-24T03:00")}
    assert dedupe.drop_repeats([new], items, has_sources=False)[1] == ["Приём у врача"]
    assert dedupe.drop_repeats([new], items, has_sources=True) == ([new], [])
    task = {"filename": "t.md", "markdown": note("Позвонить сестре", "task", status="открыто", due="2026-11-02T10:00").replace("2026-10-20T10:00", "2026-10-24T03:00")}
    same = rows_from_markdown(note("Позвонить сестре", "task", status="открыто", due="2026-11-02T10:00"))
    assert dedupe.drop_repeats([task], same, has_sources=False)[1] == ["Позвонить сестре"]
    nodue = {"filename": "t2.md", "markdown": note("Позвонить сестре", "task", status="открыто").replace("2026-10-20T10:00", "2026-10-24T03:00")}
    assert dedupe.drop_repeats([nodue], same, has_sources=False)[1] == []                  # без срока не считаем повтором


def test_followup_question_gets_previous_answer_as_context(tg, tmp_path):
    from app.snapshot import rows_from_markdown
    seen = []

    async def ask_stub(settings, **kwargs):
        seen.append(kwargs["question"])
        return {"answer": "Щелкунчик 5 декабря в 18:00.", "used": [1]}

    client = tg(StubLLM(result(llm_note(type="note", title="x", body="x", confidence=0.9))), ask_llm=ask_stub)
    _db(tmp_path).snapshot_put(rows_from_markdown(note("Щелкунчик", "event", event_at="2026-12-05T18:00")))
    send(client, update("когда щелкунчик?", uid=410))
    send(client, update("а во сколько?", uid=411))
    assert "уточнение к прошлому вопросу «когда щелкунчик?»" in seen[1] and "5 декабря" in seen[1]
    assert "уточнение" not in seen[0]
    send(client, update("где там парковка?", uid=412))
    assert "щелкунчик" in seen[2].lower()


def test_followup_detection():
    from app.telegram import is_followup
    for t in ("а во сколько?", "с кем я иду?", "где это?", "и сколько стоит", "парковка есть?"):
        assert is_followup(t), t
    assert not is_followup("когда у меня встреча с Сериком и Арманом по поводу ремонта на следующей неделе в офисе?")
