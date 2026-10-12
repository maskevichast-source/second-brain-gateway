import re

from app.links import LinkInfo
from app.service import build_private_response, build_response

from .conftest import FIXED_NOW, note, result

CID = "20261012-093000-482913"


def build(content="Серик посоветовал фильм Дюна 2", res=None, links=None, urls=None, **kw):
    kw.setdefault("revision", 1)
    return build_response(
        capture_id=CID,
        client="ios",
        content=content,
        clarifications=kw.pop("clarifications", ""),
        result=res,
        links=links or [],
        content_urls=urls or [],
        now=FIXED_NOW,
        **kw,
    )


def front(markdown: str) -> dict:
    """Грубый разбор блока свойств для проверок: ключ -> строки значения."""
    lines = markdown.split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)
    block, key = {}, None
    for line in lines[1:end]:
        if line.startswith("  - "):
            block[key].append(line[4:])
        else:
            key, _, value = line.partition(": ")
            key = key.rstrip(":")
            block[key] = value if value else []
    return block


# ---------- одна заметка ----------

def test_media_note_is_complete(movie_result):
    data = build(res=movie_result)
    assert data["count"] == 1 and data["fallback"] is False and data["private"] is False
    assert data["message"] == "Медиа: Дюна: часть вторая"
    file = data["files"][0]
    assert file["folder"] == "0-Inbox" and file["type"] == "media"
    assert re.fullmatch(r"2026-10-12-дюна-часть-вторая-[0-9a-f]{6}\.md", file["filename"])

    md = file["markdown"]
    props = front(md)
    # порядок: общие поля, особые поля типа, хвост
    keys = list(props)
    assert keys[:9] == ["schema", "title", "summary", "created", "type", "kind", "status", "stage", "area"]
    assert keys.index("year") < keys.index("tags") < keys.index("capture_id")
    assert props["created"] == "2026-10-12T09:30"
    assert props["type"] == '"media"' and props["kind"] == '"фильм"' and props["status"] == '"хочу"'
    assert props["stage"] == '"inbox"' and props["area"] == '"отдых"'
    assert props["year"] == "2024" and props["creator"] == '"Дени Вильнёв"'
    assert props["recommended_by"] == '"[[Серик]]"'
    assert props["people"] == ['"[[Серик]]"']
    assert props["tags"] == ["кино", "фантастика"]
    assert props["ai_filled"] == ['"year"', '"creator"']
    assert props["confidence"] == "0.92" and props["source"] == '"ios"'
    assert "Фильм посоветовал Серик." in md
    assert "> [!quote]- Оригинал\n> Серик посоветовал фильм Дюна 2" in md
    # верхний уровень повторяет первый файл
    assert (data["filename"], data["markdown"]) == (file["filename"], md)


def test_values_from_ai_are_validated():
    res = result(note(
        type="media", kind="что-то", status="смотрю", area="космос", title="X", priority="срочно",
        fields={"year": "1700", "rating": "11", "platform": "Netflix", "finished_at": "вчера",
                "recommended_by": ["Серику"], "unknown": "лишнее", "length": ["2", "часа"]},
    ))
    props = front(build(res=res)["files"][0]["markdown"])
    assert props["kind"] == '"другое"'            # неизвестный вид -> «другое»
    assert props["status"] == '"хочу"'            # неизвестный статус -> первый из списка
    assert props["area"] == '"разное"'            # неизвестная сфера -> «разное»
    assert "priority" not in props
    assert "year" not in props and "rating" not in props and "finished_at" not in props
    assert props["platform"] == '"Netflix"' and props["recommended_by"] == '"[[Серику]]"'
    assert props["length"] == '"2, часа"'
    assert "unknown" not in props


def test_unknown_type_becomes_note_and_statusless_types_have_no_status():
    props = front(build(res=result(note(type="космос", title="X", status="что-то")))["files"][0]["markdown"])
    assert props["type"] == '"note"' and "status" not in props and "kind" not in props
    props = front(build(res=result(note(type="journal", title="День", status="сделано")))["files"][0]["markdown"])
    assert "status" not in props


def test_dates_numbers_and_enums_are_typed():
    res = result(
        note(type="task", title="Позвонить", fields={"due": "2026-10-16T18:00", "remind_at": "2026-10-16", "repeat": "раз в неделю"}),
    )
    props = front(build(res=res)["files"][0]["markdown"])
    assert props["due"] == "2026-10-16T18:00"       # без кавычек, Dataview видит дату
    assert props["remind_at"] == "2026-10-16"
    assert props["status"] == '"открыто"'

    res = result(note(type="product", title="Кроссовки", kind="одежда", fields={"price": "60 000", "currency": "тенге"}))
    assert front(build(res=res)["files"][0]["markdown"])["price"] == "60000"

    res = result(note(type="place", title="Кафе", fields={"price_level": "Дорого", "city": "Астана"}))
    props = front(build(res=res)["files"][0]["markdown"])
    assert props["price_level"] == '"дорого"' and props["city"] == '"Астана"'


