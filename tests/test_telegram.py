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
    photo = {"update_id": 4, "message": {"from": {"id": ME}, "chat": {"id": ME, "type": "private"}, "photo": [{}]}}
    assert "только текст" in send(client, photo).json()["text"]
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
