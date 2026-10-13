"""Turn pasted text or a web page into clean text that is pleasant to hear."""

import re


class ExtractionError(Exception):
    """Something the user can fix or should know about; the message is shown on the page."""


class UnsafeURL(ExtractionError):
    """The link points somewhere the server must not fetch."""


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_LINE_MARKER = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]+|[-*+•][ \t]+|>[ \t]?)", re.MULTILINE)
_EMPHASIS = re.compile(r"[*`~]")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_END_PUNCTUATION = ".!?…:;\"'”’)"


def clean_text(text: str) -> str:
    """Strip markup and links, join wrapped lines, and make every paragraph end in punctuation.

    Paragraphs are separated by a blank line in the result. Returns "" if nothing is readable.
    """
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _LINE_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)

    paragraphs: list[str] = []
    for block in _PARAGRAPH_BREAK.split(text):
        block = " ".join(block.split())
        if not any(character.isalnum() for character in block):
            continue
        if block[-1] not in _END_PUNCTUATION:
            block += "."
        paragraphs.append(block)
    return "\n\n".join(paragraphs)
