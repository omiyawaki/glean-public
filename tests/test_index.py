from datetime import date

from glean.index import Index
from glean.schema import Author, Record
from glean.store import Library


def test_rebuild_populates_records(index: Index, lib: Library):
    index.rebuild()
    conn = index.connect()
    n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n == 4


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
    assert n == 3


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
