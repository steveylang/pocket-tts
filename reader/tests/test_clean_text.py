from reader.extract import clean_text


def test_markdown_markers_and_urls_are_removed():
    text = "# Title\n\n- first point\n- second point\n\nSee [the docs](https://example.com/x) now."
    assert clean_text(text) == "Title.\n\nfirst point second point.\n\nSee the docs now."


def test_bare_urls_are_dropped():
    assert clean_text("Go to https://example.com/a?b=1 for more.") == "Go to for more."


def test_existing_end_punctuation_is_kept_and_missing_is_added():
    assert clean_text("Really?\n\nA heading") == "Really?\n\nA heading."


def test_hard_wrapped_lines_are_joined_into_one_paragraph():
    assert clean_text("one two\nthree four.") == "one two three four."


def test_emphasis_and_code_marks_are_removed():
    assert clean_text("This is **bold** and `code`.") == "This is bold and code."


def test_symbol_or_emoji_only_paragraphs_are_dropped():
    assert clean_text("Hello.\n\n🙂🙂 --- ***\n\nBye.") == "Hello.\n\nBye."


def test_nothing_readable_gives_empty_string():
    assert clean_text("") == ""
    assert clean_text("   \n\n 🙂 ") == ""
    assert clean_text("https://example.com") == ""
