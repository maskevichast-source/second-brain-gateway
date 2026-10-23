"""Важные новости про сервисы, на которых живёт Джарвис: сбои, взломы, смена ключей.

Читает публичные ленты статуса (RSS/Atom). Первый запуск ничего не присылает, кроме свежего (за сутки), чтобы не завалить
старым. Повторов нет: каждая запись запоминается. Тревоги про безопасность (утечка, взлом, замена ключей) помечены 🔴 и
доходят даже ночью. Если лента недоступна, молча пропускаем: тревогу про саму ленту не шлём.
"""
from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .db import Database
from .textutil import one_line, trim_words

logger = logging.getLogger(__name__)
SEEN_SETTING = "news_seen"
MAX_SEEN = 600
MAX_ITEM_BYTES = 400_000
SECURITY = re.compile(r"security|breach|unauthori[sz]ed|compromis|credential|leak|exposed|rotate|secrets?|api key|"
                      r"взлом|утечк|компромет|ключ|секрет|несанкцион", re.IGNORECASE)
# Не про работу сервиса: задержки поддержки, биллинг, документация. Такое не присылаем (если не про безопасность).
NOISE = re.compile(r"customer support|support (?:response|ticket|team)|billing|invoice|documentation|status page|"
                   r"subscription (?:page|email)|dependabot|copilot|codespaces|discussions|gists?\b|github pages|sponsors|"
                   r"marketplace|notifications?\b|degraded performance for (?:issues|pull requests) search", re.IGNORECASE)


def _clean(text: str, limit: int) -> str:
    text = html.unescape(html.unescape(re.sub(r"<[^>]+>", " ", text or "")))     # &quot; и &amp;quot;
    return one_line(text, limit)


def _fix_link(link: str) -> str:
    return re.sub(r"(?<!:)/{2,}", "/", link or "")


@dataclass(frozen=True)
class NewsItem:
    uid: str
    source: str
    title: str
    link: str
    published: datetime | None
    summary: str

    @property
    def critical(self) -> bool:
        return bool(SECURITY.search(f"{self.title} {self.summary}"))


def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str, source: str) -> list[NewsItem]:
    """RSS 2.0 и Atom. Мусорный XML даёт пустой список."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        if _strip(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = _strip(child.tag)
            if name == "link":
                fields.setdefault("link", child.get("href") or (child.text or "").strip())
            else:
                fields.setdefault(name, (child.text or "").strip())
        title = _clean(fields.get("title", ""), 160)
        if not title:
            continue
        summary = _clean(fields.get("description") or fields.get("summary") or fields.get("content") or "", 400)
        uid = fields.get("guid") or fields.get("id") or fields.get("link") or title
        if NOISE.search(f"{title} {summary}") and not SECURITY.search(f"{title} {summary}"):
            continue
        items.append(NewsItem(f"{source}|{uid}", source, title, _fix_link(fields.get("link", "")),
                              _date(fields.get("pubDate") or fields.get("published") or fields.get("updated")), summary))
    return items


def _load_seen(db: Database) -> list[str]:
    try:
        data = json.loads(db.get_setting(SEEN_SETTING, "") or "[]")
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


async def fetch_new(db: Database, feeds, now: datetime, client: httpx.AsyncClient | None = None,
                    fresh_hours: int = 24) -> list[NewsItem]:
    """Новые записи со всех лент. Первый запуск по ленте: только свежее за сутки, остальное молча запоминается."""
    seen = _load_seen(db)
    seen_set = set(seen)
    first_run = not seen
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10, follow_redirects=True)
    fresh: list[NewsItem] = []
    try:
        for name, url in feeds:
            try:
                response = await client.get(url, headers={"User-Agent": "second-brain-gateway"})
                response.raise_for_status()
                parsed = parse_feed(response.text[:MAX_ITEM_BYTES], name)
            except (httpx.HTTPError, ValueError) as exc:
                logger.info("NEWS: лента %s недоступна (%s)", name, type(exc).__name__)
                continue
            for item in parsed:
                if item.uid in seen_set:
                    continue
                seen_set.add(item.uid)
                seen.append(item.uid)
                recent = item.published is None or (now.astimezone(timezone.utc) - item.published) <= timedelta(hours=fresh_hours)
                if not first_run or recent:
                    fresh.append(item)
    finally:
        if owns:
            await client.aclose()
    db.set_setting(SEEN_SETTING, json.dumps(seen[-MAX_SEEN:], ensure_ascii=False))
    fresh.sort(key=lambda i: (not i.critical, i.published or datetime.min.replace(tzinfo=timezone.utc)))
    return fresh


def format_item(item: NewsItem) -> str:
    icon = "🔴 Безопасность" if item.critical else "ℹ️ Сбой сервиса"
    text = f"{icon}: {item.source}\n{item.title}"
    if item.summary and item.summary != item.title:
        text += f"\n{trim_words(item.summary, 220)}"
    if item.critical:
        text += "\n\nЕсли это про ключи или токены: замени их у провайдера и в Railway."
    if item.link:
        text += f"\n{item.link}"
    return text
