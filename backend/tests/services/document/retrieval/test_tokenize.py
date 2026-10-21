"""Unit tests for the BM25 tokeniser.

Pure — no DB, no model. Verifies stopword filtering, acronym preservation,
hyphen handling, and caching determinism.
"""

from app.services.document.retrieval._tokenize import (
    is_stopword,
    tokenize,
    tokenize_cached,
)


class TestIsStopword:
    def test_common_stopwords(self):
        for w in ("the", "is", "and", "of", "to"):
            assert is_stopword(w)

    def test_content_words(self):
        for w in ("hcov", "sars-cov-2", "mrna", "interferon", "vaccine"):
            assert not is_stopword(w)


class TestTokenize:
    def test_empty(self):
        assert tokenize("") == []

    def test_basic_split(self):
        assert tokenize("What about HCV treatment?") == ["hcv", "treatment"]

    def test_lowercases(self):
        assert tokenize("COVID-19 AND SARS") == ["covid-19", "sars"]

    def test_acronyms_kept(self):
        # Acronyms and short alphanumeric tokens survive the stopword filter.
        out = tokenize("HCV mRNA SARS-CoV-2 IFN-beta")
        assert "hcv" in out
        assert "mrna" in out
        assert "sars-cov-2" in out

    def test_short_tokens_dropped(self):
        # Single-char tokens (including digits) are dropped by the regex.
        out = tokenize("patient 5 days")
        assert "5" not in out
        assert "days" in out

    def test_hyphenated_kept(self):
        # Hyphenated words stay as one token (medical text relies on this).
        out = tokenize("type-ii-interferon")
        assert out == ["type-ii-interferon"]

    def test_punctuation_splits(self):
        # Slashes / spaces / parens are not in [_WORD_RE] so they split.
        out = tokenize("IFN-alpha interferon")
        assert "ifn-alpha" in out
        assert "interferon" in out


class TestTokenizeCached:
    def test_same_input_same_output(self):
        a = tokenize_cached("HCV treatment trial")
        b = tokenize_cached("HCV treatment trial")
        assert a == b

    def test_returns_tuple(self):
        out = tokenize_cached("hello world")
        assert isinstance(out, tuple)

    def test_caching_is_deterministic(self):
        # Idempotent across calls — same string, same tokens.
        assert tokenize_cached("Hello") == tokenize_cached("hello")
