import re
import sqlite3
import time

import pytest

from app.config import load_settings
from app.db import Database
from app.links import LinkInfo

from .conftest import StubLinks, StubLLM, auth, note, result

CID = "20261012-093000-482913"
TEXT = "купить кабель HDMI до пятницы"


def post(client, **fields):
    body = {"capture_id": CID, "input_type": "text", "content": TEXT}
    body.update(fields)
    return client.post("/capture", json=body, headers=auth())


@pytest.fixture
def task_result():
    return result(
        note(type="task", status="открыто", area="дом", title="Купить кабель HDMI",
             tags=["дом", "покупки"], body="Купить кабель HDMI до пятницы", confidence=0.93)
    )


def test_health_needs_no_auth(make_client):
    assert make_client(StubLLM()).get("/health").json() == {"status": "ok"}


def test_docs_are_hidden(make_client):
    client = make_client(StubLLM())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}, {"x-api-key": "t" * 31}])
def test_auth_required(make_client, headers):
    llm = StubLLM()
    response = make_client(llm).post("/capture", json={"content": "x"}, headers=headers)
    assert response.status_code == 401
    assert llm.calls == []


def test_auth_checked_before_body_validation(make_client):
    assert make_client(StubLLM()).post("/capture", content=b"not json").status_code == 401


def test_happy_path_text(make_client, task_result):
    llm = StubLLM(task_result)
    data = post(make_client(llm)).json()

    assert data["status"] == "success" and data["schema"] == 1
    assert data["needs_review"] is False and data["needs_review_flag"] == "no"
    assert data["count"] == 1 and len(data["files"]) == 1 and data["private"] is False
    file = data["files"][0]
    # обратная совместимость: первый файл продублирован наверху
    assert data["filename"] == file["filename"] and data["markdown"] == file["markdown"]
    assert data["folder"] == "0-Inbox"
    assert re.fullmatch(r"2026-10-12-купить-кабель-hdmi-[0-9a-f]{6}\.md", file["filename"])

    md = file["markdown"]
    assert md.startswith('---\nschema: 1\ntitle: "Купить кабель HDMI"\n')
    assert "created: 2026-10-12T09:30\n" in md
    assert 'type: "task"' in md and 'status: "открыто"' in md and 'stage: "inbox"' in md
    assert "tags:\n  - дом\n  - покупки\n" in md
    assert f'capture_id: "{CID}"' in md
    assert "> [!quote]- Оригинал\n> купить кабель HDMI до пятницы" in md
    assert llm.calls[0]["content"] == TEXT
    assert llm.calls[0]["revision"] == 1


def test_extra_fields_from_future_clients_are_ignored(make_client, task_result):
    response = post(make_client(StubLLM(task_result)), file_base64="", timestamp="2026-10-12T04:30:00Z")
    assert response.status_code == 200


def test_idempotent_by_capture_id(make_client, task_result):
    llm = StubLLM(task_result)
    client = make_client(llm)
    first = post(client).json()
    second = post(client).json()
    assert first == second
    assert len(llm.calls) == 1


def test_needs_review_then_clarification(make_client):
    ambiguous = result(
        note(type="task", title="Позвонить", body="Позвонить", confidence=0.3),
        needs_review=True,
        questions=["Кому?", "По какому делу?", "Лишний третий вопрос"],
    )
    llm = StubLLM(ambiguous)
    client = make_client(llm)

    first = post(client, content="позвонить тому парню").json()
    assert first["needs_review"] is True and first["needs_review_flag"] == "yes"
    assert first["review_questions"] == ["Кому?", "По какому делу?"]
    assert first["review_text"] == "Кому?\nПо какому делу?"
    assert first["message"] == "Нужно уточнение"

    second = post(client, content="позвонить тому парню", revision=2,
                  user_clarifications="Серику, про ремонт").json()
    assert second["needs_review"] is False and second["needs_review_flag"] == "no"
    assert second["review_questions"] == [] and second["review_text"] == ""
    assert llm.calls[1]["clarifications"] == "Серику, про ремонт"
    assert llm.calls[1]["revision"] == 2
    assert "Уточнение: Серику, про ремонт" in second["files"][0]["markdown"]


def test_needs_review_without_questions_gets_default(make_client):
    data = post(make_client(StubLLM(result(note(body="х"), needs_review=True)))).json()
    assert data["needs_review"] is True
    assert len(data["review_questions"]) == 1


def test_fallback_when_llm_unavailable_keeps_note(make_client):
    data = post(make_client(StubLLM(None)), content="Важная мысль\nвторая строка").json()
    assert data["fallback"] is True and data["needs_review"] is False
    assert "ИИ недоступен" in data["message"]
    md = data["files"][0]["markdown"]
    assert 'title: "Важная мысль"' in md and "  - unprocessed" in md
    assert "Важная мысль\nвторая строка" in md


