from app.textutil import (
    clean_tags,
    file_suffix,
    find_urls,
    is_valid_capture_id,
    parse_date,
    parse_datetime,
    parse_month_day,
    quote_block,
    single_url,
    slugify,
    split_private,
    strip_leading_yaml,
    trim_words,
    wikilink,
    wikilinks,
)


def test_slugify_keeps_cyrillic_and_drops_specials():
    assert slugify('Архитектура "Second Brain" / v2: план?') == "архитектура-second-brain-v2-план"


def test_slugify_never_empty_and_limited():
    assert slugify("???") == "zametka"
    assert len(slugify("а" * 200)) <= 50


def test_clean_tags():
    assert clean_tags(["#Дом", "дом", "Machine Learning", "2026", "", "a/b", "bad*chars!"]) == [
        "дом",
        "machine-learning",
        "a/b",
        "badchars",
    ]


def test_clean_tags_limit():
    assert len(clean_tags([f"t{i}x" for i in range(20)])) == 8


def test_strip_leading_yaml():
    body = "---\ntitle: x\nevil: true\n---\n\nТекст заметки"
    assert strip_leading_yaml(body).strip() == "Текст заметки"
    assert strip_leading_yaml("Обычный текст\n---\nне трогать") == "Обычный текст\n---\nне трогать"


def test_single_url():
    assert single_url("  https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    assert single_url("посмотри https://example.com") is None
    assert single_url("https://a.com https://b.com") is None


def test_find_urls():
    text = "смотри https://a.com/x, и (https://b.com/y?q=1). Ещё https://a.com/x"
    assert find_urls(text) == ["https://a.com/x", "https://b.com/y?q=1"]
    assert find_urls("без ссылок") == []
    assert len(find_urls(" ".join(f"https://s{i}.com" for i in range(10)))) == 3


def test_capture_id_validation():
    assert is_valid_capture_id("20261012-093000-482913")
    assert not is_valid_capture_id("short")
    assert not is_valid_capture_id("has space in it")
    assert not is_valid_capture_id("../../etc/passwd")


def test_quote_block_keeps_empty_lines():
    assert quote_block("a\n\nb") == "> a\n>\n> b"


def test_file_suffix_stable():
    assert file_suffix("abc12345") == file_suffix("abc12345")
    assert file_suffix("abc12345") != file_suffix("abc12345-2")
    assert len(file_suffix("abc12345")) == 6


def test_split_private():
    assert split_private("! мой секрет") == (True, "мой секрет")
    assert split_private("!!тест") == (True, "тест")
    assert split_private("Секрет: пароль от двери") == (True, "пароль от двери")
    assert split_private("секрет, не говорить") == (True, "не говорить")
    assert split_private("обычная заметка!") == (False, "обычная заметка!")
    assert split_private("секретарь позвонил") == (False, "секретарь позвонил")


def test_trim_words():
    assert trim_words("коротко", 20) == "коротко"
    assert trim_words("один два три четыре пять", 12) == "один два…"
    assert trim_words("а" * 50, 10) == "а" * 10 + "…"


def test_wikilink():
    assert wikilink("Серик") == "[[Серик]]"
    assert wikilink("[[Серик Иванов]]") == "[[Серик Иванов]]"
    assert wikilink("  Серик   Иванов ") == "[[Серик Иванов]]"
    assert wikilink("Серик|псевдоним#раздел") == "[[Серикпсевдонимраздел]]"
    assert wikilink("") is None
    assert wikilink("[[]]") is None


def test_wikilinks_dedupe_and_type():
    assert wikilinks(["Серик", "[[Серик]]", "", "Аня"]) == ["[[Серик]]", "[[Аня]]"]
    assert wikilinks("Серик") == ["[[Серик]]"]
    assert wikilinks(None) == []


def test_parse_date():
    assert parse_date("2026-10-14") == "2026-10-14"
    assert parse_date("2026-10-14T18:00") == "2026-10-14"
    assert parse_date("2026-02-30") is None
    assert parse_date("завтра") is None


def test_parse_datetime():
    assert parse_datetime("2026-10-14T18:00") == "2026-10-14T18:00"
    assert parse_datetime("2026-10-14 18:05") == "2026-10-14T18:05"
    assert parse_datetime("2026-10-14") == "2026-10-14"
    assert parse_datetime("2026-10-14T25:00") is None
    assert parse_datetime("вечером") is None


def test_parse_month_day():
    assert parse_month_day("3-5") == "03-05"
    assert parse_month_day("03-05") == "03-05"
    assert parse_month_day("02-29") == "02-29"
    assert parse_month_day("13-01") is None
    assert parse_month_day("1990-03-05") == "1990-03-05"
    assert parse_month_day("5 марта") is None
