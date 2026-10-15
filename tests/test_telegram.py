import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vault_sync  # noqa: E402

from .conftest import StubLLM, auth, note, result  # noqa: E402

SECRET = "s" * 20
ME = 111
WEBHOOK = {"x-telegram-bot-api-secret-token": SECRET}


def update(text, uid=1, user=ME, chat_type="private", **extra):
    message = {"message_id": uid, "from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text}
    message.update(extra)
    return {"update_id": uid, "message": message}


@pytest.fixture
def tg(make_client):
    def _make(llm=None, **overrides):
        overrides.setdefault("telegram_secret", SECRET)
        overrides.setdefault("telegram_allowed_ids", frozenset({ME}))
        return make_client(llm or StubLLM(result(note(type="task", title="Купить кабель", body="Купить кабель"))), **overrides)
    return _make


def send(client, payload, headers=WEBHOOK):
    return client.post("/telegram/webhook", json=payload, headers=headers)


def test_webhook_is_off_without_secret(make_client):
    assert send(make_client(StubLLM(), telegram_secret=""), update("привет")).status_code == 404


def test_wrong_or_missing_secret_is_rejected(tg):
    client = tg()
    assert send(client, update("привет"), headers={}).status_code == 401
    assert send(client, update("привет"), headers={"x-telegram-bot-api-secret-token": "x" * 20}).status_code == 401


def test_setup_mode_tells_user_their_id(tg):
    reply = send(tg(telegram_allowed_ids=frozenset()), update("привет", user=777)).json()
    assert reply["method"] == "sendMessage" and "777" in reply["text"] and "TELEGRAM_ALLOWED_USER_IDS" in reply["text"]


def test_strangers_and_groups_are_ignored(tg):
    llm = StubLLM(result(note(body="x")))
    client = tg(llm)
    assert send(client, update("привет", user=999)).json() == {}
    assert send(client, update("привет", chat_type="group")).json() == {}
    assert send(client, {"update_id": 5, "edited_message": {}}).json() == {}
    assert llm.calls == []


def test_text_goes_to_queue_and_is_acknowledged(tg):
    client = tg()
    reply = send(client, update("купить кабель HDMI")).json()
    assert reply["chat_id"] == ME and "Дело: Купить кабель" in reply["text"]
    queue = client.get("/pending", headers=auth()).json()
    assert queue["count"] == 1 and queue["items"][0]["folder"] == "0-Inbox"
    assert 'source: "telegram"' in queue["items"][0]["markdown"]
    removed = client.post("/ack", json={"ids": [queue["items"][0]["id"]]}, headers=auth()).json()
    assert removed["removed"] == 1 and client.get("/pending", headers=auth()).json()["count"] == 0


def test_update_retry_does_not_duplicate(tg):
    llm = StubLLM(result(note(type="task", title="Дело", body="д")))
    client = tg(llm)
    first = send(client, update("сделать дело", uid=42)).json()
    second = send(client, update("сделать дело", uid=42)).json()
    assert first == second and len(llm.calls) == 1
    assert client.get("/pending", headers=auth()).json()["count"] == 1


def test_clarification_round_trip(tg):
    ambiguous = result(note(type="task", title="Позвонить", body="Позвонить"), needs_review=True, questions=["Кому?"])
    llm = StubLLM(ambiguous)
    client = tg(llm)
    first = send(client, update("позвонить тому парню", uid=1)).json()
    assert "Кому?" in first["text"] and "/skip" in first["text"]
    assert client.get("/pending", headers=auth()).json()["count"] == 0       # ничего не сохранено до ответа
    second = send(client, update("Серику", uid=2)).json()
    assert llm.calls[1]["revision"] == 2 and llm.calls[1]["clarifications"] == "Серику"
    assert "Уточнение: Серику" in client.get("/pending", headers=auth()).json()["items"][0]["markdown"]
    assert second["chat_id"] == ME
    # состояние ожидания очищено
    assert send(client, update("/skip", uid=3)).json()["text"] == "Сейчас нечего пропускать."


def test_skip_saves_as_is(tg):
    ambiguous = result(note(type="task", title="Позвонить", body="Позвонить"), needs_review=True, questions=["Кому?"])
    client = tg(StubLLM(ambiguous))
    send(client, update("позвонить тому парню", uid=1))
    send(client, update("/skip", uid=2))
    assert client.get("/pending", headers=auth()).json()["count"] == 1


def test_private_message_skips_llm(tg):
    llm = StubLLM(result(note(body="x")))
    client = tg(llm)
    reply = send(client, update("! код от сейфа 4321")).json()
    assert "без ИИ" in reply["text"] and llm.calls == []
    assert "4321" in client.get("/pending", headers=auth()).json()["items"][0]["markdown"]


def test_commands_media_and_limits(tg):
    client = tg(max_content_chars=50)
    assert "Пришли мысль" in send(client, update("/start", uid=1)).json()["text"]
    assert "Ждут выгрузки в хранилище: 0" in send(client, update("/status", uid=2)).json()["text"]
    assert "Не знаю такой команды" in send(client, update("/abracadabra", uid=3)).json()["text"]
    photo = {"update_id": 4, "message": {"from": {"id": ME}, "chat": {"id": ME, "type": "private"}, "sticker": {}}}
    assert "Принимаю текст" in send(client, photo).json()["text"]
    assert "Слишком длинно" in send(client, update("а" * 51, uid=5)).json()["text"]


def test_internal_error_does_not_trigger_telegram_retries(tg):
    class Boom:
        async def __call__(self, *a, **k):
            raise RuntimeError("сломалось")

    client = tg(Boom())
    response = send(client, update("привет"))
    assert response.status_code == 200 and "Что-то пошло не так" in response.json()["text"]


def test_queue_requires_auth_and_validates(tg):
    client = tg()
    assert client.get("/pending").status_code == 401
    assert client.post("/ack", json={"ids": [1]}).status_code == 401
    assert client.post("/ack", json={"ids": "x"}, headers=auth()).status_code == 422
    assert client.post("/ack", content=b"{oops", headers=auth()).status_code == 422


def test_queue_is_purged_after_ttl(tg, tmp_path):
    client = tg()
    send(client, update("купить кабель"))
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    send(client, update("/status", uid=9))                      # любой save чистит просроченное
    send(client, update("ещё одно", uid=10))
    ids = [row[0] for row in conn.execute("SELECT created_at FROM outbox")]
    assert all(not stamp.startswith("2020") for stamp in ids)


# ---------- скрипт синхронизации ----------

def test_write_note_never_overwrites(tmp_path):
    first = vault_sync.write_note(tmp_path, "0-Inbox", "a.md", "один")
    same = vault_sync.write_note(tmp_path, "0-Inbox", "a.md", "один")
    other = vault_sync.write_note(tmp_path, "0-Inbox", "a.md", "два")
    assert first.name == "a.md" and same is None and other.name == "a-2.md"
    assert (tmp_path / "0-Inbox" / "a.md").read_text(encoding="utf-8") == "один"


def test_write_note_blocks_path_tricks(tmp_path):
    path = vault_sync.write_note(tmp_path, "../../evil", "../../x.md", "z")
    assert path.resolve().is_relative_to(tmp_path.resolve())


def test_sync_once_acks_only_after_writing(tmp_path, monkeypatch):
    calls = []

    def fake_api(url, token, path, body=None):
        calls.append(path)
        if path.startswith("/pending"):
            if "/ack" in calls:
                return {"items": [], "remaining": 0}
            return {"items": [{"id": 7, "folder": "0-Inbox", "filename": "n.md", "markdown": "текст"}], "remaining": 1}
        assert body == {"ids": [7]} and (tmp_path / "0-Inbox" / "n.md").exists()
        return {"removed": 1}

    monkeypatch.setattr(vault_sync, "api", fake_api)
    assert vault_sync.sync_once("http://x", "t", tmp_path) == 1
    assert "/ack" in calls


def test_settings_for_telegram(monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("CAPTURE_TOKEN", "x" * 24)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "короткий")
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "a" * 20)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456;789 abc")
    settings = load_settings()
    assert settings.telegram_secret == "a" * 20 and settings.telegram_allowed_ids == {123, 456, 789}


