from datetime import date
from pathlib import Path
import sqlite3

import pytest

from glean.index import SCHEMA_VERSION, Index
from glean.schema import Author, Record
from glean.store import Library


def test_rebuild_populates_records(index: Index, lib: Library):
    index.rebuild()
    conn = index.connect()
    n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 5


def test_index_uses_versioned_schema(index: Index):
    assert index.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        row[0]
        for row in index.connect().execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert {
        "works", "work_aliases", "library_works", "citation_edges", "graph_jobs",
        "publication_jobs", "outgoing", "stubs",
    } <= tables
    object_types = dict(
        index.connect().execute(
            "SELECT name, type FROM sqlite_master WHERE name IN ('outgoing', 'stubs')"
        )
    )
    assert object_types == {"outgoing": "view", "stubs": "view"}
    with pytest.raises(sqlite3.OperationalError):
        index.connect().execute(
            "INSERT INTO outgoing(citekey, target) VALUES ('A', 'B')"
        )


def test_rebuild_populates_authors(index: Index):
    index.rebuild()
    conn = index.connect()
    rows = conn.execute(
        "SELECT family, given FROM authors WHERE citekey = ? ORDER BY ord",
        ("Miyawaki2024Attention",),
    ).fetchall()
    assert rows == [("Miyawaki", "Osamu"), ("Vallis", "Geoffrey")]


def test_rebuild_populates_attachments(index: Index):
    index.rebuild()
    conn = index.connect()
    rows = conn.execute(
        "SELECT kind FROM attachments WHERE citekey = ? ORDER BY ord",
        ("Miyawaki2024Attention",),
    ).fetchall()
    assert [r[0] for r in rows] == ["main", "supplement"]


def test_rebuild_populates_tags(index: Index, lib: Library):
    r = lib.load("Miyawaki2024Attention")
    r.tags = ["proj-a", "upper-troposphere"]
    lib.save(r, today=date(2026, 4, 20))

    index.rebuild()

    conn = index.connect()
    rows = conn.execute(
        "SELECT tag FROM tags WHERE citekey = ? ORDER BY tag",
        ("Miyawaki2024Attention",),
    ).fetchall()
    assert [r[0] for r in rows] == ["proj-a", "upper-troposphere"]


def test_search_by_title(index: Index):
    index.rebuild()
    hits = index.search("convection")
    citekeys = [h[0] for h in hits]
    assert "Miyawaki2024Attention" in citekeys
    assert "Arakawa1974Schubert" in citekeys


def test_search_by_author_given_name(index: Index):
    index.rebuild()
    hits = index.search("Osamu")
    citekeys = {h[0] for h in hits}
    assert "Miyawaki2024Attention" in citekeys
    assert "Miyawaki2023Transformer" in citekeys


def test_search_by_tag(index: Index, lib: Library):
    r = lib.load("Miyawaki2024Attention")
    r.tags = ["proj-a"]
    lib.save(r, today=date(2026, 4, 20))

    index.rebuild()

    hits = index.search("proj-a")
    assert [h[0] for h in hits] == ["Miyawaki2024Attention"]


def test_search_hyphenated_query_does_not_crash(index: Index):
    # A hyphen must be matched literally, not parsed as an FTS5 operator.
    # Regression: raised sqlite3.OperationalError "no such column: scale".
    index.rebuild()
    citekeys = {h[0] for h in index.search("large-scale")}
    assert "Arakawa1974Schubert" in citekeys
    assert "Vallis2019AOFD" in citekeys


def test_match_query_quotes_special_tokens():
    assert Index._match_query("well-mixed") == '"well-mixed"'
    assert Index._match_query("ocean heat") == '"ocean" "heat"'
    assert Index._match_query('foo"bar') == '"foo""bar"'
    assert Index._match_query("   ") == ""


def test_outgoing_resolves_doi_and_citekey(index: Index):
    index.rebuild()
    out = index.outgoing("Miyawaki2024Attention")
    assert ("10.1175/JAS-D-22-0011.1", None) in out        # DOI, not in library -> stub
    assert ("Sellers1965PhysicalClimatology", None) in out  # citekey, also a stub


