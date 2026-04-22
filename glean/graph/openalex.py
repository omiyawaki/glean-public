"""OpenAlex client. Primary source for outgoing references.

Caches raw JSON under ~/Library/Caches/glean/openalex/<doi>.json (30-day TTL).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

import httpx


CACHE_ROOT = Path("~/Library/Caches/glean/openalex").expanduser()
DEFAULT_TTL_DAYS = 30
MAILTO = "user@example.com"
TIMEOUT = 15.0


def _cache_path(kind: str, key: str) -> Path:
    safe = key.replace("/", "_").replace(":", "_")
    return CACHE_ROOT / kind / f"{safe}.json"


def _cache_get(path: Path, ttl_days: int) -> Optional[dict]:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _default_http_get(url: str) -> Optional[dict]:
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _fetch_work_by_doi(doi: str, *, http_get: Callable[[str], Optional[dict]],
                       ttl_days: int) -> Optional[dict]:
    cached = _cache_get(_cache_path("works", doi), ttl_days)
    if cached is not None:
        return cached
    url = f"https://api.openalex.org/works/doi:{doi}?mailto={MAILTO}"
    data = http_get(url)
    if data is None:
        return None
    _cache_put(_cache_path("works", doi), data)
    return data


def _fetch_work_by_id(openalex_id: str, *, http_get: Callable[[str], Optional[dict]],
                      ttl_days: int) -> Optional[dict]:
    """openalex_id is a URL like https://openalex.org/W12345."""
    key = openalex_id.rsplit("/", 1)[-1]
    cached = _cache_get(_cache_path("refs", key), ttl_days)
    if cached is not None:
        return cached
    url = f"https://api.openalex.org/works/{key}?select=doi,title,publication_year&mailto={MAILTO}"
    data = http_get(url)
    if data is None:
        return None
    _cache_put(_cache_path("refs", key), data)
    return data


def _normalize_metadata(w: dict) -> dict:
    authors = []
    for a in w.get("authorships") or []:
        au = (a or {}).get("author") or {}
        name = au.get("display_name") or ""
        if name:
            # OpenAlex gives "Given Family" display names; split best-effort.
            parts = name.strip().rsplit(" ", 1)
            if len(parts) == 2:
                authors.append({"family": parts[1], "given": parts[0]})
            else:
                authors.append({"family": name, "given": None})
    return {
        "doi": w.get("doi") and w["doi"].replace("https://doi.org/", ""),
        "title": w.get("title") or "",
        "authors": authors,
        "year": w.get("publication_year"),
        "type": w.get("type"),
    }


def fetch(doi: str, *, http_get: Callable[[str], Optional[dict]] = _default_http_get,
          ttl_days: int = DEFAULT_TTL_DAYS) -> Optional[dict]:
    """Return {metadata: {...}, references: [doi_or_None, ...]} or None on failure.

    Reference DOIs may be None when the referenced work has no DOI in OpenAlex.
    Those entries are dropped from the final list.
    """
    w = _fetch_work_by_doi(doi, http_get=http_get, ttl_days=ttl_days)
    if w is None:
        return None
    ref_ids: list[str] = w.get("referenced_works") or []
    ref_dois: list[str] = []
    for rid in ref_ids:
        sub = _fetch_work_by_id(rid, http_get=http_get, ttl_days=ttl_days)
        if sub is None:
            continue
        d = sub.get("doi")
        if d:
            ref_dois.append(d.replace("https://doi.org/", ""))
    return {
        "metadata": _normalize_metadata(w),
        "references": ref_dois,
    }
