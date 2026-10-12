import asyncio
import json

import httpx

from app.links import (
    LinkInfo,
    describe_for_llm,
    fetch_link_info,
    is_public_ip,
    is_safe_url,
    parse_page,
)

from .conftest import make_settings

HOSTS = {
    "example.com": ["93.184.216.34"],
    "www.example.com": ["93.184.216.34"],
    "internal.example": ["10.0.0.5"],
    "mixed.example": ["93.184.216.34", "127.0.0.1"],
}


async def resolver(host):
    if host == "broken.example":
        raise OSError("нет DNS")
    return HOSTS.get(host, ["93.184.216.34"])


def run(coro):
    return asyncio.run(coro)


HTML = (
    "<html><head><title>Обычный заголовок</title>"
    '<meta property="og:title" content="OG заголовок">'
    '<meta property="og:description" content="Описание страницы">'
    '<meta property="og:site_name" content="Пример">'
    "</head><body>Много текста</body></html>"
)


def fetch(tmp_path, handler, urls, **settings):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_link_info(urls, make_settings(tmp_path, **settings), client=client, resolver=resolver)

    return run(go())


def html_response(markup=HTML, ctype="text/html; charset=utf-8", status=200, headers=None):
    return httpx.Response(status, content=markup.encode("utf-8"), headers={"content-type": ctype, **(headers or {})})


def test_public_ip_checks():
    assert is_public_ip("93.184.216.34")
    for bad in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "172.16.5.5", "169.254.169.254", "0.0.0.0", "::1",
                "fe80::1", "::ffff:127.0.0.1", "224.0.0.1", "не-адрес"):
        assert not is_public_ip(bad), bad


def test_is_safe_url_rules():
    assert run(is_safe_url("https://example.com/a", resolver))
    assert not run(is_safe_url("http://example.com/a", resolver))
    assert not run(is_safe_url("https://example.com:8443/a", resolver))
    assert not run(is_safe_url("https://user:pass@example.com/", resolver))
    assert not run(is_safe_url("https://internal.example/", resolver))
    assert not run(is_safe_url("https://mixed.example/", resolver))
    assert not run(is_safe_url("https://broken.example/", resolver))
    assert not run(is_safe_url("https:///nohost", resolver))
    assert not run(is_safe_url("ftp://example.com/", resolver))


def test_parse_page_prefers_og_tags():
    assert parse_page(HTML) == ("OG заголовок", "Описание страницы", "Пример")
    title, description, site = parse_page("<title>Только\n  title</title><meta name='description' content='Мета'>")
    assert (title, description, site) == ("Только title", "Мета", "")
    assert parse_page("<<< мусор >>>") == ("", "", "")


def test_page_title_and_description_are_read(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return html_response()

    (info,) = fetch(tmp_path, handler, ["https://example.com/article"])
    assert info.title == "OG заголовок"
    assert info.description == "Описание страницы"
    assert info.site == "Пример"
    assert info.domain == "example.com"
    assert "SecondBrainBot" in seen[0]


def test_youtube_uses_oembed(tmp_path):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"title": "Лекция про память", "author_name": "Канал Х"})

    (info,) = fetch(tmp_path, handler, ["https://youtu.be/7BfIXbGp8q4"])
    assert info.is_youtube and info.title == "Лекция про память" and info.channel == "Канал Х"
    assert urls[0].startswith("https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2F7BfIXbGp8q4")


def test_youtube_failure_still_marks_video(tmp_path):
    (info,) = fetch(tmp_path, lambda r: httpx.Response(404), ["https://youtu.be/xxxxxxxxxxx"])
    assert info.is_youtube and info.title == ""


def test_redirect_to_private_address_is_refused(tmp_path):
    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://internal.example/secret"})
        raise AssertionError("к внутреннему адресу обращаться нельзя")

    (info,) = fetch(tmp_path, handler, ["https://example.com/a"])
    assert info.title == "" and info.domain == "example.com"


def test_redirects_are_followed_within_limit(tmp_path):
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return html_response()

    (info,) = fetch(tmp_path, handler, ["https://example.com/a"])
    assert info.title == "OG заголовок"


def test_redirect_loop_gives_up(tmp_path):
    (info,) = fetch(
        tmp_path, lambda r: httpx.Response(302, headers={"location": "/again"}), ["https://example.com/x"]
    )
    assert info.title == ""


def test_non_html_and_errors_give_empty_info(tmp_path):
    (pdf,) = fetch(tmp_path, lambda r: html_response("%PDF", ctype="application/pdf"), ["https://example.com/f.pdf"])
    (err,) = fetch(tmp_path, lambda r: httpx.Response(500), ["https://example.com/x"])
    assert pdf.title == "" and err.title == ""


def test_http_links_are_not_fetched(tmp_path):
    def handler(request):
        raise AssertionError("http не читаем")

    (info,) = fetch(tmp_path, handler, ["http://example.com/a"])
    assert info.title == "" and info.domain == "example.com"


def test_page_size_is_capped(tmp_path):
    big = "<title>Начало</title>" + "x" * 5_000_000
    (info,) = fetch(tmp_path, lambda r: html_response(big), ["https://example.com/big"], link_max_bytes=2000)
    assert info.title == "Начало"


def test_network_error_does_not_raise(tmp_path):
    def handler(request):
        raise httpx.ConnectError("нет сети")

    (info,) = fetch(tmp_path, handler, ["https://example.com/a"])
    assert info.title == ""


def test_dns_failure_does_not_raise(tmp_path):
    (info,) = fetch(tmp_path, lambda r: html_response(), ["https://broken.example/a"])
    assert info.title == ""


def test_no_urls_means_no_work(tmp_path):
    assert fetch(tmp_path, lambda r: None, []) == []


def test_describe_for_llm():
    text = describe_for_llm([
        LinkInfo(url="https://youtu.be/x", title="Ролик", channel="Канал", site="YouTube", is_youtube=True),
        LinkInfo(url="https://example.com/a"),
    ])
    assert "название: Ролик" in text and "канал: Канал" in text
    assert "страницу прочитать не удалось" in text
    assert json.dumps(text)  # строка без управляющих сюрпризов
