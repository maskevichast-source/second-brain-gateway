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
        llm_timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "40")),
        rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30")),
        max_content_chars=int(os.environ.get("MAX_CONTENT_CHARS", "20000")),
    )
