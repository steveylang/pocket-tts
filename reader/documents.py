"""Turn an uploaded file (text, Markdown, HTML or PDF) into an Article to read aloud."""

import io
import re
from pathlib import PurePath

from pypdf import PdfReader

from reader.extract import Article, ExtractionError, clean_text, extract_article, title_from

MAX_DOCUMENT_BYTES = 20_000_000

_NOTHING_READABLE = "There is nothing readable in that text."
# Obsidian and static-site notes start with a --- block of metadata that must not be read aloud.
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL)
# A word split across lines ("exam-\nple"). Only before a lower-case letter, so "Smith-\nJones"
# keeps its hyphen.
_HYPHEN_BREAK = re.compile(r"(\w)-\n([a-z])")


def extract_document(filename: str, data: bytes) -> Article:
    """Read the text out of an uploaded file. Raises ExtractionError with a message for the page."""
    suffix = PurePath(filename).suffix.lower()
    if suffix in (".txt", ".md"):
        return _from_text(data, strip_front_matter=suffix == ".md")
    if suffix in (".html", ".htm"):
        return extract_article(data)
    if suffix == ".pdf":
        return _from_pdf(data)
    raise ExtractionError("That file type can't be read. Use .txt, .md, .html or .pdf.")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _from_text(data: bytes, strip_front_matter: bool) -> Article:
    raw = _decode(data).replace("\r\n", "\n")
    if strip_front_matter:
        raw = _FRONT_MATTER.sub("", raw, count=1)
    text = clean_text(raw)
    if not text:
        raise ExtractionError(_NOTHING_READABLE)
    return Article(title=title_from(text), text=text)


def _from_pdf(data: bytes) -> Article:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("This PDF is password protected.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception as error:  # pypdf raises many different types on damaged files
        raise ExtractionError("Could not read that file.") from error
    # Pages are joined with a single newline, not a blank line: a sentence often continues on the
    # next page, and a blank line would make clean_text close it with a full stop.
    raw = _HYPHEN_BREAK.sub(r"\1\2", "\n".join(pages))
    text = clean_text(raw)
    if not text:
        raise ExtractionError("This PDF has no text, it may be scanned images. There is no OCR.")
    return Article(title=title_from(text), text=text)
