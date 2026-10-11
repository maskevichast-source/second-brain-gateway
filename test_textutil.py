from app.textutil import (
    clean_tags,
    file_suffix,
    is_valid_capture_id,
    quote_block,
    single_url,
    slugify,
    strip_leading_yaml,
    yaml_quote,
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


def test_yaml_quote_escapes():
    assert yaml_quote('Он сказал "привет"\nи ушёл') == '"Он сказал \\"привет\\" и ушёл"'


def test_strip_leading_yaml():
    body = "---\ntitle: x\nevil: true\n---\n\nТекст заметки"
    assert strip_leading_yaml(body).strip() == "Текст заметки"
    assert strip_leading_yaml("Обычный текст\n---\nне трогать") == "Обычный текст\n---\nне трогать"


def test_single_url():
    assert single_url("  https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    assert single_url("посмотри https://example.com") is None
    assert single_url("https://a.com https://b.com") is None


def test_capture_id_validation():
    assert is_valid_capture_id("20261012-093000-482913")
    assert not is_valid_capture_id("short")
    assert not is_valid_capture_id("has space in it")
    assert not is_valid_capture_id("../../etc/passwd")


def test_quote_block_keeps_empty_lines():
    assert quote_block("a\n\nb") == "> a\n>\n> b"


def test_file_suffix_stable():
    assert file_suffix("abc12345") == file_suffix("abc12345")
    assert len(file_suffix("abc12345")) == 6
