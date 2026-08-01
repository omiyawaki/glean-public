from datetime import date
from pathlib import Path

import pytest

from glean.index import Index
from glean.schema import Author, Record
from glean.store import Library


def test_rebuild_populates_records(index: Index, lib: Library):
    index.rebuild()
    conn = index.connect()
    n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 5


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
