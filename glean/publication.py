"""Durable publication-date enrichment for library records."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
import time
from typing import Callable, Optional

from glean.graph import crossref, openalex
from glean.index import Index
from glean.store import Library


FetchMetadata = Callable[[str], Optional[dict]]


class PublicationFetchError(RuntimeError):
    pass


class PublicationWorker:
    def __init__(
        self,
        library: Library,
        index: Index,
        *,
        fetch_crossref: FetchMetadata = crossref.fetch_metadata,
        fetch_openalex: FetchMetadata = openalex.fetch_source_metadata,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.library = library
        self.index = index
        self.fetch_crossref = fetch_crossref
        self.fetch_openalex = fetch_openalex
        self.now = now
        self.sleep = sleep
        self.consecutive_failures = 0
        self.circuit_open_until: Optional[datetime] = None

    metadata_unavailable_error = "publication metadata unavailable"

    def run_once(self) -> bool:
        current = self.now()
        self.index.reconcile_publication_jobs(now=current)
        if self.index.publication_is_paused():
            return False
        if self.circuit_open_until and current < self.circuit_open_until:
            return False
        citekey = self.index.claim_publication_job(now=current)
        if citekey is None:
            return False
        try:
            published_at, provider = self.enrich(citekey)
        except Exception as exc:
            state = self.index.fail_publication_job(
                citekey,
                str(exc),
                now=self.now(),
            )
            self._record_transient_failure()
            print(
                f"glean publication worker failed {citekey} state={state}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        else:
            self.index.finish_publication_job(
                citekey,
                found=published_at is not None,
                now=self.now(),
            )
            self.consecutive_failures = 0
            self.circuit_open_until = None
            value = published_at.isoformat() if published_at else "empty"
            print(
                f"glean publication worker indexed {citekey} "
                f"published_at={value} provider={provider}",
                flush=True,
            )
        return True

    def _record_transient_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= 10:
            self.circuit_open_until = self.now() + timedelta(minutes=15)
            self.consecutive_failures = 0

    @staticmethod
    def _date_from(metadata: Optional[dict]) -> Optional[date]:
        value = (metadata or {}).get("published_at")
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    def enrich(self, citekey: str) -> tuple[Optional[date], str]:
        if not self.library.exists(citekey):
            raise PublicationFetchError(f"record disappeared: {citekey}")
        source = self.library.load(citekey)
        if not source.doi:
            raise PublicationFetchError(f"{citekey} has no DOI")

        crossref_metadata = self.fetch_crossref(source.doi)
        published_at = self._date_from(crossref_metadata)
        provider = "crossref" if crossref_metadata is not None else ""
        openalex_metadata = None
        if published_at is None:
            openalex_metadata = self.fetch_openalex(source.doi)
            published_at = self._date_from(openalex_metadata)
            if openalex_metadata is not None:
                provider = "openalex"
        if crossref_metadata is None and openalex_metadata is None:
            raise PublicationFetchError(self.metadata_unavailable_error)

        if published_at is not None:
            with self.library.mutation_lock():
                latest = self.library.load(citekey)
                if latest.published_at is None:
                    latest.published_at = published_at
                    self.library._save_unlocked(latest, today=self.now().date())
                    self.index._upsert(latest)
                else:
                    published_at = latest.published_at
                    provider = "existing"
        return published_at, provider or "none"

    def run_forever(
        self,
        *,
        idle_interval: float = 5.0,
        request_interval: float = 0.2,
    ) -> None:
        while True:
            try:
                worked = self.run_once()
            except Exception as exc:
                print(
                    f"glean publication worker loop failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                worked = False
            self.sleep(request_interval if worked else idle_interval)
