import re

import pytest

from app.config import load_settings
from app.llm import LLMNote

from .conftest import StubLLM, auth

CID = "20261012-093000-482913"


def post(client, **fields):
    body = {"capture_id": CID, "input_type": "text", "content": "купить кабель HDMI до пятницы"}
    body.update(fields)
    return client.post("/capture", json=body, headers=auth())


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
    response = make_client(StubLLM()).post("/capture", content=b"not json")
    assert response.status_code == 401


def test_happy_path_text(make_client, good_note):
    llm = StubLLM(good_note)
    data = post(make_client(llm)).json()

    assert data["status"] == "success"
    assert data["needs_review"] is False and data["needs_review_flag"] == "no"
    assert data["folder"] == "0-Inbox"
    assert data["fallback"] is False
    assert re.fullmatch(r"2026-10-12-купить-кабель-hdmi-[0-9a-f]{6}\.md", data["filename"])

    md = data["markdown"]
    assert md.startswith('---\ntitle: "Купить кабель HDMI"\n')
    assert 'created: "2026-10-12 09:30"' in md
    assert 'type: "task"' in md
    assert "tags:\n  - дом\n  - покупки\n" in md
    assert "confidence: 0.93" in md
    assert f'capture_id: "{CID}"' in md
    assert "- [ ] Купить кабель HDMI до пятницы" in md
    # исходный текст всегда сохраняется
    assert "> [!quote]- Оригинал\n> купить кабель HDMI до пятницы" in md
    assert llm.calls[0]["content"] == "купить кабель HDMI до пятницы"


def test_extra_fields_from_future_clients_are_ignored(make_client, good_note):
    response = post(make_client(StubLLM(good_note)), file_base64="", file_name="", timestamp="2026-10-12T04:30:00Z")
    assert response.status_code == 200


def test_idempotent_by_capture_id(make_client, good_note):
    llm = StubLLM(good_note)
    client = make_client(llm)
    first = post(client).json()
    second = post(client).json()
    assert first == second
    assert len(llm.calls) == 1


def test_needs_review_then_clarification(make_client):
    ambiguous = LLMNote(
        title="Позвонить",
        note_type="task",
        tags=["звонок"],
        confidence=0.3,
        needs_review=True,
        review_questions=["Кому?", "По какому делу?", "Лишний третий вопрос"],
        content="Позвонить",
    )
    llm = StubLLM(ambiguous)
    client = make_client(llm)

    first = post(client, content="позвонить тому парню").json()
    assert first["needs_review"] is True and first["needs_review_flag"] == "yes"
    assert first["review_questions"] == ["Кому?", "По какому делу?"]
    assert first["review_text"] == "Кому?\nПо какому делу?"
    assert first["message"] == "Нужно уточнение"

    second = post(client, content="позвонить тому парню", revision=2, user_clarifications="Серику, про ремонт").json()
    assert second["needs_review"] is False and second["needs_review_flag"] == "no"
    assert second["review_questions"] == []
    assert llm.calls[1]["clarifications"] == "Серику, про ремонт"
    assert llm.calls[1]["revision"] == 2
    assert "Уточнение: Серику, про ремонт" in second["markdown"]


def test_needs_review_without_questions_gets_default(make_client):
    note = LLMNote(title="Х", content="х", needs_review=True, review_questions=[])
    data = post(make_client(StubLLM(note))).json()
    assert data["needs_review"] is True
    assert len(data["review_questions"]) == 1


def test_fallback_when_llm_unavailable_keeps_note(make_client):
    data = post(make_client(StubLLM(None)), content="Важная мысль\nвторая строка").json()
    assert data["fallback"] is True
    assert data["needs_review"] is False
    assert data["message"] == "ИИ недоступен — заметка сохранена как есть"
    assert data["yaml_frontmatter"]["title"] == "Важная мысль"
    assert data["yaml_frontmatter"]["confidence"] == 0.0
    assert "  - unprocessed" in data["markdown"]
    assert "Важная мысль\nвторая строка" in data["markdown"]


def test_llm_output_cannot_break_filename_or_yaml(make_client):
    hostile = LLMNote(
        title='../../etc/passwd: "хитрый" заголовок\nstatus: hacked',
        note_type="concept",
        tags=["ok", "ev il", "---"],
        content="---\nstatus: hacked\n---\n\nЧистый текст",
    )
    data = post(make_client(StubLLM(hostile))).json()

    assert "/" not in data["filename"] and "\\" not in data["filename"] and ".." not in data["filename"]
    assert data["filename"].endswith(".md")
    lines = data["markdown"].split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)  # закрывающая черта frontmatter — ровно одна
    front = lines[1:end]
    # служебные ключи не продублированы, заголовок остался одной строкой
    assert [line for line in front if line.startswith("status:")] == ['status: "inbox"']
    assert len([line for line in front if line.startswith("title:")]) == 1
    # чужой блок --- ... --- из тела вырезан
    assert "hacked" not in "\n".join(lines[end + 1 :])
    assert data["markdown_body"].startswith("Чистый текст")
    assert data["yaml_frontmatter"]["tags"] == ["ok", "ev-il"]


def test_single_url_is_saved_as_link_without_review(make_client):
    note = LLMNote(
        title="Статья про Obsidian",
        note_type="resource",
        tags=["obsidian"],
        needs_review=True,  # для ссылок игнорируется
        content="Содержимое страницы не загружалось.",
    )
    data = post(make_client(StubLLM(note)), content="https://example.com/post?id=1").json()
    assert data["needs_review"] is False
    assert "**Ссылка:** https://example.com/post?id=1" in data["markdown"]
    assert "Оригинал" not in data["markdown"]


def test_link_fallback_title_uses_host(make_client):
    data = post(make_client(StubLLM(None)), input_type="url", content="https://example.com/a").json()
    assert data["yaml_frontmatter"]["title"] == "Ссылка: example.com"
    assert data["yaml_frontmatter"]["type"] == "resource"


def test_url_type_with_plain_text_is_treated_as_text(make_client, good_note):
    data = post(make_client(StubLLM(good_note)), input_type="url", content="посмотри потом https://x.com").json()
    assert "Оригинал" in data["markdown"]


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
    response = make_client(StubLLM()).post("/capture", content=b"{oops", headers=auth())
    assert response.status_code == 422


def test_too_long_content(make_client):
    response = post(make_client(StubLLM(), max_content_chars=100), content="а" * 101)
    assert response.status_code == 413


def test_missing_capture_id_is_generated(make_client, good_note):
    client = make_client(StubLLM(good_note))
    response = client.post("/capture", json={"content": "без id"}, headers=auth())
    assert response.status_code == 200
    assert response.json()["capture_id"].startswith("srv-")


def test_rate_limit(make_client, good_note):
    client = make_client(StubLLM(good_note), rate_limit_per_minute=3)
    codes = [post(client, capture_id=f"id-number-{i:04d}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_original_is_stored_in_database(make_client, good_note, tmp_path):
    import sqlite3

    post(make_client(StubLLM(good_note)))
    conn = sqlite3.connect(tmp_path / "data" / "brain.db")
    row = conn.execute("SELECT content, revision FROM captures WHERE capture_id = ?", (CID,)).fetchone()
    assert row == ("купить кабель HDMI до пятницы", 1)


def test_settings_require_strong_token(monkeypatch):
    monkeypatch.setenv("CAPTURE_TOKEN", "short")
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("CAPTURE_TOKEN", "x" * 24)
    assert load_settings().capture_token == "x" * 24
