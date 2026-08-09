"""Durable citation-graph enrichment worker."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
import time
from typing import Callable, Optional

from glean.graph import crossref, openalex
from glean.index import Index
from glean.schema import ReferenceGraphState, ReferenceID
from glean.store import Library


Fetch = Callable[[str], Optional[dict]]
Hydrate = Callable[[list[str]], list[dict]]


class GraphFetchError(RuntimeError):
    pass


class GraphWorker:
    def __init__(
        self,
        library: Library,
        index: Index,
        *,
        fetch_openalex: Fetch = openalex.fetch,
        fetch_crossref: Fetch = crossref.fetch,
        hydrate_openalex: Hydrate = openalex.fetch_metadata_batch,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.library = library
        self.index = index
        self.fetch_openalex = fetch_openalex
        self.fetch_crossref = fetch_crossref
        self.hydrate_openalex = hydrate_openalex
        self.now = now
        self.sleep = sleep
        self.consecutive_failures = 0
        self.circuit_open_until: Optional[datetime] = None

    def run_once(self) -> bool:
        current = self.now()
        self.index.reconcile_graph_jobs(now=current)
        if self.index.graph_is_paused():
            return False
        if self.circuit_open_until and current < self.circuit_open_until:
            return False
        citekey = self.index.claim_graph_job(now=current)
        if citekey is None:
            references = self.index.claim_work_hydration_batch(now=current)
            if not references:
                return False
            try:
                metadata = self.hydrate_openalex(
                    [reference.serialized for reference in references]
                )
                fetched_at = self.now().date().isoformat()
                for work in metadata:
                    self.index.upsert_work_metadata(
                        work,
                        source="openalex",
                        fetched_at=fetched_at,
                    )
                complete = all(
                    self.index.finish_work_hydration(reference, now=self.now())
                    for reference in references
                )
                if complete:
                    self.consecutive_failures = 0
                    self.circuit_open_until = None
                else:
                    self._record_transient_failure()
                print(
                    f"glean graph worker hydration batch size={len(references)} "
                    f"complete={complete}",
                    flush=True,
                )
            except Exception as exc:
                for reference in references:
                    self.index.finish_work_hydration(
                        reference,
                        error=str(exc),
                        now=self.now(),
                    )
                self._record_transient_failure()
                print(
                    f"glean graph worker hydration failed size={len(references)}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return True
        try:
            self.enrich(citekey)
        except Exception as exc:
            state = self.index.fail_graph_job(citekey, str(exc), now=self.now())
            self._record_transient_failure()
            print(
                f"glean graph worker failed {citekey} state={state}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        else:
            self.index.finish_graph_job(citekey)
            self.consecutive_failures = 0
            self.circuit_open_until = None
            enriched = self.library.load(citekey)
            graph_status = (
                enriched.reference_graph.status if enriched.reference_graph else "unknown"
            )
            print(
                f"glean graph worker indexed {citekey} status={graph_status} "
                f"references={len(enriched.references)}",
                flush=True,
            )
        return True

    def _record_transient_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= 10:
            self.circuit_open_until = self.now() + timedelta(minutes=15)
            self.consecutive_failures = 0

    def enrich(self, citekey: str) -> None:
        if not self.library.exists(citekey):
            raise GraphFetchError(f"record disappeared: {citekey}")
        source = self.library.load(citekey)
        if not source.doi:
            raise GraphFetchError(f"{citekey} has no DOI")

        providers: list[str] = []
        result = self.fetch_openalex(source.doi)
        if result is not None:
            providers.append("openalex")
        if result is None or not result.get("references"):
            fallback = self.fetch_crossref(source.doi)
            if fallback is not None:
                providers.append("crossref")
                result = fallback
        if result is None:
            raise GraphFetchError(f"no citation metadata available for {source.doi}")

        references: list[ReferenceID] = []
        seen: set[tuple[str, str]] = set()
        for raw in result.get("references") or []:
            reference = ReferenceID.parse(raw)
            key = (reference.kind, reference.normalized_value)
            if key not in seen:
                references.append(reference)
                seen.add(key)

        fetched_at = self.now().date()
        source_metadata = result.get("metadata") or {}
        published_at = None
        if source_metadata.get("published_at"):
            try:
                published_at = date.fromisoformat(str(source_metadata["published_at"]))
            except ValueError:
                published_at = None
        with self.library.mutation_lock():
            latest = self.library.load(citekey)
            if latest.published_at is None and published_at is not None:
                latest.published_at = published_at
            latest.references = references
            latest.reference_graph = ReferenceGraphState(
                status="complete" if references else "empty",
                providers=providers,
                fetched_at=fetched_at,
            )
            self.library._save_unlocked(latest, today=fetched_at)
            self.index._upsert(latest)
            if source_metadata:
                self.index.upsert_work_metadata(
                    source_metadata,
                    source=providers[-1],
                    fetched_at=fetched_at.isoformat(),
                    overwrite=False,
                )
            for work in result.get("works") or []:
                self.index.upsert_work_metadata(
                    work,
                    source="openalex",
                    fetched_at=fetched_at.isoformat(),
                )

    def run_forever(self, *, idle_interval: float = 5.0) -> None:
        while True:
            try:
                worked = self.run_once()
            except Exception as exc:
                print(f"glean graph worker loop failed: {exc}", file=sys.stderr, flush=True)
                worked = False
            if not worked:
                self.sleep(idle_interval)