# ---------- профиль, новые люди, интервью, напоминания ----------

from .conftest import vault_note  # noqa: E402,F401


def people_result(*names, **kw):
    return result(note(type="task", title="Позвонить", body="позвонить", people=list(names), **kw))


def test_new_person_triggers_question_and_card(tg):
    llm = StubLLM(people_result("Серик"))
    client = tg(llm)
    first = send(client, update("позвонить Серику", uid=1)).json()["text"]
    assert "Кто такой(ая) Серик?" in first and "/skip" in first
    llm.value = result(note(type="person", title="Серик", body="друг", fields={"relation": "друг"}))
    second = send(client, update("друг, работает в банке", uid=2)).json()["text"]
    assert "Карточка: Серик" in second
    assert "Данные о человеке по имени Серик" in llm.calls[1]["content"] and llm.calls[1]["revision"] == 2
    people = client.get("/profile", headers=auth()).json()["people"]
    assert people == [{"name": "Серик", "role": "друг", "aliases": []}]
    # во второй раз про него уже не спрашивают
    llm.value = people_result("Серик")
    again = send(client, update("позвонить Серику ещё раз", uid=3)).json()["text"]
    assert "Кто такой" not in again


def test_known_people_are_sent_to_llm_and_canonicalised(tg):
    llm = StubLLM(people_result("серику"))
    client = tg(llm)
    client.post("/profile", json={"context": "Диана — моя жена. УК — Damo Group.",
                                  "people": [{"name": "Серик", "role": "друг", "aliases": ["серику", "Серёга"]}]}, headers=auth())
    reply = send(client, update("позвонить Серику", uid=1)).json()["text"]
    assert "Кто такой" not in reply
    assert "Диана — моя жена" in llm.calls[0]["context"] and "Серик (друг" in llm.calls[0]["people_text"]
    markdown = client.get("/pending", headers=auth()).json()["items"][0]["markdown"]
    assert '"[[Серик]]"' in markdown


