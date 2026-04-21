"""Citekey validation and generation.

Format: <FirstAuthorFamily><Year><FirstContentWord>, CamelCased, ASCII-folded.
Collision suffixes: a, b, c. See DESIGN.md "Citekey rules".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


CITEKEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NONALPHA = re.compile(r"[^A-Za-z0-9]+")
_WS = re.compile(r"\s+")

STOPWORDS = frozenset({
    "a", "an", "the", "of", "on", "in", "at", "to", "for", "from",
    "and", "or", "but", "with", "by", "as", "is", "are", "be",
})


def is_valid(s: str) -> bool:
    return bool(s) and bool(CITEKEY_RE.match(s))


def looks_like_doi(s: str) -> bool:
    return "/" in s


def _ascii_fold(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _camel(word: str) -> str:
    folded = _ascii_fold(word)
    parts = []
    for chunk in _NONALPHA.split(folded):
        if chunk:
            parts.append(chunk[:1].upper() + chunk[1:].lower())
    return "".join(parts)


def first_content_word(title: str) -> str:
    folded = _ascii_fold(title)
    for raw in _WS.split(folded):
        clean = _NONALPHA.sub("", raw)
        if clean and clean.lower() not in STOPWORDS:
            return clean
    return ""


def generate(family: str, year: int, title: str) -> str:
    family_part = _camel(family)
    year_part = str(year)
    content_part = _camel(first_content_word(title))
    return f"{family_part}{year_part}{content_part}"


def disambiguate(base: str, existing: Iterable[str]) -> str:
    taken = set(existing)
    if base not in taken:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"too many citekey collisions for {base!r}")
