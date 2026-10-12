from app.frontmatter import Raw, dump, emit


def test_strings_are_quoted_and_single_line():
    assert emit("title", 'Он сказал "привет"\nи ушёл') == ['title: "Он сказал \\"привет\\" и ушёл"']


def test_numbers_bools_and_raw_values():
    assert emit("year", 2024) == ["year: 2024"]
    assert emit("price", 60000.0) == ["price: 60000"]
    assert emit("price", 12.5) == ["price: 12.5"]
    assert emit("private", True) == ["private: true"]
    assert emit("created", Raw("2026-10-12T09:30")) == ["created: 2026-10-12T09:30"]


def test_empty_values_are_skipped():
    for value in (None, "", [], ()):
        assert emit("x", value) == []
    assert emit("tags", ["", None]) == []


def test_lists_are_quoted_but_tags_are_plain():
    assert emit("people", ["[[Серик]]"]) == ["people:", '  - "[[Серик]]"']
    assert emit("tags", ["дом", "a/b", "в-аду"]) == ["tags:", "  - дом", "  - a/b", "  - в-аду"]


def test_ambiguous_tags_get_quotes():
    lines = emit("tags", ["yes", "null", "2026"])
    assert lines == ["tags:", '  - "yes"', '  - "null"', '  - "2026"']


def test_dump_wraps_in_dashes_and_keeps_order():
    text = dump([("a", "x"), ("skip", None), ("b", 1)])
    assert text == '---\na: "x"\nb: 1\n---'
