"""Названия по ссылкам.

- YouTube: название ролика и канал берутся у YouTube (oEmbed), без ключа.
- Остальные страницы: читаются только заголовок, описание и название сайта
  (title, og:title, og:description, og:site_name), не больше LINK_MAX_BYTES, по https,
  только публичные адреса. Текст страницы целиком не сохраняется.
- Любой сбой даёт запись без названия, а не ошибку: заметка всё равно сохранится.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx

from .config import Settings
from .textutil import one_line

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; SecondBrainBot/1.0)"

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass
class LinkInfo:
    url: str
    domain: str = ""
    title: str = ""
    description: str = ""
    site: str = ""
    channel: str = ""
    is_youtube: bool = False


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)   # is_global отсекает и 100.64.0.0/10


async def is_safe_url(url: str, resolver: Resolver) -> bool:
    """https, порт 443, без логина в адресе, и все адреса хоста публичные."""
    try:
        parts = urlparse(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    try:
        addresses = await resolver(host)
    except OSError:
        return False
    return bool(addresses) and all(is_public_ip(a) for a in addresses)


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            data = {k.lower(): (v or "") for k, v in attrs}
            key = (data.get("property") or data.get("name") or "").lower()
            if key and data.get("content") and key not in self.meta:
                self.meta[key] = data["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def parse_page(markup: str) -> tuple[str, str, str]:
    """Возвращает (заголовок, описание, название сайта)."""
    parser = _HeadParser()
    try:
        parser.feed(markup)
    except Exception:  # битая разметка не должна ронять запись
        pass
    meta = parser.meta
    title = meta.get("og:title") or parser.title
    description = meta.get("og:description") or meta.get("description", "")
    return one_line(title, 200), one_line(description, 400), one_line(meta.get("og:site_name", ""), 80)


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


async def _fetch_youtube(url: str, client: httpx.AsyncClient, timeout: float) -> LinkInfo:
    info = LinkInfo(url=url, domain=_domain(url), site="YouTube", is_youtube=True)
    try:
        response = await client.get(
            f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json",
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code == 200:
            data = response.json()
            info.title = one_line(data.get("title", ""), 200)
            info.channel = one_line(data.get("author_name", ""), 100)
    except (httpx.HTTPError, ValueError):
        logger.info("oEmbed YouTube недоступен")
    return info


def _connected_to_public_ip(response) -> bool:
    """После соединения сверяем реальный IP сервера: DNS мог ответить публичным адресом при проверке и внутренним при подключении."""
    try:
        stream = response.extensions.get("network_stream")
        addr = stream.get_extra_info("server_addr") if stream is not None else None
    except Exception:  # noqa: BLE001 - нет данных о соединении (тестовый транспорт): проверка по DNS уже пройдена
        return True
    return True if not addr else is_public_ip(str(addr[0]))


async def _fetch_page(url: str, client: httpx.AsyncClient, settings: Settings, resolver: Resolver) -> LinkInfo:
    info = LinkInfo(url=url, domain=_domain(url))
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not await is_safe_url(current, resolver):
                return info
            async with client.stream(
                "GET", current, headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=settings.link_timeout, follow_redirects=False,
            ) as response:
                if not _connected_to_public_ip(response):
                    return info                       # защита от DNS-rebinding: адрес, к которому реально подключились
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return info
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200 or "html" not in response.headers.get("content-type", "").lower():
                    return info
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) >= settings.link_max_bytes:
                        break
                encoding = response.charset_encoding or "utf-8"
            try:
                markup = bytes(data[: settings.link_max_bytes]).decode(encoding, errors="replace")
            except LookupError:
                markup = bytes(data[: settings.link_max_bytes]).decode("utf-8", errors="replace")
            info.title, info.description, info.site = parse_page(markup)
            return info
    except (httpx.HTTPError, ValueError):
        logger.info("страница недоступна")
    return info


async def fetch_link_info(
    urls: list[str],
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> list[LinkInfo]:
    """Данные по каждой ссылке. Ошибки не пробрасываются."""
    if not urls:
        return []
    resolver = resolver or default_resolver
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.link_timeout)

    async def one(url: str) -> LinkInfo:
        host = (urlparse(url).hostname or "").lower()
        try:
            if host in YOUTUBE_HOSTS:
                return await _fetch_youtube(url, client, settings.link_timeout)
            return await _fetch_page(url, client, settings, resolver)
        except Exception:  # любая неожиданность: заметка важнее названия
            logger.warning("не удалось получить данные по ссылке")
            return LinkInfo(url=url, domain=_domain(url))

    try:
        return list(await asyncio.gather(*(one(u) for u in urls)))
    finally:
        if owns_client:
            await client.aclose()


def describe_for_llm(infos: list[LinkInfo]) -> str:
    """Короткий блок для ИИ: что известно о ссылках."""
    lines: list[str] = []
    for info in infos:
        parts = [info.url]
        if info.title:
            parts.append(f"название: {info.title}")
        if info.channel:
            parts.append(f"канал: {info.channel}")
        if info.site:
            parts.append(f"сайт: {info.site}")
        if info.description:
            parts.append(f"описание: {info.description}")
        if not (info.title or info.description):
            parts.append("страницу прочитать не удалось")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)
