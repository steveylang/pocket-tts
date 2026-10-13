import pytest

from reader.documents import extract_document
from reader.extract import ExtractionError
from reader.tests.pdfs import blank_pdf, make_pdf
from reader.tests.samples import ARTICLE_HTML


def test_a_text_file_is_cleaned_and_titled():
    article = extract_document("notes.txt", b"First line\nwrapped here.\n\nSecond paragraph.")
    assert article.text == "First line wrapped here.\n\nSecond paragraph."
    assert article.title == "First line wrapped here."


def test_markdown_markup_is_stripped():
    article = extract_document("a.md", b"# Heading\n\nSome **bold** and [a link](http://x.io).")
    assert article.text == "Heading.\n\nSome bold and a link."


def test_markdown_front_matter_is_not_read_aloud():
    data = b"---\ntitle: Secret\ntags: [a, b]\n---\n\nThe real text starts here."
    article = extract_document("note.md", data)
    assert article.text == "The real text starts here."


def test_a_horizontal_rule_that_is_not_front_matter_is_left_alone():
    article = extract_document("note.md", b"Intro.\n\n---\n\nAfter the rule.")
    assert article.text == "Intro.\n\nAfter the rule."


def test_windows_line_endings_and_a_byte_order_mark():
    article = extract_document("a.txt", b"\xef\xbb\xbfOne.\r\n\r\nTwo.\r\n")
    assert article.text == "One.\n\nTwo."


def test_a_file_that_is_not_utf8_is_read_as_latin1():
    article = extract_document("a.txt", "Un café.".encode("latin-1"))
    assert article.text == "Un café."


def test_html_goes_through_the_article_extractor():
    article = extract_document("page.html", ARTICLE_HTML)
    assert article.title == "Notes on Growing Tomatoes"
    assert "Log in" not in article.text


def test_the_extension_is_matched_case_insensitively():
    assert extract_document("NOTES.TXT", b"Hello there.").text == "Hello there."


def test_pdf_pages_are_joined_and_titled():
    article = extract_document("a.pdf", make_pdf(["Hello first page.", "Second page here."]))
    assert article.text == "Hello first page. Second page here."
    assert article.title == "Hello first page. Second page here."


def test_a_word_hyphenated_across_a_line_break_is_rejoined():
    article = extract_document("a.pdf", make_pdf(["A long exam-\nple of wrapping\nacross lines."]))
    assert article.text == "A long example of wrapping across lines."


def test_a_real_hyphen_before_a_capital_is_kept():
    article = extract_document("a.pdf", make_pdf(["See the Smith-\nJones report."]))
    assert "Smith- Jones" in article.text


def test_a_pdf_without_text_says_it_may_be_a_scan():
    with pytest.raises(ExtractionError, match="no text"):
        extract_document("scan.pdf", blank_pdf())


def test_a_password_protected_pdf_is_reported():
    with pytest.raises(ExtractionError, match="password"):
        extract_document("locked.pdf", blank_pdf(password="secret"))


@pytest.mark.parametrize("data", [b"", b"this is not a pdf"])
def test_a_damaged_pdf_is_a_plain_error(data: bytes):
    with pytest.raises(ExtractionError, match="Could not read"):
        extract_document("bad.pdf", data)


@pytest.mark.parametrize("name", ["empty.txt", "empty.md", "empty.TXT"])
def test_an_empty_text_file_has_nothing_readable(name: str):
    with pytest.raises(ExtractionError, match="nothing readable"):
        extract_document(name, b"")


@pytest.mark.parametrize("name", ["song.mp3", "README", "archive.tar.gz", ""])
def test_other_file_types_are_refused_with_the_supported_list(name: str):
    with pytest.raises(ExtractionError, match=r"\.txt, \.md, \.html or \.pdf"):
        extract_document(name, b"data")
