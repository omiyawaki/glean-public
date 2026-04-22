"""Lightweight shim to import paper_intake's resolver module.

paper_intake isn't pip-installable; it's a script collection at
~/tools/paper_intake. We add that directory to sys.path on first import
and re-export the two functions glean depends on. Tests monkey-patch
`normalize_identifier` / `crossref_metadata` on this module to avoid real
network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

_PI_DIR = str(Path("~/tools/paper_intake").expanduser())


def _ensure_on_path() -> None:
    if _PI_DIR not in sys.path:
        sys.path.insert(0, _PI_DIR)


def normalize_identifier(s: str) -> Optional[str]:
    _ensure_on_path()
    from resolver import normalize_identifier as _fn
    return _fn(s)


def crossref_metadata(doi: str) -> Optional[dict]:
    _ensure_on_path()
    from resolver import crossref_metadata as _fn
    return _fn(doi)