def test_llm_output_cannot_break_filename_or_yaml(make_client):
    hostile = result(
        note(
            type="idea",
            title='../../etc/passwd: "хитрый" заголовок\nstatus: hacked',
            tags=["ok", "ev il", "---"],
            body="---\nstatus: hacked\n---\n\nЧистый текст",
        )
    )
    data = post(make_client(StubLLM(hostile))).json()
    file = data["files"][0]
    assert "/" not in file["filename"] and "\\" not in file["filename"] and ".." not in file["filename"]
    assert file["filename"].endswith(".md")
    lines = file["markdown"].split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)
    block = lines[1:end]
    assert len([line for line in block if line.startswith("title:")]) == 1
    assert not [line for line in block if line.startswith("status: hacked")]
    assert "hacked" not in "\n".join(lines[end + 1:])
    assert "Чистый текст" in "\n".join(lines[end + 1:])
    assert "  - ev-il" in block


def test_link_context_is_passed_to_llm_and_links_fetched(make_client, task_result):
    links = StubLinks([LinkInfo(url="https://example.com/post", title="Статья про память", domain="example.com")])
    llm = StubLLM(task_result)
    post(make_client(llm, links), content="глянуть https://example.com/post")
    assert links.calls == [["https://example.com/post"]]
    assert "Статья про память" in llm.calls[0]["link_context"]


def test_no_links_means_no_fetch(make_client, task_result):
    links = StubLinks()
    post(make_client(StubLLM(task_result), links))
    assert links.calls == []


def test_single_url_fallback_is_bookmark_without_review(make_client):
    data = post(make_client(StubLLM(None)), input_type="url", content="https://example.com/a").json()
    md = data["files"][0]["markdown"]
    assert data["needs_review"] is False
    assert 'title: "Ссылка: example.com"' in md and 'type: "bookmark"' in md


def test_url_type_with_plain_text_is_treated_as_text(make_client, task_result):
    data = post(make_client(StubLLM(task_result)), input_type="url", content="посмотри потом https://x.com").json()
    assert "Оригинал" in data["markdown"]


# ---------- режим «без ИИ» ----------

@pytest.mark.parametrize("text", ["! пароль от сейфа 4321", "секрет: пароль от сейфа 4321"])
def test_private_mode_skips_llm_links_and_cache(make_client, text, tmp_path):
    llm, links = StubLLM(), StubLinks()
    client = make_client(llm, links)
    data = post(client, content=text + " https://example.com").json()
    assert data["private"] is True and data["fallback"] is False and data["needs_review"] is False
    assert llm.calls == [] and links.calls == []
    md = data["files"][0]["markdown"]
    assert "пароль от сейфа 4321" in md and "private: true" in md
    assert "! пароль" not in md and "секрет:" not in md
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


def test_private_mode_needs_text(make_client):
    assert post(make_client(StubLLM()), content="!").status_code == 422


# ---------- валидация ----------

@pytest.mark.parametrize("kind", ["audio", "image", "pdf", "video"])
def test_unsupported_types_are_rejected_clearly(make_client, kind):
    response = post(make_client(StubLLM()), input_type=kind)
    assert response.status_code == 422
    assert "text и url" in response.json()["detail"]


@pytest.mark.parametrize(
    "fields",
    [{"content": ""}, {"content": "   "}, {"capture_id": "bad id!"}, {"capture_id": "short"}, {"revision": 3}],
)
def test_validation_errors(make_client, fields):
    assert post(make_client(StubLLM()), **fields).status_code == 422


def test_invalid_json_returns_422(make_client):
    assert make_client(StubLLM()).post("/capture", content=b"{oops", headers=auth()).status_code == 422


def test_too_long_content(make_client):
    assert post(make_client(StubLLM(), max_content_chars=100), content="а" * 101).status_code == 413


def test_missing_capture_id_is_generated(make_client, task_result):
    response = make_client(StubLLM(task_result)).post("/capture", json={"content": "без id"}, headers=auth())
    assert response.status_code == 200
    assert response.json()["capture_id"].startswith("srv-")


def test_rate_limit(make_client, task_result):
    client = make_client(StubLLM(task_result), rate_limit_per_minute=3)
    codes = [post(client, capture_id=f"id-number-{i:04d}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_many_notes_in_one_capture(make_client):
    many = result(*[note(title=f"Заметка {i}", body=f"тело {i}") for i in range(3)])
    data = post(make_client(StubLLM(many))).json()
    assert data["count"] == 3
    names = [f["filename"] for f in data["files"]]
    assert len(set(names)) == 3


# ---------- хранение: тексты на сервере не лежат ----------

def test_database_holds_no_note_text_except_short_lived_response(make_client, task_result, tmp_path):
    post(make_client(StubLLM(task_result)))
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if not row[0].startswith("sqlite_")}
    assert tables == {"responses", "outbox", "tg_pending", "tg_state", "profile", "known_people", "snapshot"}
    # очередь Telegram и ожидание уточнений при обычном /capture не используются
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tg_pending").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM known_people").fetchone()[0] == 0


