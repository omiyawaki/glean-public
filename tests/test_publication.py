from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from glean.index import GRAPH_RETRY_DELAYS_SECONDS, Index
from glean.publication import PublicationWorker
from glean.schema import Author, Record
from glean.store import Library


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def setup_index(tmp_path: Path) -> tuple[Library, Index]:
    library = Library(tmp_path / "library")
    library.save(Record(
        citekey="Example2026Paper",
        type="journal-article",
        title="Example paper",
        authors=[Author(family="Example", given="Erin")],
        year=2026,
        doi="10.1234/example",
    ), today=date(2026, 8, 1))
    index = Index(library, tmp_path / "index.sqlite")
    index.rebuild()
    index.enqueue_publication_job("Example2026Paper")
    return library, index


def test_worker_prefers_crossref_and_preserves_concurrent_fields(tmp_path: Path):
    library, index = setup_index(tmp_path)
    clock = Clock()

    def fetch_crossref(_doi: str) -> dict:
        latest = library.load("Example2026Paper")
        latest.notes = "edited while metadata was fetched"
        library.save(latest, today=date(2026, 8, 9))
        index._upsert(latest)
        return {"published_at": "2026-05-03"}

    worker = PublicationWorker(
        library,
        index,
        fetch_crossref=fetch_crossref,
        fetch_openalex=lambda _doi: {"published_at": "2026-05-04"},
        now=clock,
    )

    assert worker.run_once() is True
    stored = library.load("Example2026Paper")
    assert stored.published_at == date(2026, 5, 3)
    assert stored.notes == "edited while metadata was fetched"
    assert index.publication_job("Example2026Paper") is None


def test_worker_falls_back_to_openalex_and_records_empty(tmp_path: Path):
    library, index = setup_index(tmp_path)
    worker = PublicationWorker(
        library,
        index,
        fetch_crossref=lambda _doi: {"published_at": None},
        fetch_openalex=lambda _doi: {"published_at": "2026-04-02"},
        now=Clock(),
    )
    worker.run_once()
    assert library.load("Example2026Paper").published_at == date(2026, 4, 2)

    second = Record(
        citekey="Empty2025Paper",
        type="journal-article",
        title="No exact date",
        authors=[Author(family="Empty")],
        year=2025,
        doi="10.1234/empty",
    )
    library.save(second, today=date(2026, 8, 1))
    index._upsert(second)
    index.enqueue_publication_job(second.citekey)
    empty_worker = PublicationWorker(
        library,
        index,
        fetch_crossref=lambda _doi: {"published_at": None},
        fetch_openalex=lambda _doi: {"published_at": None},
        now=Clock(),
    )
    empty_worker.run_once()
    assert index.publication_job(second.citekey)["state"] == "empty"


def test_worker_uses_retry_schedule_for_provider_failures(tmp_path: Path):
    _library, index = setup_index(tmp_path)
    clock = Clock()
    worker = PublicationWorker(
        _library,
        index,
        fetch_crossref=lambda _doi: None,
        fetch_openalex=lambda _doi: None,
        now=clock,
    )

    for attempt, delay in enumerate(GRAPH_RETRY_DELAYS_SECONDS, start=1):
        assert worker.run_once() is True
        job = index.publication_job("Example2026Paper")
        assert job["state"] == "retry_wait"
        assert job["attempts"] == attempt
        clock.value += timedelta(seconds=delay)

    assert worker.run_once() is True
    assert index.publication_job("Example2026Paper")["state"] == "failed"
