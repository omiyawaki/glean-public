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
from typing import Any, Optional

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


def strip_markup(text: str) -> str:
    """Remove JATS/HTML markup (<scp>, <jats:..., <i>, etc.) from titles/abstracts
    and normalize. Re-exported so glean internals (graph fetch, etc.) can use
    the same logic as the main intake path.
    """
    _ensure_on_path()
    from resolver import strip_markup as _fn
    return _fn(text)


def resolve_ams_landing_url(doi: str) -> Optional[str]:
    _ensure_on_path()
    from pdf_fetch import resolve_ams_landing_url as _fn
    return _fn(doi)


def is_preprint_doi(doi: str) -> bool:
    _ensure_on_path()
    from resolver import is_preprint_doi as _fn
    return bool(_fn(doi))


def fetch_pdf(
    doi: str,
    publisher_url: str,
    title: str,
    *,
    fetcher: Any = None,
    dir_path: str,
) -> Any:
    _ensure_on_path()
    from pdf_fetch import fetch_pdf as _fn
    return _fn(doi, publisher_url, title, fetcher=fetcher, dir_path=dir_path)


def make_pdf_fetcher() -> Any:
    _ensure_on_path()
    from pdf_fetch import PdfFetcher
    return PdfFetcher()


def pdf_backlog_add(
    doi: str,
    *,
    title: str = "",
    citekey: str = "",
    tried: Optional[list] = None,
) -> None:
    _ensure_on_path()
    import pdf_backlog
    pdf_backlog.add(doi, title=title, citekey=citekey, tried=tried)


def pdf_backlog_remove(doi: str) -> bool:
    _ensure_on_path()
    import pdf_backlog
    return bool(pdf_backlog.remove(doi))