def test_expired_responses_are_purged(tmp_path):
    db = Database(tmp_path / "x.db", ttl_hours=24)
    db.init()
    db.save(capture_id="old-capture-1", revision=1, response={"a": 1})
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("UPDATE responses SET created_at = ?", (time.time() - 25 * 3600,))
    conn.commit()
    conn.close()
    db.save(capture_id="new-capture-1", revision=1, response={"b": 2})
    assert db.get("old-capture-1", 1) is None
    assert db.get("new-capture-1", 1) == {"b": 2}


def test_legacy_captures_table_is_dropped(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE captures (capture_id TEXT, revision INT, content TEXT)")
    conn.execute("INSERT INTO captures VALUES ('x', 1, 'секретный текст')")
    conn.commit()
    conn.close()
    Database(path, ttl_hours=24).init()
    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "captures" not in tables
    assert b"\xd1\x81\xd0\xb5\xd0\xba\xd1\x80\xd0\xb5\xd1\x82" not in path.read_bytes()


def test_settings_require_strong_token(monkeypatch):
    monkeypatch.setenv("CAPTURE_TOKEN", "short")
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("CAPTURE_TOKEN", "x" * 24)
    assert load_settings().capture_token == "x" * 24


# ---------- вопросы и сводка ----------

from .conftest import StubAsk, vault_note  # noqa: E402

CORPUS = "\n=====NOTE=====\n".join([
    vault_note("Дюна 2", type="media", kind='"фильм"', status='"хочу"', created="2026-10-11T20:00"),
    vault_note("Пароль от сейфа 4321", private=True),
    vault_note("Позвонить Серику", type="task", status='"открыто"', due="2026-10-10"),
])


def ask_post(client, path="/ask", **fields):
    body = {"question": "какие фильмы планировал?", "corpus": CORPUS}
    body.update(fields)
    return client.post(path, json=body, headers=auth())


def test_ask_returns_answer_sources_and_skips_private(make_client):
    stub = StubAsk({"answer": "• Дюна 2 — хочу посмотреть", "used": [1]})
    data = ask_post(make_client(StubLLM(), ask_llm=stub)).json()
    assert data["status"] == "success" and data["answer"].startswith("• Дюна 2")
    assert data["sources"] == ["Дюна 2"] and data["sources_text"] == "• Дюна 2"
    assert data["private_skipped"] == 1 and data["notes_total"] == 3 and data["fallback"] is False
    sent = "\n".join(stub.calls[0]["lines"])
    assert "4321" not in sent and "Пароль" not in sent        # приватное до ИИ не доходит


def test_ask_fallback_without_llm(make_client):
    data = ask_post(make_client(StubLLM(), ask_llm=StubAsk(None))).json()
    assert data["fallback"] is True and "Дюна 2" in data["answer"]


def test_ask_validation_and_auth(make_client):
    client = make_client(StubLLM(), ask_llm=StubAsk(None))
    assert client.post("/ask", json={"question": "x", "corpus": CORPUS}).status_code == 401
    assert ask_post(client, question="  ").status_code == 422
    assert ask_post(client, question="а" * 1001).status_code == 413
    assert client.post("/ask", content=b"{oops", headers=auth()).status_code == 422
    big = make_client(StubLLM(), ask_llm=StubAsk(None), max_corpus_chars=100)
    assert ask_post(big, corpus="ы" * 101).status_code == 413


def test_digest_endpoint(make_client):
    data = ask_post(make_client(StubLLM()), path="/digest").json()
    assert data["status"] == "success"
    assert "Просрочено:" in data["digest"] and "Позвонить Серику" in data["digest"]
    assert "4321" not in data["digest"]


def test_ask_and_digest_store_nothing(make_client, tmp_path):
    client = make_client(StubLLM(), ask_llm=StubAsk({"answer": "ок", "used": []}))
    ask_post(client)
    ask_post(client, path="/digest")
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0
    assert b"Duna" not in (tmp_path / "data" / "brain.db").read_bytes()


def test_links_never_trigger_questions_even_if_model_asks(make_client):
    asking = result(note(type="bookmark", title="Ролик", body="x"), needs_review=True, questions=["Что с этим сделать?"])
    data = post(make_client(StubLLM(asking)), content="глянуть потом https://youtu.be/7BfIXbGp8q4 пригодится").json()
    assert data["needs_review"] is False and data["needs_review_flag"] == "no" and data["review_questions"] == []