def test_person_question_can_be_skipped_and_queue_continues(tg):
    llm = StubLLM(people_result("Аля", "Борис"))
    client = tg(llm)
    first = send(client, update("позвонить Але и Борису", uid=1)).json()["text"]
    assert "Кто такой(ая) Аля?" in first
    assert "Кто такой(ая) Борис?" in send(client, update("/skip", uid=2)).json()["text"]
    assert "пропустил Борис" in send(client, update("/skip", uid=3)).json()["text"]


def test_new_thought_after_person_question_is_a_new_note(tg):
    llm = StubLLM(people_result("Аля"))
    client = tg(llm)
    send(client, update("позвонить Але", uid=1))
    llm.value = result(note(type="idea", title="Идея", body="идея"))
    reply = send(client, update("идея: https://example.com/x очень интересно", uid=2)).json()["text"]
    assert "Идея" in reply and len(llm.calls) == 2 and llm.calls[1]["content"].startswith("идея:")


def test_interview_flow(tg):
    from app import interview

    llm = StubLLM(result(note(type="person", title="Диана", body="жена", fields={"relation": "жена"}, people=["Диана"])))
    client = tg(llm)
    start = send(client, update("/interview", uid=1)).json()["text"]
    assert "Начинаем интервью" in start and "[1/" in start
    second = send(client, update("Владислав, Астана, айти", uid=2)).json()["text"]
    assert "Записал: Диана" in second and "[2/" in second
    assert llm.calls[0]["revision"] == 2 and "Интервью, блок «Ты»" in llm.calls[0]["content"]
    assert "[3/" in send(client, update("/skip", uid=3)).json()["text"]
    assert send(client, update("/stop", uid=4)).json()["text"].startswith("Остановил")
    llm.value = result(note(type="idea", title="Мысль", body="мысль"))
    assert "Мысль" in send(client, update("просто мысль", uid=5)).json()["text"]      # на паузе это обычная запись
    resumed = send(client, update("/interview", uid=6)).json()["text"]
    assert "Продолжаем" in resumed and "[3/" in resumed
    # люди из интервью запоминаются без лишних вопросов
    assert any(p["name"] == "Диана" and p["role"] == "жена" for p in client.get("/profile", headers=auth()).json()["people"])
    assert len(interview.QUESTIONS) >= 20