def test_person_card_fields():
    res = result(note(
        type="person", title="Серик", area="окружение",
        fields={"name": "Серик", "role": "юрист", "phones": ["+7 701 000 00 00 (личный)"],
                "birthday": "5-3", "relation_group": "знакомые", "gift_ideas": "книга, чай"},
    ))
    md = build(content="Серик — юрист", res=res)["files"][0]["markdown"]
    props = front(md)
    assert props["status"] == '"актуален"'
    assert props["phones"] == ['"+7 701 000 00 00 (личный)"']
    assert props["birthday"] == '"05-03"'
    assert props["gift_ideas"] == ['"книга"', '"чай"']
    assert props["relation_group"] == '"знакомые"'


def test_birthday_with_year_is_a_date():
    res = result(note(type="person", title="Аня", fields={"birthday": "1990-03-05"}))
    assert front(build(res=res)["files"][0]["markdown"])["birthday"] == "1990-03-05"


def test_ai_filled_only_keeps_allowed_present_fields():
    res = result(note(
        type="media", title="X", fields={"year": 2020, "why": "интересно"},
        ai_filled=["year", "why", "title", "creator", "platform"],
    ))
    assert front(build(res=res)["files"][0]["markdown"])["ai_filled"] == ['"year"']


def test_hostile_ai_output_cannot_break_file():
    res = result(note(
        title='../../etc/passwd: "хитрый" заголовок\nstatus: hacked',
        type="idea", tags=["ok", "ev il", "---"], people=["[[Серик]]\nstatus: hacked"],
        body="---\nstatus: hacked\n---\n\nЧистый текст",
    ))
    data = build(res=res)
    file = data["files"][0]
    assert "/" not in file["filename"] and ".." not in file["filename"] and "\\" not in file["filename"]
    lines = file["markdown"].split("\n")
    end = lines.index("---", 1)
    block = lines[1:end]
    assert [x for x in block if x.startswith("status:")] == ['status: "сырая"']
    assert len([x for x in block if x.startswith("title:")]) == 1
    assert "hacked" not in "\n".join(lines[end + 1:]).replace("Оригинал", "")
    assert "Чистый текст" in file["markdown"]


def test_source_url_must_come_from_the_record():
    res = result(note(type="media", title="Видео", source_url="https://invented.example/x"))
    assert "source_url" not in front(build(content="смотреть позже", res=res, urls=[])["files"][0]["markdown"])

    res = result(note(type="media", title="Видео", source_url="https://youtu.be/abc"))
    md = build(content="глянуть https://youtu.be/abc", res=res, urls=["https://youtu.be/abc"])["files"][0]["markdown"]
    assert front(md)["source_url"] == '"https://youtu.be/abc"'
    assert "**Ссылка:** https://youtu.be/abc" in md


def test_single_url_is_attached_to_single_note():
    res = result(note(type="media", kind="статья", title="Статья"))
    md = build(content="https://example.com/a", res=res, urls=["https://example.com/a"])["files"][0]["markdown"]
    assert front(md)["source_url"] == '"https://example.com/a"'


def test_youtube_info_fills_creator_and_platform():
    info = LinkInfo(url="https://youtu.be/abc", title="Ролик", channel="Канал", is_youtube=True)
    res = result(note(type="media", kind="видео", title="Ролик", source_url="https://youtu.be/abc"))
    props = front(build(content="https://youtu.be/abc", res=res, links=[info], urls=[info.url])["files"][0]["markdown"])
    assert props["creator"] == '"Канал"' and props["platform"] == '"YouTube"'
    assert "ai_filled" not in props


def test_bookmark_gets_domain_and_title_from_link_info():
    info = LinkInfo(url="https://example.com/a", domain="example.com", title="Заголовок страницы")
    res = result(note(type="bookmark", title="Закладка", source_url=info.url))
    props = front(build(content=info.url, res=res, links=[info], urls=[info.url])["files"][0]["markdown"])
    assert props["domain"] == '"example.com"' and props["page_title"] == '"Заголовок страницы"'
    assert props["status"] == '"не-разобрано"'


# ---------- несколько заметок ----------

def three_movies():
    return result(*[note(type="media", kind="фильм", title=t, status="хочу") for t in ("Дюна", "Оппенгеймер", "Интерстеллар")])


