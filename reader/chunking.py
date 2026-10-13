"""Split text into chunks of whole sentences, each short enough to generate in a few seconds.

The model splits each chunk again internally, so this only has to keep chunks small and avoid
cutting sentences in half.
"""

import re

# About 25 seconds of speech. Tune after listening on the phone.
MAX_CHARS = 400
# Generation takes about 0.6 s per second of speech here, so the first chunk is kept short:
# playback starts after a few seconds, and the worker then stays ahead of the listener.
FIRST_CHUNK_CHARS = 150

# A sentence ends at . ! ? or … (plus closing quotes/brackets) followed by whitespace or the end
# of the text, so decimals such as 3.14 stay whole. The last sentence may lack punctuation.
_SENTENCE = re.compile(r".+?(?:[.!?…]+[\"'”’)\]]*(?=\s|$)|$)", re.DOTALL)
_CLAUSE_BREAK = re.compile(r"(?<=[,;:])\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def split_sentences(paragraph: str) -> list[str]:
    sentences = [match.group().strip() for match in _SENTENCE.finditer(paragraph)]
    return [sentence for sentence in sentences if sentence]


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """Break an over-long sentence at commas/semicolons/colons, then at spaces, then mid-word."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    for part in _CLAUSE_BREAK.split(sentence):
        line = ""
        for word in part.split():
            while len(word) > max_chars:
                if line:
                    pieces.append(line)
                    line = ""
                pieces.append(word[:max_chars])
                word = word[max_chars:]
            if line and len(line) + 1 + len(word) > max_chars:
                pieces.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            pieces.append(line)
    return pieces


def _units(text: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = " ".join(paragraph.split())
        for sentence in split_sentences(paragraph):
            units.extend(_split_long(sentence, max_chars))
    return units


def _pack(units: list[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = f"{current} {unit}".strip()
    if current:
        chunks.append(current)
    return chunks


def chunk_text(
    text: str, max_chars: int = MAX_CHARS, first_max_chars: int | None = FIRST_CHUNK_CHARS
) -> list[str]:
    """Pack sentences into chunks of at most max_chars; the first is at most first_max_chars."""
    chunks = _pack(_units(text, max_chars), max_chars)
    if first_max_chars is not None and chunks and len(chunks[0]) > first_max_chars:
        head = _pack(_units(chunks[0], first_max_chars), first_max_chars)
        # Only the very first chunk must be short; the rest of it stays one normal chunk.
        chunks = [head[0], *([" ".join(head[1:])] if len(head) > 1 else []), *chunks[1:]]
    return chunks
