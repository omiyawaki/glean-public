"""Citekey validation. Generation will be added in step 5 (importer).

Format: <FirstAuthorFamily><Year><FirstContentWord>, CamelCased, ASCII-folded.
Collision suffixes: a, b, c. See DESIGN.md "Citekey rules".
"""

import re

CITEKEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid(s: str) -> bool:
    return bool(s) and bool(CITEKEY_RE.match(s))


def looks_like_doi(s: str) -> bool:
    return "/" in s
