import base64
import io

from app import files
from .conftest import StubLLM, auth, note, result


def upload(client, data=b"\x89PNG-bytes", filename="Снимок экрана.png", mime="image/png", **extra):
    body = {"file_base64": base64.b64encode(data).decode(), "filename": filename, "mime": mime, "capture_id": "up-0001-aaaa", **extra}
    return client.post("/capture_file", json=body, headers=auth())


class Vision:
    def __init__(self, text="Чек: 1 200 ₸"):
        self.text, self.calls = text, []

    async def __call__(self, data, mime):
        self.calls.append((len(data), mime))
        return self.text


class Audio:
    def __init__(self, text="Купить хлеб завтра"):
        self.text, self.calls = text, []

    async def __call__(self, data, name, mime):
        self.calls.append((name, mime))
        return self.text


def test_screenshot_is_recognized_noted_and_original_queued(make_client):
    llm = StubLLM(result(note(type="note", title="Чек", body="Чек: 1 200 ₸")))
    vision = Vision()
    client = make_client(llm, vision=vision)
    reply = upload(client)
    assert reply.status_code == 200
    body = reply.json()
    assert body["recognized"] is True and "распознан" in body["file_notice"]
    assert "Текст с картинки" in llm.calls[0]["content"] and "Чек: 1 200" in llm.calls[0]["content"]
    src = body["sources"][0]
    assert src["folder"].startswith("7-Исходники/") and src["filename"].endswith(".png")
    assert f"![[{src['filename']}]]" in body["files"][0]["markdown"] and "source_files:" in body["files"][0]["markdown"]
    items = client.get("/pending?service=1", headers=auth()).json()["items"]
    assert [i["kind"] for i in items] == ["file"]
    data = client.get(f"/file/{items[0]['id']}", headers=auth())
    assert data.status_code == 200 and data.content == b"\x89PNG-bytes" and data.headers["content-type"] == "image/png"
    # после подтверждения файл стирается и на сервере не остаётся
    assert client.post("/ack", json={"ids": [items[0]["id"]]}, headers=auth()).status_code == 200
    assert client.get(f"/file/{items[0]['id']}", headers=auth()).status_code == 404


def test_without_vision_original_is_still_saved_and_user_is_told_why(make_client):
    llm = StubLLM(result(note(type="note", title="Скриншот", body="Скриншот")))
    client = make_client(llm)
    body = upload(client).json()
    assert body["recognized"] is False and "VISION_MODEL" in body["file_notice"]
    assert body["sources"] and "Вложение с iPhone" in llm.calls[0]["content"]
    assert len(client.get("/pending?service=1", headers=auth()).json()["items"]) == 1


def test_voice_is_transcribed_and_kept(make_client):
    llm = StubLLM(result(note(type="task", title="Купить хлеб", body="Купить хлеб")))
    audio = Audio()
    client = make_client(llm, audio_transcriber=audio, stt_api_key="k")
    body = upload(client, filename="Новая запись.m4a", mime="audio/m4a").json()
    assert body["recognized"] is True and audio.calls[0][0] == "Новая запись.m4a"
    assert "Расшифровка голоса" in llm.calls[0]["content"] and body["sources"][0]["filename"].endswith(".m4a")


def test_private_caption_sends_nothing_outside_but_keeps_original(make_client):
    vision = Vision()
    llm = StubLLM(result(note(type="note", title="x")))
    client = make_client(llm, vision=vision)
    body = upload(client, content="! паспорт").json()
    assert not vision.calls and not llm.calls and body["private"] is True
    assert body["sources"] and len(client.get("/pending?service=1", headers=auth()).json()["items"]) == 1


def test_questions_are_skipped_so_file_is_not_lost(make_client):
    llm = StubLLM(result(note(type="note", title="Что это"), needs_review=True, questions=["Что это?"]))
    client = make_client(llm, vision=Vision())
    body = upload(client).json()
    assert body["files"] and body["sources"]


def test_phone_can_pull_originals_with_files_flag(make_client):
    client = make_client(StubLLM(result(note(type="note", title="Чек"))), vision=Vision())
    upload(client)
    assert client.get("/pending", headers=auth()).json()["count"] == 0          # старый шорткат файлов не получает
    got = client.get("/pending?files=1", headers=auth()).json()
    assert got["count"] == 1 and got["items"][0]["kind"] == "file"


def test_bad_requests_are_rejected(make_client):
    client = make_client(StubLLM(None))
    assert client.post("/capture_file", json={}, headers={}).status_code == 401
    assert client.post("/capture_file", json={"file_base64": ""}, headers=auth()).status_code == 422
    assert upload(client, capture_id="x").status_code == 422
    assert client.post("/capture_file", data="не json", headers=auth()).status_code == 422


def test_big_image_is_shrunk_for_vision():
    from PIL import Image
    image = Image.effect_noise((2600, 2600), 90).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    data = buf.getvalue()
    assert len(data) > files.MAX_IMAGE_BYTES
    small, mime = files.shrink_image(data)
    assert mime == "image/jpeg" and len(small) <= files.MAX_IMAGE_BYTES
    assert files.shrink_image(b"abc") == (b"abc", "")