def test_interview_private_answer_skips_llm(tg):
    llm = StubLLM(result(note(body="x")))
    client = tg(llm)
    send(client, update("/interview", uid=1))
    send(client, update("! только для меня", uid=2))
    assert llm.calls == []
    md = client.get("/pending", headers=auth()).json()["items"][0]["markdown"]
    assert "только для меня" in md and "private: true" in md


def test_reminders_are_built_from_note_fields_and_queued(tg):
    task = result(note(type="task", status="открыто", title="Отправить бумаги", body="x",
                       fields={"due": "2026-10-14"}),
                  note(type="event", status="запланировано", title="Приём", body="y",
                       fields={"event_at": "2026-10-15T10:00", "event_end": "2026-10-15T11:00"}),
                  note(type="task", status="открыто", title="Просроченное", body="z", fields={"due": "2026-10-01"}))
    client = tg(StubLLM(task))
    reply = send(client, update("дела", uid=1)).json()["text"]
    assert "Напоминаний и событий для iPhone: 2" in reply
    items = client.get("/pending", headers=auth()).json()["items"]
    reminders = [r for item in items for r in item["reminders"]]
    assert {(r["kind"], r["date"], r["time"]) for r in reminders} == {("reminder", "2026-10-14", ""), ("event", "2026-10-15", "10:00")}
    capture = client.post("/capture", json={"capture_id": "rem-test-0001", "content": "дела"}, headers=auth()).json()
    assert capture["reminders_count"] == 2 and capture["reminders"][0]["title"] == "Отправить бумаги"


def test_profile_endpoints(tg):
    client = tg()
    assert client.get("/profile").status_code == 401
    assert client.post("/profile", json={"context": "ы" * 9000, "people": [{"name": "Серик"}, {"name": ""}, {"name": "Серик"}]},
                       headers=auth()).json() == {"status": "success", "context_chars": 8000, "people": 1}
    assert client.post("/profile", content=b"{oops", headers=auth()).status_code == 422
    assert client.delete("/profile", headers=auth()).json()["status"] == "success"
    assert client.get("/profile", headers=auth()).json() == {"status": "success", "context": "", "people": []}


def test_capture_can_pass_context_and_people_statelessly(tg):
    llm = StubLLM(people_result("Серик"))
    client = tg(llm)
    data = client.post("/capture", json={"capture_id": "ctx-test-0001", "content": "позвонить Серику",
                                         "context": "Я живу в Астане", "known_people": [{"name": "Серик", "role": "друг"}]},
                       headers=auth()).json()
    assert data["new_people"] == [] and llm.calls[0]["context"] == "Я живу в Астане"
    other = client.post("/capture", json={"capture_id": "ctx-test-0002", "content": "позвонить Серику"}, headers=auth()).json()
    assert other["new_people"] == ["Серик"]            # без списка человек новый; сервер ничего сам не запоминает
    assert client.get("/profile", headers=auth()).json()["people"] == []


def test_interview_blocks_and_single_block(tg):
    from app import interview

    assert len(interview.QUESTIONS) >= 60
    llm = StubLLM(result(note(type="note", title="Дом", body="x")))
    client = tg(llm)
    listing = send(client, update("/interview блоки", uid=1)).json()["text"]
    assert "Дом —" in listing and "Документы —" in listing
    assert "Не знаю такого блока" in send(client, update("/interview абвгд", uid=2)).json()["text"]
    start = send(client, update("/interview дом", uid=3)).json()["text"]
    total = len(interview.block_indices("Дом"))
    assert f"[1/{total}] Дом" in start and f"около {total}" in start
    nxt = send(client, update("ЖК Нурлы", uid=4)).json()["text"]
    assert f"[2/{total}] Дом" in nxt
    for uid in range(5, 5 + total - 2):
        send(client, update("/skip", uid=uid))
    done = send(client, update("/skip", uid=100)).json()["text"]
    assert "Интервью закончено" in done
    # после конца блока обычные сообщения снова записываются как заметки
    assert "[" not in send(client, update("просто текст", uid=101)).json()["text"][:2]


