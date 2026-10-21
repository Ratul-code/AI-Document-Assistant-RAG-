"""Tokenisation primitives shared by BM25 and any future lexical code.

Single source of truth for "what is a token in this codebase." Pure
Python, no deps. The English stopword list is a small hardcoded set —
intentionally not from NLTK so the retrieval layer stays dep-light.

`tokenize` is conservative on purpose: it keeps acronyms, digit
strings, and short alphanumeric tokens because user documents often
contain domain-specific acronyms and short identifiers ("DMV",
"SSA", "ID-123", "HCV"). The stopword list only drops common
English function words.
"""

from __future__ import annotations

import re
from functools import lru_cache

# English stopwords — short, conservative. Adding words here changes
# BM25 behaviour globally, so keep it tight.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a
    about
    above
    again
    against
    all
    am
    an
    and
    any
    are
    as
    at
    be
    because
    been
    before
    being
    below
    between
    both
    but
    by
    can
    could
    did
    do
    does
    doing
    down
    during
    each
    few
    for
    from
    further
    had
    has
    have
    having
    he
    her
    here
    hers
    herself
    him
    himself
    his
    how
    i
    if
    in
    into
    is
    it
    its
    itself
    just
    me
    more
    most
    my
    myself
    no
    nor
    not
    now
    of
    off
    on
    once
    only
    or
    other
    our
    ours
    ourselves
    out
    over
    own
    same
    she
    should
    so
    some
    such
    than
    that
    the
    their
    theirs
    them
    themselves
    then
    there
    these
    they
    this
    those
    through
    to
    too
    under
    until
    up
    very
    was
    we
    were
    what
    when
    where
    which
    while
    who
    whom
    why
    will
    with
    would
    you
    your
    yours
    yourself
    yourselves
    """.split()
)

# Word characters: letters + digits + hyphens (keep "sars-cov-2" as one
# token, but split on whitespace, slashes, dots, parens, etc.).
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]+")


def is_stopword(token: str) -> bool:
    """True iff `token` is in the stopword set. Lookup is O(1)."""
    return token in _STOPWORDS


def tokenize(text: str) -> list[str]:
    """Lowercase + word-split + stopword filter.

    Empty input → []. Same input + same stopword set → same tokens
    (the function is deterministic; no caching needed for that, but
    `tokenize_cached` is provided for the hot path).

    Token boundary: `_WORD_RE` matches 2+ chars of `[A-Za-z0-9-]`.
    Single-character tokens are dropped at the regex level — that's
    deliberate (they're almost always stopwords or punctuation that
    snuck through, and BM25 IDF is uninformative on them).
    """
    if not text:
        return []
    lowered = text.lower()
    out: list[str] = []
    for tok in _WORD_RE.findall(lowered):
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


@lru_cache(maxsize=4096)
def tokenize_cached(text: str) -> tuple[str, ...]:
    """Cached tokenisation — tuple so it's hashable + list-coercible.

    `functools.lru_cache` requires hashable args; `str` is fine.
    Same text → same tuple. Use this in the BM25 build path where we
    tokenise every chunk exactly once but want the per-query path
    to share work across repeated identical queries (eval harness).
    """
    return tuple(tokenize(text))


__all__ = ["is_stopword", "tokenize", "tokenize_cached"]
