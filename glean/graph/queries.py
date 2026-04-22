"""Graph queries: incoming_for, outgoing_for, unread_cited_by_read, shortest_path.

See DESIGN.md "Citation graph module".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from glean.citekey import looks_like_doi
from glean.index import Index
from glean.schema import Record
from glean.store import Library


@dataclass
class Stub:
    """An external referenced paper that isn't in the library."""
    target: str               # DOI or citekey, as stored in references:
    title: Optional[str] = None
    year: Optional[int] = None
    authors_flat: Optional[str] = None

    @property
    def is_doi(self) -> bool:
        return looks_like_doi(self.target)


def _resolve_target(index: Index, target: str) -> Optional[str]:
    """Return a matching in-library citekey for this target, or None for a stub.
    DOI comparison is case-insensitive."""
    conn = index.connect()
    if looks_like_doi(target):
        row = conn.execute(
            "SELECT citekey FROM records WHERE lower(doi) = lower(?)", (target,)
        ).fetchone()
    else:
        row = conn.execute("SELECT citekey FROM records WHERE citekey = ?", (target,)).fetchone()
    return row[0] if row else None


def _stub_for(index: Index, target: str) -> Stub:
    conn = index.connect()
    if looks_like_doi(target):
        row = conn.execute(
            "SELECT title, year, authors_flat FROM stubs WHERE doi = ?", (target,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT title, year, authors_flat FROM stubs WHERE citekey = ?", (target,)
        ).fetchone()
    if row:
        return Stub(target=target, title=row[0], year=row[1], authors_flat=row[2])
    return Stub(target=target)


def outgoing_for(lib: Library, index: Index, citekey: str) -> list[Record | Stub]:
    """Return the record's references, resolved to Records when possible, Stubs otherwise.

    Order matches the `references:` list in the source YAML. The SQLite
    outgoing table isn't ordered by insertion and a PK sort would lose author
    intent, so we walk the YAML's list directly.
    """
    if not lib.exists(citekey):
        return []
    r = lib.load(citekey)
    out: list[Record | Stub] = []
    for t in r.references:
        resolved = _resolve_target(index, t)
        if resolved and lib.exists(resolved):
            out.append(lib.load(resolved))
        else:
            out.append(_stub_for(index, t))
    return out


def incoming_for(lib: Library, index: Index, record: Record) -> list[Record]:
    """Return records that cite this one, resolved via DOI or citekey."""
    conn = index.connect()
    keys: list[str] = []
    targets: list[str] = []
    if record.doi:
        targets.append(record.doi)
    targets.append(record.citekey)
    placeholders = ", ".join("?" for _ in targets)
    for row in conn.execute(
        f"SELECT DISTINCT citekey FROM outgoing WHERE target IN ({placeholders})",
        targets,
    ):
        keys.append(row[0])
    return [lib.load(k) for k in keys if lib.exists(k)]


def unread_cited_by_read(lib: Library, index: Index,
                         min_citations: int = 2) -> list[tuple[Stub, int]]:
    """Return (stub, citation_count) pairs for external papers cited by
    at least `min_citations` records with read=true, sorted by count descending.
    """
    conn = index.connect()
    rows = conn.execute(
        "SELECT o.target, COUNT(DISTINCT o.citekey) AS n "
        "FROM outgoing o JOIN records r ON r.citekey = o.citekey "
        "WHERE r.read = 1 "
        "GROUP BY o.target "
        "HAVING n >= ? "
        "ORDER BY n DESC, o.target ASC",
        (min_citations,),
    ).fetchall()
    out: list[tuple[Stub, int]] = []
    for target, n in rows:
        resolved = _resolve_target(index, target)
        if resolved is not None:
            continue
        out.append((_stub_for(index, target), n))
    return out


def shortest_path(lib: Library, index: Index, a: str, b: str) -> Optional[list[str]]:
    """Shortest path from citekey `a` to citekey `b` over the directed citation
    graph. Returns a list of citekeys [a, ..., b] or None if no path exists."""
    import networkx as nx

    conn = index.connect()
    G = nx.DiGraph()
    # Only add edges where the target resolves to an in-library record; stubs
    # are leaves and not useful for pathfinding.
    for src, target in conn.execute("SELECT citekey, target FROM outgoing"):
        resolved = _resolve_target(index, target)
        if resolved:
            G.add_edge(src, resolved)
    try:
        return nx.shortest_path(G, source=a, target=b)
    except (nx.NodeNotFound, nx.NetworkXNoPath):
        return None
