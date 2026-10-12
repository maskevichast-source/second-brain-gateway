import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.llm import LLMNote, LLMResult  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "t" * 32
FIXED_NOW = datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("Asia/Almaty"))  # понедельник


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        capture_token=TOKEN,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-test",
        deepseek_base_url="https://llm.invalid",
        data_dir=tmp_path / "data",
        timezone="Asia/Almaty",
        llm_timeout=5.0,
        rate_limit_per_minute=1000,
        max_content_chars=2000,
    )
    base.update(overrides)
    return Settings(**base)


def note(**kwargs) -> LLMNote:
    kwargs.setdefault("title", "Заметка")
    return LLMNote(**kwargs)


def result(*notes, needs_review=False, questions=()) -> LLMResult:
    return LLMResult(notes=list(notes), needs_review=needs_review, review_questions=list(questions))


class StubLLM:
    """Подменяет вызов DeepSeek и запоминает, с чем его вызывали."""

    def __init__(self, value=None):
        self.value = value
        self.calls = []

    async def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        return self.value


class StubLinks:
    def __init__(self, infos=None):
        self.infos = infos or []
        self.calls = []

    async def __call__(self, urls, settings):
        self.calls.append(list(urls))
        return self.infos


@pytest.fixture
def movie_result():
    return result(
        note(
            type="media",
            kind="фильм",
            status="хочу",
            area="отдых",
            title="Дюна: часть вторая",
            summary="Фильм, посоветовал Серик",
            tags=["Кино", "#фантастика"],
            people=["Серик"],
            fields={"year": 2024, "creator": "Дени Вильнёв", "recommended_by": "Серик"},
            ai_filled=["year", "creator"],
            body="Фильм посоветовал Серик. Хочу посмотреть.",
            confidence=0.92,
        )
    )


@pytest.fixture
def make_client(tmp_path):
    def _make(llm=None, links=None, ask_llm=None, **overrides):
        settings = make_settings(tmp_path, **overrides)
        app = create_app(
            settings=settings,
            llm=llm or StubLLM(None),
            clock=lambda: FIXED_NOW,
            link_fetcher=links or StubLinks(),
            ask_llm=ask_llm,
        )
        return TestClient(app)

    return _make


def auth():
    return {"x-api-key": TOKEN}


class StubAsk:
    """Подменяет вызов DeepSeek для вопросов."""

    def __init__(self, value=None):
        self.value = value
        self.calls = []

    async def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        return self.value


def vault_note(title, *, type="note", body="", private=False, **front) -> str:
    """Собирает текст заметки, как её хранит Obsidian."""
    lines = ["---", "schema: 1", f'title: "{title}"', f'type: "{type}"']
    for key, value in front.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines += [f'  - "{item}"' for item in value]
        else:
            lines.append(f"{key}: {value}")
    if private:
        lines.append("private: true")
    lines += ["---", "", body or title, ""]
    return "\n".join(lines)
