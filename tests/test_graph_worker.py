from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from glean.graph.worker import GraphWorker
from glean.index import GRAPH_RETRY_DELAYS_SECONDS, Index
from glean.schema import Author, Record, ReferenceGraphState, ReferenceID
from glean.store import Library


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def paper(*, citekey: str = "Example2026Paper") -> Record:
    return Record(
        citekey=citekey,
        type="journal-article",
        title="Original library title",
        authors=[Author(family="Example", given="Erin")],
        year=2026,
        doi=f"10.1234/{citekey.lower()}",
        source="crossref",
        source_fetched_at=date(2026, 8, 1),
    )


def setup_index(tmp_path: Path, record: Record) -> tuple[Library, Index]:
    library = Library(tmp_path / "library")
    library.save(record, today=date(2026, 8, 1))
    index = Index(library, tmp_path / "index.sqlite")
    index.rebuild()
    return library, index


def test_worker_commits_typed_graph_and_preserves_concurrent_fields(tmp_path: Path):
    library, index = setup_index(tmp_path, paper())
    index.enqueue_graph_job("Example2026Paper")
    clock = Clock()

    def fetch_openalex(_doi: str) -> dict:
        latest = library.load("Example2026Paper")
        latest.notes = "edited while the network request was running"
        library.save(latest, today=date(2026, 8, 9))
        index._upsert(latest)
        return {
            "metadata": {
                "doi": latest.doi,
                "openalex_id": "W100",
                "title": "Provider title must not replace library metadata",
                "authors": [],
                "year": 2026,
            },
            "references": ["10.5678/target", "openalex:W200"],
            "works": [
                {
                    "doi": "10.5678/target",
                    "openalex_id": "W150",
                    "title": "Hydrated target",
                    "authors": [{"family": "Target", "given": "T."}],
                    "year": 2020,
                }
            ],
        }

    worker = GraphWorker(
        library,
        index,
        fetch_openalex=fetch_openalex,
        fetch_crossref=lambda _doi: None,
        hydrate_openalex=lambda _values: [],
        now=clock,
    )

    assert worker.run_once() is True
    stored = library.load("Example2026Paper")
    assert stored.notes == "edited while the network request was running"
    assert [reference.serialized for reference in stored.references] == [
        "doi:10.5678/target",
        "openalex:W200",
    ]
    assert stored.reference_graph == ReferenceGraphState(
        status="complete",
        providers=["openalex"],
        fetched_at=date(2026, 8, 9),
    )
    assert stored.source == "crossref"
    assert stored.source_fetched_at == date(2026, 8, 1)
    assert index.graph_job("Example2026Paper") is None
    source = index.graph_relation_state("Example2026Paper")
    assert source is not None
    assert source["source"]["title"] == "Original library title"
    aliases = index.connect().execute(
        "SELECT kind, value FROM work_aliases WHERE work_id = "
        "(SELECT work_id FROM library_works WHERE citekey = ?)",
        ("Example2026Paper",),
    ).fetchall()
    assert ("openalex", "W100") in aliases


def test_worker_uses_crossref_when_openalex_has_no_references(tmp_path: Path):
    library, index = setup_index(tmp_path, paper())
    index.enqueue_graph_job("Example2026Paper")
    worker = GraphWorker(
        library,
        index,
        fetch_openalex=lambda _doi: {
            "metadata": {}, "references": [], "works": [],
        },
        fetch_crossref=lambda _doi: {
            "metadata": {}, "references": ["10.9999/fallback"], "works": [],
        },
        hydrate_openalex=lambda _values: [],
        now=Clock(),
    )

    worker.run_once()

    stored = library.load("Example2026Paper")
    assert stored.references == [ReferenceID("doi", "10.9999/fallback")]
    assert stored.reference_graph is not None
    assert stored.reference_graph.providers == ["openalex", "crossref"]


def test_worker_records_successfully_empty_graph(tmp_path: Path):
    library, index = setup_index(tmp_path, paper())
    index.enqueue_graph_job("Example2026Paper")
    worker = GraphWorker(
        library,
        index,
        fetch_openalex=lambda _doi: {
            "metadata": {}, "references": [], "works": [],
        },
        fetch_crossref=lambda _doi: {
            "metadata": {}, "references": [], "works": [],
        },
        hydrate_openalex=lambda _values: [],
        now=Clock(),
    )

    worker.run_once()

    stored = library.load("Example2026Paper")
    assert stored.references == []
    assert stored.reference_graph is not None
    assert stored.reference_graph.status == "empty"


def test_worker_pause_and_retry_schedule(tmp_path: Path):
    library, index = setup_index(tmp_path, paper())
    index.enqueue_graph_job("Example2026Paper")
    clock = Clock()
    worker = GraphWorker(
        library,
        index,
        fetch_openalex=lambda _doi: (_ for _ in ()).throw(OSError("offline")),
        fetch_crossref=lambda _doi: None,
        hydrate_openalex=lambda _values: [],
        now=clock,
    )
    index.set_graph_paused(True)
    assert worker.run_once() is False
    assert index.graph_job("Example2026Paper")["state"] == "queued"
    index.set_graph_paused(False)

    for attempt, delay in enumerate(GRAPH_RETRY_DELAYS_SECONDS, start=1):
        assert worker.run_once() is True
        job = index.graph_job("Example2026Paper")
        assert job is not None
        assert job["attempts"] == attempt
        assert job["state"] == "retry_wait"
        expected = clock.value + timedelta(seconds=delay)
        assert job["next_attempt_at"] == expected.isoformat()
        clock.value = expected

    assert worker.run_once() is True
    assert index.graph_job("Example2026Paper")["state"] == "failed"


def test_worker_hydrates_rebuilt_external_metadata_in_batch(tmp_path: Path):
    record = paper()
    record.references = [ReferenceID("openalex", "W200")]
    record.reference_graph = ReferenceGraphState(
        status="complete",
        providers=["openalex"],
        fetched_at=date(2026, 8, 1),
    )
    library, index = setup_index(tmp_path, record)
    requested: list[list[str]] = []

    def hydrate(values: list[str]) -> list[dict]:
        requested.append(values)
        return [{
            "openalex_id": "W200",
            "doi": None,
            "title": "Rehydrated after rebuild",
            "authors": [{"family": "External", "given": None}],
            "year": 1999,
        }]

    worker = GraphWorker(
        library,
        index,
        fetch_openalex=lambda _doi: None,
        fetch_crossref=lambda _doi: None,
        hydrate_openalex=hydrate,
        now=Clock(),
    )

    assert worker.run_once() is True
    assert requested == [["openalex:W200"]]
    assert index.reference_entries("Example2026Paper")[0]["title"] == (
        "Rehydrated after rebuild"
    )


def test_circuit_breaker_opens_after_ten_consecutive_failures(tmp_path: Path):
    library, index = setup_index(tmp_path, paper())
    clock = Clock()
    worker = GraphWorker(library, index, now=clock)

    for _ in range(10):
        worker._record_transient_failure()

    assert worker.circuit_open_until == clock.value + timedelta(minutes=15)
    assert worker.consecutive_failures == 0