def test_interview_block_survives_pause(tg):
    from app import interview

    client = tg(StubLLM(result(note(body="x"))))
    send(client, update("/interview документы", uid=1))
    send(client, update("/skip", uid=2))
    send(client, update("/stop", uid=3))
    total = len(interview.block_indices("Документы"))
    assert f"[2/{total}] Документы" in send(client, update("/interview", uid=4)).json()["text"]


def test_interview_non_answers_are_not_saved(tg):
    llm = StubLLM(result(note(type="person", title="Диана", body="жена")))
    client = tg(llm)
    send(client, update("/interview люди", uid=1))
    for uid, text in enumerate(["Нет таких", "нет", "Не знаю", "-"], start=2):
        out = send(client, update(text, uid=uid)).json()["text"]
        assert "пропускаю" in out
    assert llm.calls == []                                          # ИИ не вызывался
    assert client.get("/pending", headers=auth()).json()["items"] == []


def test_interview_block_summary_note_and_empty_ai_result(tg):
    from app import interview

    llm = StubLLM(result(note(type="person", title="Серик", body="друг", fields={"relation": "друг"})))
    client = tg(llm)
    send(client, update("/interview документы", uid=1))
    total = len(interview.block_indices("Документы"))
    out = None
    for uid in range(2, 2 + total):
        out = send(client, update("Договор аренды до декабря", uid=uid)).json()["text"]
    assert "Блок «Документы» собран" in out and "Интервью закончено" in out
    items = client.get("/pending", headers=auth()).json()["items"]
    summary = [i for i in items if i["filename"].count("interview")]
    assert len(summary) == 1
    md = summary[0]["markdown"]
    assert "title: \"Интервью — Документы\"" in md and "Договор аренды до декабря" in md and "Оригинал" not in md
    # оригинал заметок интервью без служебной шапки
    person = next(i for i in items if "Серик" in i["markdown"] and "interview" not in i["filename"])
    assert "Интервью, Документы: Договор аренды до декабря" in person["markdown"] and "Вопрос:" not in person["markdown"]


def test_empty_ai_result_saves_nothing(tg):
    from app.llm import LLMResult

    client = tg(StubLLM(LLMResult(notes=[], nothing_to_save=True)))
    send(client, update("/interview дом", uid=1))
    out = send(client, update("Ничего такого не вспомню толком, ЖК вроде Нурлы", uid=2)).json()["text"]
    assert "новых отдельных заметок нет" in out
    assert client.get("/pending", headers=auth()).json()["items"] == []


def test_forwarded_message_keeps_who_sent_it(tg):
    llm = StubLLM(result(note(type="task", title="Прислать смету", body="Прислать смету")))
    client = tg(llm)
    reply = send(client, update("Нужна смета до пятницы", forward_origin={
        "type": "user", "sender_user": {"first_name": "Серик", "last_name": "Ахметов"}})).json()
    assert reply["method"] == "sendMessage"
    assert "Переслано от Серик Ахметов: Нужна смета до пятницы" in llm.calls[0]["content"]


def test_photo_with_caption_and_bare_document_are_saved_without_questions(tg):
    llm = StubLLM(result(note(type="document", title="Чек", body="Чек")))
    client = tg(llm)
    p = update("", uid=5, photo=[{"file_id": "x"}], caption="Чек за замок")
    p["message"].pop("text")
    assert send(client, p).json()["method"] == "sendMessage"
    assert "Чек за замок\n(вложение в Telegram: фото)" in llm.calls[0]["content"]
    d = update("", uid=6, document={"file_id": "y", "file_name": "договор.pdf"})
    d["message"].pop("text")
    send(client, d)
    assert "Вложение в Telegram: файл «договор.pdf»" in llm.calls[1]["content"]
