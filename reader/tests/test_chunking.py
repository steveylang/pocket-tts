from reader.chunking import FIRST_CHUNK_CHARS, MAX_CHARS, chunk_text, split_sentences


def words(chunks: list[str]) -> list[str]:
    return " ".join(chunks).split()


def test_empty_text_gives_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("  \n\n  ") == []


def test_short_text_is_one_chunk():
    assert chunk_text("Hello there. How are you?") == ["Hello there. How are you?"]


def test_sentences_are_packed_up_to_the_limit_and_never_split():
    sentence = "x" * 99 + "."  # 100 characters
    text = " ".join([sentence] * 5)
    chunks = chunk_text(text, max_chars=250, first_max_chars=None)
    assert chunks == [f"{sentence} {sentence}", f"{sentence} {sentence}", sentence]
    assert all(len(chunk) <= 250 for chunk in chunks)


def test_decimal_points_and_quotes_do_not_end_a_sentence():
    assert split_sentences("Pi is 3.14 today. Yes!") == ["Pi is 3.14 today.", "Yes!"]
    assert split_sentences('He said "stop." Then left') == ['He said "stop."', "Then left"]


def test_paragraph_breaks_are_collapsed_into_the_packing():
    assert chunk_text("One.\n\nTwo.") == ["One. Two."]


def test_huge_unpunctuated_text_still_respects_the_limit():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
    assert words(chunks) == text.split()


def test_a_single_enormous_word_is_sliced():
    text = "a" * 1000
    chunks = chunk_text(text, max_chars=400, first_max_chars=None)
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert "".join(chunks) == text


def test_a_long_sentence_is_split_at_commas_first():
    clause = "this clause has a fair number of words in it"
    sentence = ", ".join([clause] * 12) + "."
    chunks = chunk_text(sentence, max_chars=200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert all(chunk.startswith("this clause") for chunk in chunks)


def test_the_first_chunk_is_short_so_playback_starts_quickly():
    text = " ".join(f"This is sentence number {i} of the test article." for i in range(60))
    chunks = chunk_text(text)
    assert len(chunks[0]) <= FIRST_CHUNK_CHARS
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
    assert words(chunks) == text.split()
    assert len(chunks[1]) > FIRST_CHUNK_CHARS  # everything after the first chunk is full size


def test_a_long_first_sentence_is_still_cut_for_a_quick_start():
    sentence = ", ".join(["some words here"] * 30) + "."
    chunks = chunk_text(sentence)
    assert len(chunks[0]) <= FIRST_CHUNK_CHARS
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
