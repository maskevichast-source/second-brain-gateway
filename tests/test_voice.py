import asyncio

import httpx

from app import stt
from .conftest import StubLLM, auth, make_settings, note, result
from .test_telegram import send, tg, update  # noqa: F401


def voice_update(uid, duration=10):
    base = update("", uid=uid)
    base["message"].pop("text")
    base["message"]["voice"] = {"file_id": "FILE1", "duration": duration}
    return base


def test_stt_pipeline(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.oga"}})
        if "/file/bot" in request.url.path:
            return httpx.Response(200, content=b"OGGDATA")
        assert request.headers["authorization"] == "Bearer key"
        return httpx.Response(200, json={"text": " Привет, это тест. "})

    settings = make_settings(tmp_path, stt_api_key="key", telegram_bot_token="tok")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await stt.transcribe(settings, "FILE1", c)

    assert asyncio.run(run()) == "Привет, это тест."
    assert len(calls) == 3 and calls[-1].endswith("/audio/transcriptions")


def test_stt_failure_and_missing_config(tmp_path):
    settings = make_settings(tmp_path, stt_api_key="key", telegram_bot_token="tok")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
            return await stt.transcribe(settings, "FILE1", c)

    assert asyncio.run(run()) is None
    assert asyncio.run(stt.transcribe(make_settings(tmp_path), "FILE1")) is None


def test_voice_without_stt_explains(tg):
    out = send(tg(StubLLM(None)), voice_update(1)).json()["text"]
    assert "STT_API_KEY" in out


def test_voice_is_saved_like_text_and_echoed(tg):
    async def fake(file_id):
        assert file_id == "FILE1"
        return "посмотреть Дюну на выходных"

    llm = StubLLM(result(note(type="media", title="Дюна", body="x")))
    client = tg(llm, transcriber=fake)
    out = send(client, voice_update(1)).json()["text"]
    assert out.startswith("🎙 «посмотреть Дюну на выходных»") and "Дюна" in out
    assert llm.calls[0]["content"] == "посмотреть Дюну на выходных"


def test_voice_goes_into_diary_dialog(tg):
    async def fake(file_id):
        return "день был хороший, закрыл вопрос с УК"

    llm = StubLLM(result(note(type="journal", title="День", body="x")))
    client = tg(llm, transcriber=fake)
    send(client, update("/diary", uid=1))
    out = send(client, voice_update(2)).json()["text"]
    assert out.startswith("🎙") and "[2/5]" in out
    send(client, update("/done", uid=3))
    assert "закрыл вопрос с УК" in llm.calls[0]["content"]


def test_voice_failure_and_too_long(tg):
    async def none(file_id):
        return None

    client = tg(StubLLM(None), transcriber=none)
    assert "Не смог расшифровать" in send(client, voice_update(1)).json()["text"]
    assert "длиннее 5 минут" in send(client, voice_update(2, duration=400)).json()["text"]
