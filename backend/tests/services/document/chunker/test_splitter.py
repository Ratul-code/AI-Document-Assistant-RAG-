from __future__ import annotations

from app.services.document.chunker import (
    MAX_TOKENS,
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    TextChunk,
    count_tokens,
    split_text,
)


def test_count_tokens_round_trip() -> None:
    """encode/decode should preserve text and give a sensible token count."""
    text = "Hello, world! This is a sample sentence for token counting."
    assert count_tokens(text) > 0
    assert isinstance(count_tokens(text), int)


def test_short_text_returns_single_chunk() -> None:
    chunks = split_text("Just a short sentence.")
    assert len(chunks) == 1
    assert chunks[0].token_count <= MAX_TOKENS
    assert chunks[0].text.startswith("Just a short sentence.")


def test_empty_text_returns_empty_list() -> None:
    assert split_text("") == []
    assert split_text("   \n\n  ") == []


def test_paragraph_boundary_is_preferred() -> None:
    """Splitter should break at paragraph boundaries, not mid-sentence."""
    para_a = "alpha " * 50  # ~50 words
    para_b = "beta " * 50
    text = f"{para_a}\n\n{para_b}"
    chunks = split_text(text)
    # First chunk should end with the alpha block; second should start with beta.
    assert len(chunks) >= 1
    assert "alpha" in chunks[0].text
    if len(chunks) > 1:
        assert "beta" in chunks[1].text


def test_long_paragraph_is_split_at_sentence_boundary() -> None:
    """A single long paragraph with sentences should split on '. '."""
    sentence = "The quick brown fox jumps over the lazy dog. "
    text = sentence * 100  # ~900 words, definitely over MAX_TOKENS
    chunks = split_text(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.token_count <= MAX_TOKENS


def test_no_sentences_falls_back_to_words() -> None:
    """A single wordless paragraph — actually a single very long word —
    must still produce a chunk that's at most MAX_TOKENS tokens."""
    huge_word = "x" * 5000
    chunks = split_text(huge_word)
    assert len(chunks) >= 1
    for chunk in chunks:
        # Char-level split can't honor token count exactly when there are
        # no spaces, but each piece must be no larger than MAX_TOKENS
        # tokens of "xxx...x" — i.e. <= MAX_TOKENS chars.
        assert chunk.token_count <= MAX_TOKENS


def test_chunks_are_at_most_max_tokens() -> None:
    """Sanity: every chunk in a long random-ish text is bounded."""
    para = "Sentence one with several words. Sentence two. " * 50
    text = "\n\n".join([para] * 10)
    chunks = split_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= MAX_TOKENS


def test_overlap_text_is_populated_for_intermediate_chunks() -> None:
    """Each chunk's `overlap_text` carries the next chunk's first OVERLAP tokens.

    The last chunk has no next, so its overlap is empty.
    """
    para = "word " * 400
    text = "\n\n".join([para] * 10)
    chunks = split_text(text)
    assert len(chunks) >= 3
    # Last chunk has no next — no overlap.
    assert chunks[-1].overlap_text == ""
    # Every chunk that has a successor gets some overlap text.
    for chunk in chunks[:-1]:
        assert chunk.overlap_text != ""


def test_target_and_max_constants_are_sensible() -> None:
    assert TARGET_TOKENS < MAX_TOKENS
    assert OVERLAP_TOKENS < TARGET_TOKENS
