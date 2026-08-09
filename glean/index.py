"""SQLite FTS5 index, derived from the YAML records.

Staleness is detected by comparing each YAML's modified_at against the
stored yaml_modified column. See DESIGN.md "SQLite index schema".
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from glean.schema import Record, ReferenceID
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
    isbn            TEXT,
    pmid            TEXT,
    pmcid           TEXT,
    legacy_citekey  TEXT,
    added_at        TEXT,
    read            INTEGER NOT NULL DEFAULT 0,
    reference_status TEXT,
    reference_providers TEXT,
    reference_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_doi ON records(doi) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_records_year ON records(year);
CREATE INDEX IF NOT EXISTS idx_records_legacy_citekey
    ON records(legacy_citekey) WHERE legacy_citekey IS NOT NULL;

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

CREATE TABLE IF NOT EXISTS tags (
    citekey TEXT NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (citekey, tag),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

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

CREATE TABLE IF NOT EXISTS stubs (
    doi          TEXT UNIQUE,
    citekey      TEXT UNIQUE,
    title        TEXT,
    year         INTEGER,
    authors_flat TEXT,
    CHECK (doi IS NOT NULL OR citekey IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS works (
    work_id              INTEGER PRIMARY KEY,
    title                TEXT,
    year                 INTEGER,
    authors_flat         TEXT,
    metadata_source      TEXT,
    metadata_fetched_at  TEXT,
    hydration_state      TEXT,
    hydration_attempts   INTEGER NOT NULL DEFAULT 0,
    hydration_next_at    TEXT,
    hydration_error      TEXT,
    hydration_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS work_aliases (
    kind              TEXT NOT NULL,
    value             TEXT NOT NULL,
    normalized_value  TEXT NOT NULL,
    work_id           INTEGER NOT NULL,
    PRIMARY KEY (kind, normalized_value),
    FOREIGN KEY (work_id) REFERENCES works(work_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_work_aliases_work ON work_aliases(work_id);

CREATE TABLE IF NOT EXISTS library_works (
    citekey  TEXT PRIMARY KEY,
    work_id  INTEGER NOT NULL UNIQUE,
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE,
    FOREIGN KEY (work_id) REFERENCES works(work_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS citation_edges (
    citekey         TEXT NOT NULL,
    ord             INTEGER NOT NULL,
    target_work_id  INTEGER NOT NULL,
    target_id       TEXT NOT NULL,
    PRIMARY KEY (citekey, ord),
    UNIQUE (citekey, target_work_id),
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE,
    FOREIGN KEY (target_work_id) REFERENCES works(work_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_citation_edges_target
    ON citation_edges(target_work_id);

CREATE TABLE IF NOT EXISTS graph_jobs (
    citekey          TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 10,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT,
    updated_at       TEXT NOT NULL,
    FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_graph_jobs_ready
    ON graph_jobs(state, next_attempt_at, priority DESC, updated_at);

CREATE TABLE IF NOT EXISTS graph_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
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

SCHEMA_VERSION = 2
GRAPH_RETRY_DELAYS_SECONDS = (300, 1_800, 7_200, 43_200, 86_400)


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
            # check_same_thread=False so TUI background workers can read this
            # connection. Writes happen on the main thread via the usual
            # transactional helpers; concurrent access is read-dominant.
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)
            needs_rebuild = self._migrate_schema(self._conn)
            if needs_rebuild:
                self.rebuild()
        return self._conn

    def _migrate_schema(self, conn: sqlite3.Connection) -> bool:
        previous_version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
        additions = {
            "reference_status": "TEXT",
            "reference_providers": "TEXT",
            "reference_fetched_at": "TEXT",
        }
        with conn:
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE records ADD COLUMN {name} {declaration}")
            work_columns = {row[1] for row in conn.execute("PRAGMA table_info(works)")}
            work_additions = {
                "hydration_state": "TEXT",
                "hydration_attempts": "INTEGER NOT NULL DEFAULT 0",
                "hydration_next_at": "TEXT",
                "hydration_error": "TEXT",
                "hydration_updated_at": "TEXT",
            }
            for name, declaration in work_additions.items():
                if name not in work_columns:
                    conn.execute(f"ALTER TABLE works ADD COLUMN {name} {declaration}")
            for name in ("outgoing", "stubs"):
                row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = ?",
                    (name,),
                ).fetchone()
                if row and row[0] == "table":
                    conn.execute(f"DROP TABLE {name}")
            conn.executescript(
                """
                CREATE VIEW IF NOT EXISTS outgoing AS
                SELECT e.citekey,
                       CASE
                         WHEN e.target_id LIKE 'doi:%' THEN substr(e.target_id, 5)
                         WHEN e.target_id LIKE 'citekey:%' THEN substr(e.target_id, 9)
                         ELSE e.target_id
                       END AS target
                FROM citation_edges e;

                CREATE VIEW IF NOT EXISTS stubs AS
                SELECT
                  (SELECT value FROM work_aliases da
                   WHERE da.work_id = w.work_id AND da.kind = 'doi' LIMIT 1) AS doi,
                  COALESCE(
                    (SELECT value FROM work_aliases ca
                     WHERE ca.work_id = w.work_id AND ca.kind = 'citekey' LIMIT 1),
                    CASE WHEN NOT EXISTS (
                      SELECT 1 FROM work_aliases da
                      WHERE da.work_id = w.work_id AND da.kind = 'doi'
                    ) THEN (
                      SELECT 'openalex:' || value FROM work_aliases oa
                      WHERE oa.work_id = w.work_id AND oa.kind = 'openalex' LIMIT 1
                    ) END
                  ) AS citekey,
                  w.title,
                  w.year,
                  w.authors_flat
                FROM works w
                LEFT JOIN library_works lw ON lw.work_id = w.work_id
                WHERE lw.work_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM citation_edges e WHERE e.target_work_id = w.work_id
                  );
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return previous_version < SCHEMA_VERSION

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def rebuild(self) -> None:
        records, unavailable = self.library.scan_records()
        if unavailable:
            raise RuntimeError(
                "refusing to rebuild an incomplete index while "
                f"{len(unavailable)} library record(s) are unavailable"
            )
        conn = self.connect()
        with conn:
            for table in (
                "citation_edges", "library_works", "work_aliases", "works",
                "records", "authors", "keywords", "tags", "attachments",
                "fts",
            ):
                conn.execute(f"DELETE FROM {table}")
        for record in records:
            self._upsert(record)
        self._sync_stubs()

    def refresh(self) -> tuple[int, int, int]:
        """Return (added, updated, deleted)."""
        conn = self.connect()
        known = {
            row[0]: row[1]
            for row in conn.execute("SELECT citekey, yaml_modified FROM records")
        }
        records, unavailable = self.library.scan_records()
        seen: set[str] = set(unavailable)
        added = updated = 0
        for record in records:
            seen.add(record.citekey)
            mod_str = record.modified_at.isoformat() if record.modified_at else ""
            if record.citekey not in known:
                self._upsert(record)
                added += 1
            elif known[record.citekey] != mod_str:
                self._upsert(record)
                updated += 1
        if known and not seen:
            raise RuntimeError(
                "refusing to delete the entire index after the library scan "
                "returned no records"
            )
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
            preserved_job = conn.execute(
                "SELECT state, priority, attempts, next_attempt_at, last_error, updated_at "
                "FROM graph_jobs WHERE citekey = ?",
                (record.citekey,),
            ).fetchone()
            # Remove any prior state, then re-insert.
            conn.execute("DELETE FROM records WHERE citekey = ?", (record.citekey,))
            conn.execute("DELETE FROM fts WHERE citekey = ?", (record.citekey,))

            conn.execute(
                "INSERT INTO records(citekey, yaml_path, yaml_modified, title, year, "
                "type, doi, arxiv, isbn, pmid, pmcid, legacy_citekey, added_at, read, "
                "reference_status, reference_providers, reference_fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.citekey,
                    str(self.library.path_for(record.citekey)),
                    record.modified_at.isoformat() if record.modified_at else "",
                    record.title,
                    record.year,
                    record.type,
                    record.doi,
                    record.arxiv,
                    record.isbn,
                    record.pmid,
                    record.pmcid,
                    record.legacy_citekey,
                    record.added_at.isoformat() if record.added_at else None,
                    1 if record.read else 0,
                    record.reference_graph.status if record.reference_graph else None,
                    ",".join(record.reference_graph.providers)
                    if record.reference_graph else None,
                    record.reference_graph.fetched_at.isoformat()
                    if record.reference_graph else None,
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
                "INSERT INTO tags(citekey, tag) VALUES (?, ?)",
                [(record.citekey, tag) for tag in record.tags],
            )
            conn.executemany(
                "INSERT INTO attachments(citekey, ord, path, kind) VALUES (?, ?, ?, ?)",
                [
                    (record.citekey, i, a.path, a.kind)
                    for i, a in enumerate(record.attachments)
                ],
            )
            source_work_id = self._ensure_library_work(conn, record)
            for ordinal, reference in enumerate(record.references):
                target_work_id = self._ensure_work_for_reference(conn, reference)
                conn.execute(
                    "INSERT OR IGNORE INTO citation_edges"
                    "(citekey, ord, target_work_id, target_id) VALUES (?, ?, ?, ?)",
                    (record.citekey, ordinal, target_work_id, reference.serialized),
                )
            self._update_work_metadata(
                conn,
                source_work_id,
                title=record.title,
                year=record.year,
                authors_flat=_authors_flat(record),
                metadata_source=record.source or "library",
                metadata_fetched_at=(
                    record.source_fetched_at.isoformat()
                    if record.source_fetched_at else None
                ),
            )
            conn.execute(
                "INSERT INTO fts(citekey, title, abstract, keywords, authors_flat) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.citekey,
                    record.title,
                    record.abstract or "",
                    " ".join(record.keywords + record.tags),
                    _authors_flat(record),
                ),
            )
            if preserved_job is not None and record.reference_graph is None:
                conn.execute(
                    "INSERT INTO graph_jobs"
                    "(citekey, state, priority, attempts, next_attempt_at, last_error, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record.citekey, *preserved_job),
                )

    def _alias_work_id(self, conn: sqlite3.Connection, reference: ReferenceID) -> Optional[int]:
        row = conn.execute(
            "SELECT work_id FROM work_aliases WHERE kind = ? AND normalized_value = ?",
            (reference.kind, reference.normalized_value),
        ).fetchone()
        return row[0] if row else None

    def _ensure_work_for_reference(
        self,
        conn: sqlite3.Connection,
        reference: ReferenceID,
    ) -> int:
        existing = self._alias_work_id(conn, reference)
        if existing is not None:
            return existing
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "INSERT INTO works(hydration_state, hydration_updated_at) "
            "VALUES ('queued', ?)",
            (timestamp,),
        )
        work_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO work_aliases(kind, value, normalized_value, work_id) "
            "VALUES (?, ?, ?, ?)",
            (reference.kind, reference.value, reference.normalized_value, work_id),
        )
        return work_id

    def _ensure_library_work(self, conn: sqlite3.Connection, record: Record) -> int:
        aliases = [ReferenceID("citekey", record.citekey)]
        if record.doi:
            aliases.append(ReferenceID("doi", record.doi))
        work_ids = {
            work_id
            for alias in aliases
            if (work_id := self._alias_work_id(conn, alias)) is not None
        }
        if work_ids:
            work_id = min(work_ids)
            for duplicate in sorted(work_ids - {work_id}):
                self._merge_works(conn, work_id, duplicate)
        else:
            work_id = self._ensure_work_for_reference(conn, aliases[0])
        for alias in aliases:
            existing = self._alias_work_id(conn, alias)
            if existing is None:
                conn.execute(
                    "INSERT INTO work_aliases(kind, value, normalized_value, work_id) "
                    "VALUES (?, ?, ?, ?)",
                    (alias.kind, alias.value, alias.normalized_value, work_id),
                )
            elif existing != work_id:
                self._merge_works(conn, work_id, existing)
        conn.execute(
            "DELETE FROM library_works WHERE work_id = ? AND citekey <> ?",
            (work_id, record.citekey),
        )
        conn.execute(
            "INSERT INTO library_works(citekey, work_id) VALUES (?, ?) "
            "ON CONFLICT(citekey) DO UPDATE SET work_id = excluded.work_id",
            (record.citekey, work_id),
        )
        return work_id

    def _merge_works(
        self,
        conn: sqlite3.Connection,
        keep_work_id: int,
        duplicate_work_id: int,
    ) -> None:
        if keep_work_id == duplicate_work_id:
            return
        for citekey, ordinal, target_id in conn.execute(
            "SELECT citekey, ord, target_id FROM citation_edges "
            "WHERE target_work_id = ?",
            (duplicate_work_id,),
        ).fetchall():
            existing = conn.execute(
                "SELECT 1 FROM citation_edges WHERE citekey = ? AND target_work_id = ?",
                (citekey, keep_work_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM citation_edges WHERE citekey = ? AND ord = ?",
                    (citekey, ordinal),
                )
            else:
                conn.execute(
                    "UPDATE citation_edges SET target_work_id = ?, target_id = ? "
                    "WHERE citekey = ? AND ord = ?",
                    (keep_work_id, target_id, citekey, ordinal),
                )
        conn.execute(
            "UPDATE work_aliases SET work_id = ? WHERE work_id = ?",
            (keep_work_id, duplicate_work_id),
        )
        conn.execute(
            "UPDATE OR IGNORE library_works SET work_id = ? WHERE work_id = ?",
            (keep_work_id, duplicate_work_id),
        )
        duplicate = conn.execute(
            "SELECT title, year, authors_flat, metadata_source, metadata_fetched_at "
            "FROM works WHERE work_id = ?",
            (duplicate_work_id,),
        ).fetchone()
        if duplicate:
            self._update_work_metadata(
                conn,
                keep_work_id,
                title=duplicate[0],
                year=duplicate[1],
                authors_flat=duplicate[2],
                metadata_source=duplicate[3],
                metadata_fetched_at=duplicate[4],
                overwrite=False,
            )
        conn.execute("DELETE FROM works WHERE work_id = ?", (duplicate_work_id,))

    def _update_work_metadata(
        self,
        conn: sqlite3.Connection,
        work_id: int,
        *,
        title: Optional[str],
        year: Optional[int],
        authors_flat: Optional[str],
        metadata_source: Optional[str],
        metadata_fetched_at: Optional[str],
        overwrite: bool = True,
    ) -> None:
        if overwrite:
            conn.execute(
                "UPDATE works SET title = ?, year = ?, authors_flat = ?, "
                "metadata_source = ?, metadata_fetched_at = ? WHERE work_id = ?",
                (
                    title, year, authors_flat, metadata_source,
                    metadata_fetched_at, work_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE works SET title = COALESCE(title, ?), year = COALESCE(year, ?), "
                "authors_flat = COALESCE(authors_flat, ?), "
                "metadata_source = COALESCE(metadata_source, ?), "
                "metadata_fetched_at = COALESCE(metadata_fetched_at, ?) "
                "WHERE work_id = ?",
                (
                    title, year, authors_flat, metadata_source,
                    metadata_fetched_at, work_id,
                ),
            )
        if title:
            conn.execute(
                "UPDATE works SET hydration_state = 'complete', hydration_attempts = 0, "
                "hydration_next_at = NULL, hydration_error = NULL, hydration_updated_at = ? "
                "WHERE work_id = ?",
                (datetime.now(timezone.utc).isoformat(), work_id),
            )

    def _sync_stubs(self) -> None:
        """Compatibility stubs are a read-only view of normalized works."""
        return

    @staticmethod
    def _match_query(query: str) -> str:
        """Build a safe FTS5 MATCH string from free-text input.

        Each whitespace-delimited token is wrapped as a quoted phrase so FTS5
        query operators in user input (-, :, *, ^, parentheses, AND/OR/NOT, and
        bare double quotes) match literally instead of being parsed as syntax.
        Without this, a query like "well-mixed" is read as a column filter and
        aborts with 'no such column: mixed'."""
        tokens = query.split()
        return " ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)

    def search(self, query: str, limit: int = 50) -> list[tuple[str, str, str, Optional[int]]]:
        """Return (citekey, title, authors_flat, year) rows ranked by FTS5."""
        match = self._match_query(query)
        if not match:
            return []
        conn = self.connect()
        rows = conn.execute(
            "SELECT f.citekey, f.title, f.authors_flat, r.year "
            "FROM fts f JOIN records r ON r.citekey = f.citekey "
            "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
        return list(rows)

    def outgoing(self, citekey: str) -> list[tuple[str, Optional[str]]]:
        """Return [(target, resolved_citekey | None)] for the record's references."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT e.target_id, lw.citekey "
            "FROM citation_edges e "
            "LEFT JOIN library_works lw ON lw.work_id = e.target_work_id "
            "WHERE e.citekey = ? ORDER BY e.ord",
            (citekey,),
        ).fetchall()
        return [(ReferenceID.parse(target).legacy_value, resolved) for target, resolved in rows]

    def incoming(self, record: Record) -> list[str]:
        """Return citekeys of records that cite this one (by DOI or by citekey)."""
        conn = self.connect()
        work_ids: set[int] = set()
        if record.doi:
            work_id = self._alias_work_id(conn, ReferenceID("doi", record.doi))
            if work_id is not None:
                work_ids.add(work_id)
        work_id = self._alias_work_id(conn, ReferenceID("citekey", record.citekey))
        if work_id is not None:
            work_ids.add(work_id)
        if not work_ids:
            return []
        placeholders = ", ".join("?" for _ in work_ids)
        return [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT citekey FROM citation_edges "
                f"WHERE target_work_id IN ({placeholders})",
                tuple(work_ids),
            )
        ]

    def resolve_reference(self, reference: ReferenceID) -> Optional[str]:
        conn = self.connect()
        work_id = self._alias_work_id(conn, reference)
        if work_id is None:
            return None
        row = conn.execute(
            "SELECT citekey FROM library_works WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        return row[0] if row else None

    def reference_metadata(
        self,
        reference: ReferenceID,
    ) -> tuple[Optional[str], Optional[int], Optional[str]]:
        conn = self.connect()
        work_id = self._alias_work_id(conn, reference)
        if work_id is None:
            return (None, None, None)
        row = conn.execute(
            "SELECT title, year, authors_flat FROM works WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        return tuple(row) if row else (None, None, None)

    def upsert_work_metadata(
        self,
        metadata: dict,
        *,
        source: str,
        fetched_at: str,
        overwrite: bool = True,
    ) -> Optional[int]:
        aliases: list[ReferenceID] = []
        if metadata.get("doi"):
            aliases.append(ReferenceID("doi", str(metadata["doi"])))
        if metadata.get("openalex_id"):
            aliases.append(ReferenceID("openalex", str(metadata["openalex_id"])))
        if not aliases:
            return None
        conn = self.connect()
        with conn:
            work_ids = {
                work_id
                for alias in aliases
                if (work_id := self._alias_work_id(conn, alias)) is not None
            }
            if work_ids:
                work_id = min(work_ids)
                for duplicate in sorted(work_ids - {work_id}):
                    self._merge_works(conn, work_id, duplicate)
            else:
                work_id = self._ensure_work_for_reference(conn, aliases[0])
            for alias in aliases:
                existing = self._alias_work_id(conn, alias)
                if existing is None:
                    conn.execute(
                        "INSERT INTO work_aliases(kind, value, normalized_value, work_id) "
                        "VALUES (?, ?, ?, ?)",
                        (alias.kind, alias.value, alias.normalized_value, work_id),
                    )
                elif existing != work_id:
                    self._merge_works(conn, work_id, existing)
            authors = metadata.get("authors") or []
            authors_flat = "; ".join(
                " ".join(
                    part for part in (
                        str(author.get("family") or "").strip(),
                        str(author.get("given") or "").strip(),
                    ) if part
                )
                for author in authors
                if author.get("family") or author.get("given")
            ) or None
            self._update_work_metadata(
                conn,
                work_id,
                title=str(metadata.get("title") or "").strip() or None,
                year=metadata.get("year"),
                authors_flat=authors_flat,
                metadata_source=source,
                metadata_fetched_at=fetched_at,
                overwrite=overwrite,
            )
            return work_id

    def reference_entries(self, citekey: str) -> list[dict]:
        """Ordered outgoing relations with normalized library/external metadata."""
        rows = self.connect().execute(
            "SELECT e.target_id, lw.citekey, "
            "COALESCE(lw.citekey, (SELECT value FROM work_aliases ca "
            " WHERE ca.work_id = w.work_id AND ca.kind = 'citekey' LIMIT 1)), "
            "w.title, w.year, w.authors_flat, "
            "(SELECT value FROM work_aliases da WHERE da.work_id = w.work_id "
            " AND da.kind = 'doi' LIMIT 1), "
            "(SELECT value FROM work_aliases oa WHERE oa.work_id = w.work_id "
            " AND oa.kind = 'openalex' LIMIT 1) "
            "FROM citation_edges e JOIN works w ON w.work_id = e.target_work_id "
            "LEFT JOIN library_works lw ON lw.work_id = w.work_id "
            "WHERE e.citekey = ? ORDER BY e.ord",
            (citekey,),
        ).fetchall()
        return [
            {
                "target_id": row[0],
                "citekey": row[2],
                "title": row[3],
                "year": row[4],
                "doi": row[6],
                "openalex_id": row[7],
                "authors_flat": row[5],
                "in_library": row[1] is not None,
            }
            for row in rows
        ]

    def cited_by_entries(self, citekey: str) -> list[dict]:
        """Library papers that cite *citekey*, ordered newest first."""
        rows = self.connect().execute(
            "SELECT r.citekey, r.title, r.year, r.doi, w.authors_flat, "
            "(SELECT value FROM work_aliases oa WHERE oa.work_id = lw.work_id "
            " AND oa.kind = 'openalex' LIMIT 1) "
            "FROM library_works target "
            "JOIN citation_edges e ON e.target_work_id = target.work_id "
            "JOIN records r ON r.citekey = e.citekey "
            "JOIN library_works lw ON lw.citekey = r.citekey "
            "JOIN works w ON w.work_id = lw.work_id "
            "WHERE target.citekey = ? "
            "ORDER BY (r.year IS NULL), r.year DESC, r.title, r.citekey",
            (citekey,),
        ).fetchall()
        return [
            {
                "target_id": f"doi:{row[3]}" if row[3] else f"citekey:{row[0]}",
                "citekey": row[0],
                "title": row[1],
                "year": row[2],
                "doi": row[3],
                "openalex_id": row[5],
                "authors_flat": row[4],
                "in_library": True,
            }
            for row in rows
        ]

    def graph_relation_state(self, citekey: str) -> Optional[dict]:
        row = self.connect().execute(
            "SELECT title, year, doi, reference_status, reference_providers, "
            "reference_fetched_at FROM records WHERE citekey = ?",
            (citekey,),
        ).fetchone()
        if row is None:
            return None
        job = self.graph_job(citekey)
        if row[3] in ("complete", "empty"):
            state = row[3]
        elif not row[2]:
            state = "unsupported"
        elif job is not None:
            state = "retrying" if job["state"] == "retry_wait" else job["state"]
        else:
            state = "queued"
        return {
            "source": {
                "citekey": citekey,
                "title": row[0],
                "year": row[1],
                "doi": row[2],
            },
            "graph": {
                "state": state,
                "providers": [value for value in (row[4] or "").split(",") if value],
                "fetched_at": row[5],
                "attempts": job["attempts"] if job else 0,
                "next_attempt_at": job["next_attempt_at"] if job else None,
                "last_error": job["last_error"] if job else None,
            },
        }

    def claim_work_hydration_batch(
        self,
        *,
        now: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[ReferenceID]:
        now = now or datetime.now(timezone.utc)
        conn = self.connect()
        with conn:
            conn.execute(
                "UPDATE works SET hydration_state = 'queued', hydration_attempts = 0, "
                "hydration_next_at = NULL, hydration_error = NULL, hydration_updated_at = ? "
                "WHERE hydration_state = 'running' AND hydration_updated_at < ?",
                (now.isoformat(), (now - timedelta(minutes=20)).isoformat()),
            )
            conn.execute(
                "UPDATE works SET hydration_state = 'queued', hydration_attempts = 0, "
                "hydration_next_at = NULL, hydration_error = NULL, hydration_updated_at = ? "
                "WHERE hydration_state = 'failed' AND hydration_updated_at < ?",
                (now.isoformat(), (now - timedelta(days=7)).isoformat()),
            )
            rows = conn.execute(
                "SELECT w.work_id, "
                "COALESCE((SELECT 'openalex:' || value FROM work_aliases oa "
                " WHERE oa.work_id = w.work_id AND oa.kind = 'openalex' LIMIT 1), "
                "(SELECT 'doi:' || value FROM work_aliases da "
                " WHERE da.work_id = w.work_id AND da.kind = 'doi' LIMIT 1)) "
                "FROM works w LEFT JOIN library_works lw ON lw.work_id = w.work_id "
                "WHERE lw.work_id IS NULL AND w.title IS NULL AND "
                "(w.hydration_state IS NULL OR w.hydration_state = 'queued' OR "
                " (w.hydration_state = 'retry_wait' AND w.hydration_next_at <= ?)) "
                "ORDER BY w.work_id LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
            claimed = [(work_id, value) for work_id, value in rows if value]
            conn.executemany(
                "UPDATE works SET hydration_state = 'running', hydration_updated_at = ? "
                "WHERE work_id = ?",
                [(now.isoformat(), work_id) for work_id, _ in claimed],
            )
        return [ReferenceID.parse(value) for _, value in claimed]

    def finish_work_hydration(
        self,
        reference: ReferenceID,
        *,
        error: str = "metadata unavailable",
        now: Optional[datetime] = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        conn = self.connect()
        work_id = self._alias_work_id(conn, reference)
        if work_id is None:
            return True
        row = conn.execute(
            "SELECT title, hydration_attempts FROM works WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        if row is None or row[0]:
            return True
        attempts = (row[1] or 0) + 1
        if attempts <= len(GRAPH_RETRY_DELAYS_SECONDS):
            state = "retry_wait"
            next_at = now + timedelta(seconds=GRAPH_RETRY_DELAYS_SECONDS[attempts - 1])
        else:
            state = "failed"
            next_at = None
        with conn:
            conn.execute(
                "UPDATE works SET hydration_state = ?, hydration_attempts = ?, "
                "hydration_next_at = ?, hydration_error = ?, hydration_updated_at = ? "
                "WHERE work_id = ?",
                (
                    state, attempts, next_at.isoformat() if next_at else None,
                    error[:2_000], now.isoformat(), work_id,
                ),
            )
        return False

    def reconcile_graph_jobs(self, *, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        conn = self.connect()
        with conn:
            conn.execute(
                "DELETE FROM graph_jobs WHERE citekey IN "
                "(SELECT citekey FROM records WHERE reference_status IN ('complete', 'empty'))"
            )
            conn.execute(
                "UPDATE graph_jobs SET state = 'queued', attempts = 0, "
                "next_attempt_at = NULL, last_error = NULL, updated_at = ? "
                "WHERE state = 'running' AND updated_at < ?",
                (timestamp, (now - timedelta(minutes=20)).isoformat()),
            )
            conn.execute(
                "UPDATE graph_jobs SET state = 'queued', attempts = 0, "
                "next_attempt_at = NULL, last_error = NULL, updated_at = ? "
                "WHERE state = 'failed' AND updated_at < ?",
                (timestamp, (now - timedelta(days=7)).isoformat()),
            )
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO graph_jobs"
                "(citekey, state, priority, attempts, updated_at) "
                "SELECT citekey, 'queued', 10, 0, ? FROM records "
                "WHERE doi IS NOT NULL AND reference_status IS NULL",
                (timestamp,),
            )
            return conn.total_changes - before

    def enqueue_graph_job(
        self,
        citekey: str,
        *,
        priority: int = 100,
        now: Optional[datetime] = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        conn = self.connect()
        row = conn.execute(
            "SELECT doi FROM records WHERE citekey = ?",
            (citekey,),
        ).fetchone()
        if row is None or not row[0]:
            return False
        with conn:
            conn.execute(
                "INSERT INTO graph_jobs"
                "(citekey, state, priority, attempts, next_attempt_at, last_error, updated_at) "
                "VALUES (?, 'queued', ?, 0, NULL, NULL, ?) "
                "ON CONFLICT(citekey) DO UPDATE SET "
                "state = 'queued', priority = MAX(priority, excluded.priority), "
                "attempts = 0, next_attempt_at = NULL, last_error = NULL, "
                "updated_at = excluded.updated_at",
                (citekey, priority, now.isoformat()),
            )
        return True

    def claim_graph_job(self, *, now: Optional[datetime] = None) -> Optional[str]:
        now = now or datetime.now(timezone.utc)
        conn = self.connect()
        with conn:
            row = conn.execute(
                "SELECT j.citekey FROM graph_jobs j "
                "JOIN records r ON r.citekey = j.citekey "
                "WHERE j.state = 'queued' "
                "OR (j.state = 'retry_wait' AND j.next_attempt_at <= ?) "
                "ORDER BY j.priority DESC, r.added_at DESC, j.updated_at ASC LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            citekey = row[0]
            conn.execute(
                "UPDATE graph_jobs SET state = 'running', updated_at = ? "
                "WHERE citekey = ?",
                (now.isoformat(), citekey),
            )
            return citekey

    def finish_graph_job(self, citekey: str) -> None:
        conn = self.connect()
        with conn:
            conn.execute("DELETE FROM graph_jobs WHERE citekey = ?", (citekey,))

    def fail_graph_job(
        self,
        citekey: str,
        error: str,
        *,
        now: Optional[datetime] = None,
    ) -> str:
        now = now or datetime.now(timezone.utc)
        conn = self.connect()
        row = conn.execute(
            "SELECT attempts FROM graph_jobs WHERE citekey = ?",
            (citekey,),
        ).fetchone()
        attempts = (row[0] if row else 0) + 1
        if attempts <= len(GRAPH_RETRY_DELAYS_SECONDS):
            state = "retry_wait"
            next_attempt = now + timedelta(
                seconds=GRAPH_RETRY_DELAYS_SECONDS[attempts - 1]
            )
        else:
            state = "failed"
            next_attempt = None
        with conn:
            conn.execute(
                "UPDATE graph_jobs SET state = ?, attempts = ?, next_attempt_at = ?, "
                "last_error = ?, updated_at = ? WHERE citekey = ?",
                (
                    state,
                    attempts,
                    next_attempt.isoformat() if next_attempt else None,
                    error[:2_000],
                    now.isoformat(),
                    citekey,
                ),
            )
        return state

    def graph_job(self, citekey: str) -> Optional[dict]:
        row = self.connect().execute(
            "SELECT state, attempts, next_attempt_at, last_error, updated_at "
            "FROM graph_jobs WHERE citekey = ?",
            (citekey,),
        ).fetchone()
        if row is None:
            return None
        return {
            "state": row[0],
            "attempts": row[1],
            "next_attempt_at": row[2],
            "last_error": row[3],
            "updated_at": row[4],
        }

    def graph_status(self) -> dict:
        conn = self.connect()
        total, eligible, complete, empty = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN doi IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN reference_status = 'complete' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN reference_status = 'empty' THEN 1 ELSE 0 END) "
            "FROM records"
        ).fetchone()
        jobs = {
            state: count
            for state, count in conn.execute(
                "SELECT state, COUNT(*) FROM graph_jobs GROUP BY state"
            )
        }
        return {
            "total": total or 0,
            "eligible": eligible or 0,
            "complete": complete or 0,
            "empty": empty or 0,
            "unsupported": (total or 0) - (eligible or 0),
            "queued": jobs.get("queued", 0),
            "running": jobs.get("running", 0),
            "retrying": jobs.get("retry_wait", 0),
            "failed": jobs.get("failed", 0),
            "paused": self.graph_is_paused(),
        }

    def set_graph_paused(self, paused: bool) -> None:
        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO graph_settings(key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",),
            )

    def graph_is_paused(self) -> bool:
        row = self.connect().execute(
            "SELECT value FROM graph_settings WHERE key = 'paused'"
        ).fetchone()
        return bool(row and row[0] == "1")
