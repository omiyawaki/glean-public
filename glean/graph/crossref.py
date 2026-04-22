"""Crossref client. Fallback when OpenAlex has no record or no references.

Caches raw JSON under ~/Library/Caches/glean/crossref/<doi>.json (30-day TTL).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

import httpx


CACHE_ROOT = Path("~/Library/Caches/glean/crossref").expanduser()
DEFAULT_TTL_DAYS = 30
MAILTO = "user@example.com"
TIMEOUT = 15.0


def _cache_path(doi: str) -> Path:
    safe = doi.replace("/", "_").replace(":", "_")
    return CACHE_ROOT / f"{safe}.json"


def _cache_get(doi: str, ttl_days: int) -> Optional[dict]:
    p = _cache_path(doi)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(doi: str, data: dict) -> None:
    p = _cache_path(doi)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _default_http_get(url: str) -> Optional[dict]:
    try:
        r = httpx.get(
            url,
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": f"glean/0.1 (mailto:{MAILTO})"},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _normalize_metadata(msg: dict) -> dict:
    title = (msg.get("title") or [""])[0]
    authors = []
    for a in msg.get("author") or []:
        fam = (a.get("family") or "").strip()
        giv = (a.get("given") or "").strip() or None
        if fam:
            authors.append({"family": fam, "given": giv})
    year = None
    issued = (msg.get("issued") or {}).get("date-parts") or [[]]
    if issued and issued[0]:
        year = issued[0][0]
    return {
        "doi": msg.get("DOI"),
        "title": title,
        "authors": authors,
        "year": year,
        "type": msg.get("type"),
    }


def fetch(doi: str, *, http_get: Callable[[str], Optional[dict]] = _default_http_get,
          ttl_days: int = DEFAULT_TTL_DAYS) -> Optional[dict]:
    """Return {metadata: {...}, references: [doi, ...]} or None on failure."""
    cached = _cache_get(doi, ttl_days)
    if cached is not None:
        data = cached
    else:
        url = f"https://api.crossref.org/works/{doi}"
        data = http_get(url)
        if data is None:
            return None
        _cache_put(doi, data)
    msg = data.get("message") or {}
    ref_dois = []
    for ref in msg.get("reference") or []:
        d = ref.get("DOI")
        if d:
            ref_dois.append(d)
    return {
        "metadata": _normalize_metadata(msg),
        "references": ref_dois,
    }
