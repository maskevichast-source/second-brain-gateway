import asyncio
import base64
import json

import httpx

from app import files
from .conftest import StubLLM, auth, make_settings, note, result
from .test_telegram import send, tg, update  # noqa: F401


def make_pdf(text: str | None) -> bytes:
    """Минимальный PDF на одну страницу; text=None даёт страницу без текстового слоя (как скан)."""
    stream = b"" if text is None else f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def pdf_update(uid=1, caption="", name="dogovor.pdf", file_id="f1"):
    extra = {"document": {"file_id": file_id, "file_name": name, "mime_type": "application/pdf"}}
    if caption:
        extra["caption"] = caption
    return update("", uid=uid, **extra)


class Fetcher:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def __call__(self, file_id):
        self.calls.append(file_id)
        return self.data


def test_extract_pdf_text_layer_and_scan():
    assert "Contract valid until 2026-12-01" in files.extract_pdf_text(make_pdf("Contract valid until 2026-12-01"))
    assert files.extract_pdf_text(make_pdf(None)) == ""
    assert files.extract_pdf_text(b"not a pdf") == ""


def test_pdf_with_text_becomes_normal_entry_with_source(tg):
    llm = StubLLM(result(note(type="document", title="Договор", body="x")))
    fetcher = Fetcher(make_pdf("Contract valid until 2026-12-01"))
    client = tg(llm, file_fetcher=fetcher)
    reply = send(client, pdf_update(caption="Договор аренды")).json()["text"]
    content = llm.calls[0]["content"]
    assert content.startswith("Договор аренды") and "Источник: PDF: dogovor.pdf" in content
    assert "Contract valid until 2026-12-01" in content and fetcher.calls == ["f1"]
    assert "Прочитал PDF «dogovor.pdf»" in reply


def test_pdf_text_is_truncated_to_limits(tg, monkeypatch):
    monkeypatch.setattr(files, "extract_pdf_text", lambda data: "слово " * 5000)
    llm = StubLLM(result(note(type="note", title="Длинный")))
    client = tg(llm, file_fetcher=Fetcher(b"x"), max_content_chars=2000)
    reply = send(client, pdf_update()).json()["text"]
    assert len(llm.calls[0]["content"]) <= 2000 and "(текст PDF обрезан)" in llm.calls[0]["content"]
    assert "остальное обрезал" in reply


def test_private_pdf_never_reaches_ai(tg):
    llm = StubLLM(result(note(type="note", title="Нельзя")))
    client = tg(llm, file_fetcher=Fetcher(make_pdf("Secret text")))
    reply = send(client, pdf_update(caption="! паспорт")).json()["text"]
    assert llm.calls == [] and "без ИИ" in reply
    assert "Secret text" in client.get("/pending", headers=auth()).json()["items"][0]["markdown"]


def test_scanned_pdf_is_saved_with_honest_notice(tg):
    llm = StubLLM(result(note(type="document", title="Скан")))
    client = tg(llm, file_fetcher=Fetcher(make_pdf(None)))
    reply = send(client, pdf_update(caption="Скан договора")).json()["text"]
    assert "Текст из PDF не извлёкся" in reply
    assert "Вложение в Telegram" in llm.calls[0]["content"] or "вложение в Telegram: файл «dogovor.pdf»" in llm.calls[0]["content"]


def test_pdf_download_failure_and_no_fetcher(tg):
    client = tg(StubLLM(result(note(type="note", title="А"))), file_fetcher=Fetcher(None))
    assert "Не смог скачать PDF" in send(client, pdf_update(uid=1)).json()["text"]
    client2 = tg(StubLLM(result(note(type="note", title="А"))))
    assert "Читать PDF не могу" in send(client2, pdf_update(uid=2)).json()["text"]


def photo_update(uid=1, caption="Чек"):
    return update("", uid=uid, photo=[{"file_id": "small"}, {"file_id": "big"}], caption=caption)


def test_photo_without_vision_says_text_not_recognized(tg):
    llm = StubLLM(result(note(type="note", title="Чек")))
    reply = send(tg(llm), photo_update()).json()["text"]
    assert "Текст с картинки не распознан" in reply
    assert "(вложение в Telegram: фото)" in llm.calls[0]["content"]


