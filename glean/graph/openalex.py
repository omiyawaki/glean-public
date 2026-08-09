"""OpenAlex client. Primary source for outgoing references.

Caches raw JSON under ~/Library/Caches/glean/openalex/<doi>.json (30-day TTL).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx


CACHE_ROOT = Path("~/Library/Caches/glean/openalex").expanduser()
DEFAULT_TTL_DAYS = 30
MAILTO = "user@example.com"
TIMEOUT = 15.0
REQUEST_INTERVAL_SECONDS = 0.2


class _RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self.interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()


_RATE_LIMITER = _RateLimiter(REQUEST_INTERVAL_SECONDS)


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
    _RATE_LIMITER.wait()
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


def _fetch_works_by_ids(
    openalex_ids: list[str],
    *,
    http_get: Callable[[str], Optional[dict]],
    ttl_days: int,
    batch_size: int = 50,
) -> dict[str, dict]:
    """Fetch referenced works in OR-filter batches while preserving a per-work cache."""
    keys = list(dict.fromkeys(value.rsplit("/", 1)[-1].upper() for value in openalex_ids))
    works: dict[str, dict] = {}
    missing: list[str] = []
    for key in keys:
        cached = _cache_get(_cache_path("refs", key), ttl_days)
        if cached is None:
            missing.append(key)
        else:
            works[key] = cached
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset:offset + batch_size]
        query = urlencode(
            {
                "filter": "openalex:" + "|".join(batch),
                "per-page": str(len(batch)),
                "select": "id,doi,display_name,publication_year,authorships",
                "mailto": MAILTO,
            },
            safe="|,:/",
        )
        payload = http_get(f"https://api.openalex.org/works?{query}")
        if payload is None:
            continue
        for work in payload.get("results") or []:
            key = str(work.get("id") or "").rsplit("/", 1)[-1].upper()
            if not key:
                continue
            works[key] = work
            _cache_put(_cache_path("refs", key), work)
    return works


def _fetch_works_by_dois(
    dois: list[str],
    *,
    http_get: Callable[[str], Optional[dict]],
    ttl_days: int,
    batch_size: int = 50,
) -> dict[str, dict]:
    keys = list(dict.fromkeys(value.lower() for value in dois))
    works: dict[str, dict] = {}
    missing: list[str] = []
    for key in keys:
        cached = _cache_get(_cache_path("refs-doi", key), ttl_days)
        if cached is None:
            missing.append(key)
        else:
            works[key] = cached
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset:offset + batch_size]
        query = urlencode(
            {
                "filter": "doi:" + "|".join(batch),
                "per-page": str(len(batch)),
                "select": "id,doi,display_name,publication_year,authorships",
                "mailto": MAILTO,
            },
            safe="|,:/",
        )
        payload = http_get(f"https://api.openalex.org/works?{query}")
        if payload is None:
            continue
        for work in payload.get("results") or []:
            doi = str(work.get("doi") or "")
            key = doi.removeprefix("https://doi.org/").lower()
            if not key:
                continue
            works[key] = work
            _cache_put(_cache_path("refs-doi", key), work)
    return works


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
    published_at = w.get("publication_date")
    if published_at:
        try:
            published_at = date.fromisoformat(str(published_at)).isoformat()
        except ValueError:
            published_at = None
    return {
        "openalex_id": str(w.get("id") or "").rsplit("/", 1)[-1] or None,
        "doi": w.get("doi") and w["doi"].replace("https://doi.org/", ""),
        "title": w.get("display_name") or w.get("title") or "",
        "authors": authors,
        "year": w.get("publication_year"),
        "published_at": published_at,
        "type": w.get("type"),
    }


def fetch_source_metadata(
    doi: str,
    *,
    http_get: Callable[[str], Optional[dict]] = _default_http_get,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> Optional[dict]:
    """Fetch normalized source metadata without hydrating its references."""
    work = _fetch_work_by_doi(doi, http_get=http_get, ttl_days=ttl_days)
    return _normalize_metadata(work) if work is not None else None


def fetch(doi: str, *, http_get: Callable[[str], Optional[dict]] = _default_http_get,
          ttl_days: int = DEFAULT_TTL_DAYS) -> Optional[dict]:
    """Return normalized source metadata, stable references, and target works."""
    w = _fetch_work_by_doi(doi, http_get=http_get, ttl_days=ttl_days)
    if w is None:
        return None
    ref_ids: list[str] = w.get("referenced_works") or []
    target_works = _fetch_works_by_ids(
        ref_ids,
        http_get=http_get,
        ttl_days=ttl_days,
    )
    references: list[str] = []
    normalized_works: list[dict] = []
    for rid in ref_ids:
        key = rid.rsplit("/", 1)[-1].upper()
        sub = target_works.get(key)
        if sub is None:
            references.append(f"openalex:{key}")
            continue
        metadata = _normalize_metadata(sub)
        normalized_works.append(metadata)
        references.append(metadata.get("doi") or f"openalex:{key}")
    return {
        "metadata": _normalize_metadata(w),
        "references": references,
        "works": normalized_works,
    }


def fetch_metadata_batch(
    identifiers: list[str],
    *,
    http_get: Callable[[str], Optional[dict]] = _default_http_get,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> list[dict]:
    """Hydrate up to 50 DOI/OpenAlex identities using OR-filter requests."""
    openalex_ids: list[str] = []
    dois: list[str] = []
    for identifier in identifiers:
        kind, _, value = identifier.partition(":")
        if kind.lower() == "openalex" and value:
            openalex_ids.append(value)
        elif kind.lower() == "doi" and value:
            dois.append(value)
    works = list(
        _fetch_works_by_ids(
            openalex_ids,
            http_get=http_get,
            ttl_days=ttl_days,
        ).values()
    )
    works.extend(
        _fetch_works_by_dois(
            dois,
            http_get=http_get,
            ttl_days=ttl_days,
        ).values()
    )
    deduplicated: dict[str, dict] = {}
    for work in works:
        key = str(work.get("id") or work.get("doi") or id(work))
        deduplicated[key] = work
    return [_normalize_metadata(work) for work in deduplicated.values()]
