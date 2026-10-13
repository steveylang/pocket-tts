import pytest

from reader.extract import ExtractionError, extract_article
from reader.tests.samples import ARTICLE_HTML, JAVASCRIPT_ONLY_HTML


def test_article_text_and_title_are_extracted():
    article = extract_article(ARTICLE_HTML)
    assert article.title == "Notes on Growing Tomatoes"
    assert "Tomatoes need at least six hours" in article.text
    assert "frost is forecast" in article.text


def test_menus_and_footers_are_left_out():
    text = extract_article(ARTICLE_HTML).text
    assert "Log in" not in text
    assert "newsletter" not in text


def test_every_paragraph_is_its_own_block_ending_in_punctuation():
    paragraphs = extract_article(ARTICLE_HTML).text.split("\n\n")
    assert len(paragraphs) == 4  # the heading plus three paragraphs
    assert paragraphs[0] == "Notes on Growing Tomatoes."
    assert all(paragraph[-1] in ".!?" for paragraph in paragraphs)


@pytest.mark.parametrize(
    "page", [JAVASCRIPT_ONLY_HTML, b"", b"<html><body><p>Too short.</p></body></html>"]
)
def test_pages_without_an_article_are_a_clear_error(page: bytes):
    with pytest.raises(ExtractionError, match="article"):
        extract_article(page)