def test_photo_with_vision_adds_text_and_falls_back(tg):
    calls = []

    async def vision(data, mime):
        calls.append((data, mime))
        return "Итого 1500"

    llm = StubLLM(result(note(type="note", title="Чек")))
    reply = send(tg(llm, file_fetcher=Fetcher(b"img"), vision=vision), photo_update()).json()["text"]
    assert calls == [(b"img", "image/jpeg")] and "Итого 1500" in llm.calls[0]["content"]
    assert "распознан ИИ" in reply and "не распознан" not in reply

    async def broken(data, mime):
        return None

    llm2 = StubLLM(result(note(type="note", title="Чек")))
    reply2 = send(tg(llm2, file_fetcher=Fetcher(b"img"), vision=broken), photo_update(uid=2)).json()["text"]
    assert "Текст с картинки не распознан" in reply2 and "Итого" not in llm2.calls[0]["content"]


def test_private_photo_is_not_sent_to_vision(tg):
    async def vision(data, mime):
        raise AssertionError("не должно вызываться")

    llm = StubLLM(result(note(type="note", title="Чек")))
    reply = send(tg(llm, file_fetcher=Fetcher(b"img"), vision=vision), photo_update(caption="! личное")).json()["text"]
    assert "никуда не отправляются" in reply


def test_describe_image_uses_openai_compatible_call_and_falls_back(tmp_path):
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Привет, мир"}}]})

    settings = make_settings(tmp_path, stt_api_key="k", vision_model="vm", stt_base_url="https://groq.invalid/v1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(files.describe_image(settings, b"\xff\xd8abc", "image/jpeg", client)) == "Привет, мир"
    assert seen["url"] == "https://groq.invalid/v1/chat/completions" and seen["body"]["model"] == "vm"
    part = seen["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert part == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8abc").decode()

    def no_text(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "НЕТ"}}]})

    assert asyncio.run(files.describe_image(settings, b"x", "image/png", httpx.AsyncClient(transport=httpx.MockTransport(no_text)))) is None

    def down(request):
        return httpx.Response(500)

    assert asyncio.run(files.describe_image(settings, b"x", "image/png", httpx.AsyncClient(transport=httpx.MockTransport(down)))) is None
    # отдельный провайдер для картинок (Gemini): свой ключ и адрес, голос остаётся на Groq
    seen2 = {}

    def gem(request):
        seen2["url"], seen2["auth"], seen2["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Чек: 1200"}}]})

    g = make_settings(tmp_path, stt_api_key="groq", vision_model="gemini-flash-latest", vision_api_key="gkey",
                      vision_base_url="https://generativelanguage.googleapis.com/v1beta/openai")
    assert files.vision_enabled(g)
    assert asyncio.run(files.describe_image(g, b"x", "image/png", httpx.AsyncClient(transport=httpx.MockTransport(gem)))) == "Чек: 1200"
    assert seen2["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert seen2["auth"] == "Bearer gkey" and seen2["body"]["reasoning_effort"] == "low" and seen2["body"]["max_tokens"] == 4000
    assert "reasoning_effort" not in seen["body"]                          # Groq этот параметр не получает
    oa = make_settings(tmp_path, vision_model="gpt-4o-mini", vision_api_key="sk-x", vision_base_url="https://api.openai.com/v1")
    assert asyncio.run(files.describe_image(oa, b"x", "image/png", httpx.AsyncClient(transport=httpx.MockTransport(gem)))) == "Чек: 1200"
    assert seen2["url"] == "https://api.openai.com/v1/chat/completions" and seen2["auth"] == "Bearer sk-x"
    assert seen2["body"]["max_completion_tokens"] == 4000 and "max_tokens" not in seen2["body"] and "temperature" not in seen2["body"]
    only_vision_key = make_settings(tmp_path, vision_model="m", vision_api_key="k")      # STT_API_KEY не нужен
    assert files.vision_enabled(only_vision_key)
    off = make_settings(tmp_path, stt_api_key="k")                            # VISION_MODEL не задан: выключено
    assert asyncio.run(files.describe_image(off, b"x")) is None and not files.vision_enabled(off)


def test_fetch_file_with_mock_telegram(tmp_path):
    def handler(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"result": {"file_path": "docs/a.pdf", "file_size": 3}})
        return httpx.Response(200, content=b"abc")

    settings = make_settings(tmp_path, telegram_bot_token="123:tok")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(files.fetch_file(settings, "id", client)) == b"abc"
    assert asyncio.run(files.fetch_file(make_settings(tmp_path), "id", client)) is None