def test_several_notes_get_unique_files_and_ids():
    data = build(content="посмотреть Дюну, Оппенгеймера и Интерстеллар", res=three_movies())
    assert data["count"] == 3 and data["message"] == "Сохранено заметок: 3"
    names = [f["filename"] for f in data["files"]]
    assert len(set(names)) == 3
    ids = [front(f["markdown"])["capture_id"] for f in data["files"]]
    assert ids == [f'"{CID}-1"', f'"{CID}-2"', f'"{CID}-3"']
    assert all("Оригинал" in f["markdown"] for f in data["files"])  # короткий оригинал повторяется
    assert data["filename"] == names[0]


def test_long_original_is_kept_only_in_first_note():
    content = "посмотреть " + "очень длинный список " * 60
    data = build(content=content, res=three_movies())
    flags = ["Оригинал" in f["markdown"] for f in data["files"]]
    assert flags == [True, False, False]


def test_more_than_limit_adds_rest_note_with_full_text():
    many = result(*[note(type="media", title=f"Фильм {i}") for i in range(12)])
    data = build(content="список из двенадцати фильмов", res=many, max_notes=10)
    assert data["count"] == 11
    assert [f["title"] for f in data["files"][:2]] == ["Фильм 0", "Фильм 1"]
    rest = data["files"][-1]
    assert rest["title"] == "Остаток записи" and "список из двенадцати фильмов" in rest["markdown"]


# ---------- уточнения ----------

def test_needs_review_rules():
    ambiguous = result(note(type="task", title="Позвонить"), needs_review=True, questions=["Кому?", "О чём?", "Лишний"])
    first = build(content="позвонить тому парню", res=ambiguous)
    assert first["needs_review"] is True and first["needs_review_flag"] == "yes"
    assert first["review_questions"] == ["Кому?", "О чём?"] and first["review_text"] == "Кому?\nО чём?"
    assert first["message"] == "Нужно уточнение"

    second = build(content="позвонить тому парню", res=ambiguous, revision=2, clarifications="Серику, про бумаги")
    assert second["needs_review"] is False and second["needs_review_flag"] == "no"
    assert "Уточнение: Серику, про бумаги" in second["files"][0]["markdown"]

    link = build(content="https://example.com/a", res=ambiguous, urls=["https://example.com/a"])
    assert link["needs_review"] is False

    no_questions = build(res=result(note(title="Х"), needs_review=True))
    assert no_questions["needs_review"] is True and len(no_questions["review_questions"]) == 1


# ---------- запасные варианты ----------

def test_fallback_plain_note_keeps_text():
    data = build(content="Важная мысль\nвторая строка", res=None)
    assert data["fallback"] is True and data["needs_review"] is False
    assert data["message"] == "ИИ недоступен — заметка сохранена как есть"
    props = front(data["files"][0]["markdown"])
    assert props["title"] == '"Важная мысль"' and props["type"] == '"note"' and props["confidence"] == "0"
    assert props["tags"] == ["unprocessed"]
    assert "Важная мысль\nвторая строка" in data["files"][0]["markdown"]


def test_fallback_youtube_becomes_media_without_ai():
    info = LinkInfo(url="https://youtu.be/abc", title="Лекция", channel="Канал", is_youtube=True)
    data = build(content="https://youtu.be/abc", res=None, links=[info], urls=[info.url])
    props = front(data["files"][0]["markdown"])
    assert props["type"] == '"media"' and props["kind"] == '"видео"' and props["status"] == '"хочу"'
    assert props["title"] == '"Лекция"' and props["creator"] == '"Канал"' and props["platform"] == '"YouTube"'
    assert props["source_url"] == '"https://youtu.be/abc"'


def test_fallback_link_becomes_bookmark():
    info = LinkInfo(url="https://example.com/a", domain="example.com", title="Статья", description="Описание")
    data = build(content="https://example.com/a", res=None, links=[info], urls=[info.url])
    md = data["files"][0]["markdown"]
    props = front(md)
    assert props["type"] == '"bookmark"' and props["title"] == '"Статья"' and props["page_title"] == '"Статья"'
    assert "Описание" in md

    bare = build(content="https://example.com/a", res=None, urls=["https://example.com/a"])
    assert front(bare["files"][0]["markdown"])["title"] == '"Ссылка: example.com"'


# ---------- приватный режим ----------

def test_private_response_has_no_ai_fields_and_marks_private():
    data = build_private_response(capture_id=CID, client="ios", text="код от сейфа 12 34\nвторая строка", now=FIXED_NOW)
    assert data["private"] is True and data["message"] == "Сохранено без ИИ" and data["count"] == 1
    props = front(data["files"][0]["markdown"])
    assert props["private"] == "true" and props["type"] == '"note"' and props["confidence"] == "1"
    assert props["title"] == '"код от сейфа 12 34"'
    assert "tags" not in props
    assert "код от сейфа 12 34\nвторая строка" in data["files"][0]["markdown"]
    assert "Оригинал" not in data["files"][0]["markdown"]