def test_external_aliases_merge_without_changing_reference_order(index: Index):
    index.rebuild()
    before = [entry["target_id"] for entry in index.reference_entries("Wills2023Citation")]

    index.upsert_work_metadata(
        {
            "doi": "10.1038/s41586-020-12345-6",
            "openalex_id": "W987654321",
            "title": "External cached title",
            "authors": [{"family": "External", "given": "E."}],
            "year": 2020,
        },
        source="openalex",
        fetched_at="2026-08-09",
    )

    entries = index.reference_entries("Wills2023Citation")
    assert [entry["target_id"] for entry in entries] == before
    external = entries[-1]
    assert external["openalex_id"] == "W987654321"
    assert external["title"] == "External cached title"


def test_record_rewrite_preserves_graph_retry_state(index: Index, lib: Library):
    from datetime import datetime, timezone

    index.rebuild()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    index.enqueue_graph_job("Miyawaki2024Attention", now=now)
    assert index.claim_graph_job(now=now) == "Miyawaki2024Attention"
    index.fail_graph_job("Miyawaki2024Attention", "offline", now=now)

    record = lib.load("Miyawaki2024Attention")
    record.notes = "unrelated edit"
    lib.save(record, today=date(2026, 8, 9))
    index._upsert(record)

    job = index.graph_job("Miyawaki2024Attention")
    assert job is not None
    assert job["state"] == "retry_wait"
    assert job["attempts"] == 1


def test_record_rewrite_preserves_publication_retry_state(index: Index, lib: Library):
    from datetime import datetime, timezone

    index.rebuild()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    index.enqueue_publication_job("Miyawaki2024Attention", now=now)
    assert index.claim_publication_job(now=now) == "Miyawaki2024Attention"
    index.fail_publication_job("Miyawaki2024Attention", "offline", now=now)

    record = lib.load("Miyawaki2024Attention")
    record.notes = "unrelated edit"
    lib.save(record, today=date(2026, 8, 9))
    index._upsert(record)

    job = index.publication_job("Miyawaki2024Attention")
    assert job is not None
    assert job["state"] == "retry_wait"
    assert job["attempts"] == 1


def test_publication_reconciliation_accounts_for_missing_dates(index: Index):
    index.rebuild()

    index.reconcile_publication_jobs()
    status = index.publication_status()

    assert status["total"] == 5
    assert status["eligible"] > 0
    assert status["queued"] == status["eligible"]
    assert status["complete"] == 0


def test_publication_coverage_counts_only_doi_records_as_eligible_complete(
    index: Index,
    lib: Library,
):
    index.rebuild()
    doi_record = lib.load("Miyawaki2024Attention")
    doi_record.published_at = date(2024, 6, 15)
    lib.save(doi_record, today=date(2026, 8, 9))
    index._upsert(doi_record)

    no_doi_record = lib.load("Vallis2019AOFD")
    no_doi_record.published_at = date(1965, 1, 15)
    lib.save(no_doi_record, today=date(2026, 8, 9))
    index._upsert(no_doi_record)
    index.reconcile_publication_jobs()

    status = index.publication_status()
    assert status["complete"] == 1
    assert status["eligible"] + status["unsupported"] == status["total"]


def test_stubs_populated_for_unresolved_targets(index: Index):
    index.rebuild()
    conn = index.connect()
    rows = conn.execute("SELECT doi, citekey FROM stubs").fetchall()
    pairs = set(rows)
    assert ("10.1175/JAS-D-22-0011.1", None) in pairs
    assert (None, "Sellers1965PhysicalClimatology") in pairs


def test_incoming_by_citekey_target(index: Index, lib: Library):
    index.rebuild()
    # Vallis2019AOFD and Miyawaki2024Attention both reference Sellers1965PhysicalClimatology.
    # There's no such record in the library; construct a phantom to test the query path.
    stub = Record(
        citekey="Sellers1965PhysicalClimatology",
        type="book",
        title="Physical Climatology",
        authors=[Author(family="Sellers", given="William")],
        year=1965,
        added_at=date(2026, 4, 20),
        modified_at=date(2026, 4, 20),
    )
    assert set(index.incoming(stub)) == {"Miyawaki2024Attention", "Vallis2019AOFD"}


