"""Настройки сервиса. Всё берётся из переменных окружения (на Railway — вкладка Variables)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 24


@dataclass(frozen=True)
class Settings:
    capture_token: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    data_dir: Path
    timezone: str
    llm_timeout: float
    rate_limit_per_minute: int
    max_content_chars: int
    response_ttl_hours: float = 24.0
    max_notes: int = 10
    link_timeout: float = 5.0
    link_max_bytes: int = 200_000
    max_corpus_chars: int = 3_000_000
    ask_budget_chars: int = 80_000
    max_question_chars: int = 1000


def load_settings() -> Settings:
    token = os.environ.get("CAPTURE_TOKEN", "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"CAPTURE_TOKEN не задан или короче {MIN_TOKEN_LENGTH} символов. "
            "Сгенерируйте длинный случайный токен (см. README) и добавьте его в переменные окружения."
        )

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "DEEPSEEK_API_KEY не задан: сервис будет сохранять заметки как есть, без обработки ИИ."
        )

    return Settings(
        capture_token=token,
        deepseek_api_key=api_key,
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        timezone=os.environ.get("APP_TIMEZONE", "Asia/Almaty").strip(),
        llm_timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
        rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30")),
        max_content_chars=int(os.environ.get("MAX_CONTENT_CHARS", "20000")),
        response_ttl_hours=float(os.environ.get("RESPONSE_TTL_HOURS", "24")),
        max_notes=int(os.environ.get("MAX_NOTES_PER_CAPTURE", "10")),
        link_timeout=float(os.environ.get("LINK_TIMEOUT_SECONDS", "5")),
        link_max_bytes=int(os.environ.get("LINK_MAX_BYTES", "200000")),
        max_corpus_chars=int(os.environ.get("MAX_CORPUS_CHARS", "3000000")),
        ask_budget_chars=int(os.environ.get("ASK_BUDGET_CHARS", "80000")),
    )
