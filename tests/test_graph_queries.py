from __future__ import annotations

from glean.graph.queries import (
    Stub,
    incoming_for,
    outgoing_for,
    shortest_path,
    unread_cited_by_read,
)
from glean.index import Index
from glean.schema import Record
from glean.store import Library


def test_outgoing_resolves_records_and_stubs(index: Index, lib: Library):
    index.rebuild()
    out = outgoing_for(lib, index, "Wills2023Citation")
    # Order matches the references: list in the YAML.
    assert isinstance(out[0], Record) and out[0].citekey == "Miyawaki2024Attention"
    assert isinstance(out[1], Record) and out[1].citekey == "Arakawa1974Schubert"
    assert isinstance(out[2], Stub) and out[2].target == "10.1175/JAS-D-22-0011.1"
    assert isinstance(out[3], Stub) and out[3].target == "10.1038/s41586-020-12345-6"


def test_incoming_via_doi(index: Index, lib: Library):
    index.rebuild()
    m = lib.load("Miyawaki2024Attention")
    inc = incoming_for(lib, index, m)
    assert [r.citekey for r in inc] == ["Wills2023Citation"]


def test_incoming_via_citekey(index: Index, lib: Library):
    index.rebuild()
    # Sellers doesn't exist as a record, so build a phantom just to test the
    # query path with the citekey-form reference target.
    phantom = Record(
        citekey="Sellers1965PhysicalClimatology",
        type="book",
        title="Physical Climatology",
        authors=[],  # validation runs elsewhere; query path only needs citekey and doi
        year=1965,
    )
    inc = incoming_for(lib, index, phantom)
    assert {r.citekey for r in inc} == {"Miyawaki2024Attention", "Vallis2019AOFD"}


def test_unread_cited_by_read(index: Index, lib: Library):
    index.rebuild()
    # Wills2023Citation has read=true and references 4 targets:
    #   - 10.1175/JCLI-D-23-0456.1 (resolves -> Miyawaki2024Attention; skip)
    #   - 10.1175/1520-0469(...)   (resolves -> Arakawa1974Schubert;   skip)
    #   - 10.1175/JAS-D-22-0011.1  (stub)
    #   - 10.1038/s41586-020-12345-6 (stub)
    # With min=1, both external stubs should appear.
    results = unread_cited_by_read(lib, index, min_citations=1)
    target_set = {s.target for s, _ in results}
    assert "10.1175/JAS-D-22-0011.1" in target_set
    assert "10.1038/s41586-020-12345-6" in target_set
    # With min=2, no stub is cited by >=2 read records, so empty.
    assert unread_cited_by_read(lib, index, min_citations=2) == []


def test_shortest_path_direct(index: Index, lib: Library):
    index.rebuild()
    path = shortest_path(lib, index, "Wills2023Citation", "Miyawaki2024Attention")
    assert path == ["Wills2023Citation", "Miyawaki2024Attention"]


def test_shortest_path_no_path(index: Index, lib: Library):
    index.rebuild()
    # Reverse direction isn't connected.
    assert shortest_path(lib, index, "Miyawaki2024Attention", "Wills2023Citation") is None
