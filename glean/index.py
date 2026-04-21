"""SQLite FTS5 index, derived from the YAML records.

Staleness is detected by comparing each YAML's modified_at against the
stored yaml_modified column. See DESIGN.md "SQLite index schema".
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from glean.citekey import looks_like_doi
from glean.schema import Record
from glean.store import Library


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    citekey         TEXT PRIMARY KEY,
    yaml_path       TEXT NOT NULL UNIQUE,
    yaml_modified   TEXT NOT NULL,
    title           TEXT NOT NULL,
    year            INTEGER,
    type            TEXT,
    doi             TEXT,
    arxiv           TEXT,
    added_at        TEXT,
    read            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_doi ON records(doi) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_records_year ON records(year);

CREATE TABLE IF NOT EXISTS authors (
    citekey TEXT NOT NULL,
    ord     INTEGER NOT NULL,
    family  TEXT NOT NULL,
    given   TEXT,
    PRIMARY KEY (citekey, ord),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_authors_family ON authors(family);

CREATE TABLE IF NOT EXISTS keywords (
    citekey TEXT NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (citekey, keyword),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attachments (
    citekey TEXT NOT NULL,
    ord     INTEGER NOT NULL,
    path    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (citekey, ord),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attachments_kind ON attachments(citekey, kind);

CREATE TABLE IF NOT EXISTS outgoing (
    citekey TEXT NOT NULL,
    target  TEXT NOT NULL,
    PRIMARY KEY (citekey, target),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outgoing_target ON outgoing(target);

CREATE TABLE IF NOT EXISTS stubs (
    doi          TEXT UNIQUE,
    citekey      TEXT UNIQUE,
    title        TEXT,
    year         INTEGER,
    authors_flat TEXT,
    CHECK (doi IS NOT NULL OR citekey IS NOT NULL)
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    citekey UNINDEXED,
    title,
    abstract,
    keywords,
    authors_flat,
    tokenize='porter unicode61'
);
"""


def _authors_flat(record: Record) -> str:
    parts = []
    for a in record.authors:
        if a.given:
            parts.append(f"{a.family} {a.given}")
        else:
            parts.append(a.family)
    return "; ".join(parts)


class Index:
    def __init__(self, library: Library, db_path: Path | str):
        self.library = library
        self.db_path = Path(db_path).expanduser()
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def rebuild(self) -> None:
        conn = self.connect()
        with conn:
            for table in ("records", "authors", "keywords", "attachments", "outgoing", "stubs", "fts"):
                conn.execute(f"DELETE FROM {table}")
        for record in self.library.iter_records():
            self._upsert(record)
        self._sync_stubs()

    def refresh(self) -> tuple[int, int, int]:
        """Return (added, updated, deleted)."""
        conn = self.connect()
        known = {
            row[0]: row[1]
            for row in conn.execute("SELECT citekey, yaml_modified FROM records")
        }
        seen: set[str] = set()
        added = updated = 0
        for record in self.library.iter_records():
            seen.add(record.citekey)
            mod_str = record.modified_at.isoformat() if record.modified_at else ""
            if record.citekey not in known:
                self._upsert(record)
                added += 1
            elif known[record.citekey] != mod_str:
                self._upsert(record)
                updated += 1
        deleted = 0
        for citekey in known.keys() - seen:
            self._delete(citekey)
            deleted += 1
        self._sync_stubs()
        return added, updated, deleted

    def _delete(self, citekey: str) -> None:
        conn = self.connect()
        with conn:
            conn.execute("DELETE FROM records WHERE citekey = ?", (citekey,))
            conn.execute("DELETE FROM fts WHERE citekey = ?", (citekey,))

    def _upsert(self, record: Record) -> None:
        conn = self.connect()
        with conn:
            # Remove any prior state, then re-insert.
            conn.execute("DELETE FROM records WHERE citekey = ?", (record.citekey,))
            conn.execute("DELETE FROM fts WHERE citekey = ?", (record.citekey,))

            conn.execute(
                "INSERT INTO records(citekey, yaml_path, yaml_modified, title, year, "
                "type, doi, arxiv, added_at, read) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.citekey,
                    str(self.library.path_for(record.citekey)),
                    record.modified_at.isoformat() if record.modified_at else "",
                    record.title,
                    record.year,
                    record.type,
                    record.doi,
                    record.arxiv,
                    record.added_at.isoformat() if record.added_at else None,
                    1 if record.read else 0,
                ),
            )
            conn.executemany(
                "INSERT INTO authors(citekey, ord, family, given) VALUES (?, ?, ?, ?)",
                [
                    (record.citekey, i, a.family, a.given)
                    for i, a in enumerate(record.authors)
                ],
            )
            conn.executemany(
                "INSERT INTO keywords(citekey, keyword) VALUES (?, ?)",
                [(record.citekey, kw) for kw in record.keywords],
            )
            conn.executemany(
                "INSERT INTO attachments(citekey, ord, path, kind) VALUES (?, ?, ?, ?)",
                [
                    (record.citekey, i, a.path, a.kind)
                    for i, a in enumerate(record.attachments)
                ],
            )
            conn.executemany(
                "INSERT INTO outgoing(citekey, target) VALUES (?, ?)",
                [(record.citekey, t) for t in record.references],
            )
            conn.execute(
                "INSERT INTO fts(citekey, title, abstract, keywords, authors_flat) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.citekey,
                    record.title,
                    record.abstract or "",
                    " ".join(record.keywords),
                    _authors_flat(record),
                ),
            )

    def _sync_stubs(self) -> None:
        """Ensure every outgoing target that isn't a known record has a stub row."""
        conn = self.connect()
        with conn:
            conn.execute(
                "DELETE FROM stubs WHERE "
                "(doi IS NOT NULL AND doi IN (SELECT doi FROM records WHERE doi IS NOT NULL)) "
                "OR (citekey IS NOT NULL AND citekey IN (SELECT citekey FROM records))"
            )
            targets = [row[0] for row in conn.execute("SELECT DISTINCT target FROM outgoing")]
            known_dois = {row[0] for row in conn.execute("SELECT doi FROM records WHERE doi IS NOT NULL")}
            known_cks = {row[0] for row in conn.execute("SELECT citekey FROM records")}
            for t in targets:
                if looks_like_doi(t):
                    if t in known_dois:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO stubs(doi) VALUES (?)", (t,)
                    )
                else:
                    if t in known_cks:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO stubs(citekey) VALUES (?)", (t,)
                    )

    def search(self, query: str, limit: int = 50) -> list[tuple[str, str, str, Optional[int]]]:
        """Return (citekey, title, authors_flat, year) rows ranked by FTS5."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT f.citekey, f.title, f.authors_flat, r.year "
            "FROM fts f JOIN records r ON r.citekey = f.citekey "
            "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return list(rows)

    def outgoing(self, citekey: str) -> list[tuple[str, Optional[str]]]:
        """Return [(target, resolved_citekey | None)] for the record's references."""
        conn = self.connect()
        targets = [row[0] for row in conn.execute(
            "SELECT target FROM outgoing WHERE citekey = ?", (citekey,)
        )]
        resolved: list[tuple[str, Optional[str]]] = []
        for t in targets:
            if looks_like_doi(t):
                row = conn.execute(
                    "SELECT citekey FROM records WHERE doi = ?", (t,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT citekey FROM records WHERE citekey = ?", (t,)
                ).fetchone()
            resolved.append((t, row[0] if row else None))
        return resolved

    def incoming(self, record: Record) -> list[str]:
        """Return citekeys of records that cite this one (by DOI or by citekey)."""
        conn = self.connect()
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
        return keys
