"""Связь с финансовым ботом «Ада»: короткая сводка бюджета для секретаря.

Шлюз только читает агрегаты (суммы, категории, долги, подписки, цели) по адресу ADA_SUMMARY_URL
и ничего не хранит дольше нескольких минут в памяти. Если «Ада» недоступна или не настроена,
секретарь просто работает без финансов.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, str]] = {}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: Any, currency: str = "₸") -> str:
    number = _num(value)
    if number is None:
        return "?"
    return f"{round(number):,}".replace(",", " ") + f" {currency}"


def _s(value: Any, limit: int = 60) -> str:
    return " ".join(str(value or "").split())[:limit]


def format_summary(data: dict) -> str:
    """Превращает ответ /api/secretary в короткий русский текст. Устойчив к пропускам полей."""
    if not isinstance(data, dict):
        return ""
    cur = _s(data.get("currency"), 4) or "₸"
    lines: list[str] = []
    head = f"Бюджет за {_s(data.get('month_label')) or 'текущий месяц'}"
    if data.get("day") and data.get("days_in_month"):
        head += f" (день {data['day']} из {data['days_in_month']})"
    lines.append(head + ":")
    parts = []
    for key, name in (("income", "доходы"), ("expense", "расходы"), ("balance", "баланс")):
        if _num(data.get(key)) is not None:
            parts.append(f"{name} {money(data[key], cur)}")
    if parts:
        lines.append("• " + ", ".join(parts))
    delta = _num(data.get("expense_delta_pct"))
    if delta is not None and _num(data.get("prev_expense")) is not None:
        word = "больше" if delta > 0 else "меньше"
        lines.append(f"• расходы на {abs(round(delta))}% {word}, чем в прошлом месяце ({money(data['prev_expense'], cur)})")
    cats = [c for c in (data.get("top_categories") or []) if isinstance(c, dict)][:6]
    if cats:
        rows = []
        for c in cats:
            row = f"{_s(c.get('name'))} {money(c.get('spent'), cur)}"
            if _num(c.get("limit")):
                row += f" из {money(c['limit'], cur)}"
                if _s(c.get("status")) and _s(c.get("status")) not in ("ok", "ок", "в норме"):
                    row += f" ({_s(c['status'])})"
            rows.append(row)
        lines.append("• категории: " + "; ".join(rows))
    debts = data.get("debts") if isinstance(data.get("debts"), dict) else {}
    if debts:
        lines.append(f"• долги: мне должны {money(debts.get('lent_total'), cur)}, я должен {money(debts.get('borrowed_total'), cur)}")
        for d in [d for d in (debts.get("soon") or []) if isinstance(d, dict)][:4]:
            lines.append(f"  – {_s(d.get('who'))}: {money(d.get('amount'), cur)}"
                         + (f", срок {_s(d.get('due'), 10)}" if d.get("due") else ""))
    subs = data.get("subscriptions") if isinstance(data.get("subscriptions"), dict) else {}
    if subs:
        lines.append(f"• подписки: {money(subs.get('month_total'), cur)} в месяц")
        for s in [s for s in (subs.get("upcoming") or []) if isinstance(s, dict)][:4]:
            lines.append(f"  – {_s(s.get('name'))}: {money(s.get('amount'), cur)}"
                         + (f", {_s(s.get('date'), 10)}" if s.get("date") else ""))
    goals = [g for g in (data.get("goals") or []) if isinstance(g, dict)][:4]
    if goals:
        lines.append("• цели: " + "; ".join(
            f"{_s(g.get('name'))} {money(g.get('saved'), cur)}" + (f" из {money(g['target'], cur)}" if _num(g.get("target")) else "")
            for g in goals))
    return "\n".join(lines) if len(lines) > 1 else ""


def configured(settings: Settings) -> bool:
    return bool(settings.ada_summary_url and settings.ada_summary_token)


async def fetch_summary(settings: Settings, http_client: httpx.AsyncClient | None = None) -> str:
    """Текст сводки «Ады» или пустая строка. Результат кэшируется на 5 минут."""
    if not configured(settings):
        return ""
    key = settings.ada_summary_url
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.ada_timeout)
    try:
        response = await client.get(
            settings.ada_summary_url, headers={"X-Secretary-Token": settings.ada_summary_token},
            timeout=settings.ada_timeout,
        )
        response.raise_for_status()
        text = format_summary(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ADA: сводка недоступна (%s)", type(exc).__name__)
        return ""
    finally:
        if owns:
            await client.aclose()
    _cache[key] = (time.monotonic(), text)
    return text


def clear_cache() -> None:
    _cache.clear()
