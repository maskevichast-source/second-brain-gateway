import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.llm import LLMNote  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "t" * 32
FIXED_NOW = datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("Asia/Almaty"))


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


class StubLLM:
    """Подменяет вызов DeepSeek и запоминает, с чем его вызывали."""

    def __init__(self, note=None):
        self.note = note
        self.calls = []

    async def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        return self.note


@pytest.fixture
def good_note():
    return LLMNote(
        title="Купить кабель HDMI",
        note_type="task",
        tags=["Дом", "#покупки"],
        confidence=0.93,
        content="- [ ] Купить кабель HDMI до пятницы",
    )


@pytest.fixture
def make_client(tmp_path):
    def _make(llm=None, **overrides):
        settings = make_settings(tmp_path, **overrides)
        app = create_app(settings=settings, llm=llm, clock=lambda: FIXED_NOW)
        return TestClient(app)

    return _make


def auth():
    return {"x-api-key": TOKEN}