def test_refresh_detects_changes(index: Index, lib: Library):
    index.rebuild()
    r = lib.load("Arakawa1974Schubert")
    r.title = "Updated title"
    lib.save(r, today=date(2026, 4, 20))
    added, updated, deleted = index.refresh()
    assert (added, updated, deleted) == (0, 1, 0)
    conn = index.connect()
    title = conn.execute(
        "SELECT title FROM records WHERE citekey = ?", ("Arakawa1974Schubert",)
    ).fetchone()[0]
    assert title == "Updated title"


def test_refresh_detects_deletions(index: Index, lib: Library):
    index.rebuild()
    lib.delete("Miyawaki2023Transformer")
    added, updated, deleted = index.refresh()
    assert (added, updated, deleted) == (0, 0, 1)
    conn = index.connect()
    n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 4


def test_refresh_refuses_to_clear_index_after_empty_library_scan(
    index: Index,
    lib: Library,
    monkeypatch,
):
    index.rebuild()
    monkeypatch.setattr(lib, "scan_records", lambda: ([], set()))

    with pytest.raises(RuntimeError, match="refusing to delete the entire index"):
        index.refresh()

    n = index.connect().execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 5


def test_refresh_preserves_existing_rows_for_unavailable_placeholders(
    index: Index,
    lib: Library,
    monkeypatch,
):
    index.rebuild()
    records, _ = lib.scan_records()
    unavailable = records[0].citekey
    monkeypatch.setattr(
        lib,
        "scan_records",
        lambda: (records[1:], {unavailable}),
    )

    assert index.refresh() == (0, 0, 0)
    n = index.connect().execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 5


def test_rebuild_refuses_incomplete_file_provider_scan(
    index: Index,
    lib: Library,
    monkeypatch,
):
    index.rebuild()
    records, _ = lib.scan_records()
    monkeypatch.setattr(
        lib,
        "scan_records",
        lambda: (records[1:], {records[0].citekey}),
    )

    with pytest.raises(RuntimeError, match="incomplete index"):
        index.rebuild()

    n = index.connect().execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 5


def test_schema_recovery_preserves_placeholder_rows_until_they_materialize(
    lib: Library,
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "migration-placeholder.sqlite"
    initial = Index(lib, db_path)
    initial.connect()
    all_records, _ = lib.scan_records()
    unavailable_record = all_records[0]
    with initial.connect():
        for table in ("citation_edges", "library_works", "work_aliases", "works"):
            initial.connect().execute(f"DELETE FROM {table}")
    initial.close()

    migrating = Index(lib, db_path)
    monkeypatch.setattr(
        lib,
        "scan_records",
        lambda: (all_records[1:], {unavailable_record.citekey}),
    )
    conn = migrating.connect()

    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM library_works").fetchone()[0] == 4
    assert conn.execute(
        "SELECT yaml_modified FROM records WHERE citekey = ?",
        (unavailable_record.citekey,),
    ).fetchone()[0] == "__migration_pending__"

    monkeypatch.setattr(lib, "scan_records", lambda: (all_records, set()))
    assert migrating.refresh() == (0, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM library_works").fetchone()[0] == 5


def test_refresh_detects_additions(index: Index, lib: Library):
    index.rebuild()
    r = Record(
        citekey="Held2019Radiative",
        type="journal-article",
        title="Radiative-convective equilibrium",
        authors=[Author(family="Held", given="Isaac")],
        year=2019,
        added_at=None,
        modified_at=None,
    )
    lib.save(r, today=date(2026, 4, 20))
    added, updated, deleted = index.refresh()
    assert (added, updated, deleted) == (1, 0, 0)


def test_refresh_retries_transient_record_read_deadlock(
    index: Index,
    lib: Library,
    monkeypatch,
):
    index.rebuild()
    r = Record(
        citekey="Held2019Radiative",
        type="journal-article",
        title="Radiative-convective equilibrium",
        authors=[Author(family="Held", given="Isaac")],
        year=2019,
        added_at=None,
        modified_at=None,
    )
    lib.save(r, today=date(2026, 4, 20))
    original_read_text = Path.read_text
    calls = 0

    def flaky_read_text(path: Path, *args, **kwargs):
        nonlocal calls
        if path.name == "Held2019Radiative.yaml" and calls == 0:
            calls += 1
            raise OSError(11, "Resource deadlock avoided")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    added, updated, deleted = index.refresh()

    assert (added, updated, deleted) == (1, 0, 0)
    assert calls == 1
