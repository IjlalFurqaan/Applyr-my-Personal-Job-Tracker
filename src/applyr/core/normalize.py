"""Text normalization shared by entity resolution and repo lookups."""

from __future__ import annotations

import re

LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "gmbh",
        "ltd",
        "limited",
        "llc",
        "ag",
        "corp",
        "corporation",
        "co",
        "plc",
        "sa",
        "bv",
        "oy",
        "ab",
    }
)

# Filler tokens ignored when narrowing "the Stripe backend role" to a job title.
STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "at",
        "for",
        "of",
        "in",
        "to",
        "my",
        "one",
        "role",
        "job",
        "position",
        "posting",
        "application",
        "app",
        "opening",
        "that",
        "this",
        "with",
        "from",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> list[str]:
    return [t for t in _NON_ALNUM.split(text.casefold()) if t]


def normalize_company(text: str) -> str:
    toks = tokens(text)
    while len(toks) > 1 and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS]
