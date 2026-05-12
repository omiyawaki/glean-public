"""HTTP API for browsing the glean library.

Designed for consumption by the native macOS frontend (glean.app).
Read-only for now: list all records, fetch one record. Search comes
later.

The list endpoint queries the SQLite index — fast (~ms for thousands
of records), independent of YAML reparse cost. A background thread
calls Index.refresh() every REFRESH_INTERVAL_S so YAML edits show up
in the API without per-request reparse latency. (refresh() is itself
the expensive thing — see PARKED.md on the dual-mtime fix.)
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

from glean.index import Index
from glean.store import Library


REFRESH_INTERVAL_S = 60


def _refresh_index(idx: Index, *, phase: str) -> bool:
    try:
        idx.refresh()
    except Exception as exc:
        print(f"glean web {phase} refresh failed: {exc}", file=sys.stderr, flush=True)
        return False
    return True


def _list_papers(idx: Index) -> dict:
    """All records for the library browse view, joined with authors."""
    conn = idx.connect()
    rows = conn.execute(
        "SELECT citekey, title, year, doi FROM records "
        "ORDER BY year DESC NULLS LAST, citekey ASC"
    ).fetchall()
    by_ck: dict[str, list[dict]] = {}
    for ck, family, given in conn.execute(
        "SELECT citekey, family, given FROM authors ORDER BY citekey, ord"
    ):
        by_ck.setdefault(ck, []).append({"family": family, "given": given or ""})
    entries = []
    for ck, title, year, doi in rows:
        entries.append({
            "citekey": ck,
            "title": title,
            "year": year,
            "doi": doi,
            "authors": by_ck.get(ck, []),
        })
    return {"count": len(entries), "entries": entries}


def _record_detail(lib: Library, citekey: str) -> dict | None:
    """Full YAML-backed record. Reads disk; called on-demand when the
    Mac client opens a row, so per-request cost is fine."""
    path = lib.path_for(citekey)
    if not path.is_file():
        return None
    record = lib.load(citekey)
    return {
        "citekey": record.citekey,
        "type": record.type,
        "title": record.title,
        "year": record.year,
        "doi": record.doi,
        "arxiv": record.arxiv,
        "isbn": record.isbn,
        "pmid": record.pmid,
        "pmcid": record.pmcid,
        "journal": record.journal,
        "volume": record.volume,
        "issue": record.issue,
        "pages": record.pages,
        "publisher": record.publisher,
        "url": record.url,
        "abstract": record.abstract,
        "keywords": list(record.keywords or []),
        "authors": [{"family": a.family, "given": a.given} for a in record.authors],
        "attachments": [{"path": a.path, "kind": a.kind} for a in record.attachments],
        "added_at": record.added_at.isoformat() if record.added_at else None,
        "modified_at": record.modified_at.isoformat() if record.modified_at else None,
        "read": bool(record.read),
        "notes": record.notes or "",
    }


def _start_refresh_thread(idx: Index) -> None:
    """Background daemon that re-reads YAML mtimes every minute so the
    API serves fresh data without paying refresh() cost on each request."""
    def loop() -> None:
        while True:
            _refresh_index(idx, phase="background")
            time.sleep(REFRESH_INTERVAL_S)
    threading.Thread(target=loop, daemon=True, name="glean-web-refresh").start()


def serve(host: str = "0.0.0.0", port: int = 8766) -> None:
    lib = Library(Path.home() / "Documents" / "glean")
    idx = Index(lib, Path.home() / "Library" / "Caches" / "glean" / "index.sqlite")
    _refresh_index(idx, phase="startup")
    _start_refresh_thread(idx)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict | list) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._json(200, {"ok": True})
            if self.path == "/api/papers":
                return self._json(200, _list_papers(idx))
            if self.path.startswith("/api/papers/"):
                citekey = self.path[len("/api/papers/"):]
                detail = _record_detail(lib, citekey)
                if detail is None:
                    return self._json(404, {"error": "not found"})
                return self._json(200, detail)
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            # Suppress per-request access logs; LaunchAgent stderr stays
            # readable for real warnings.
            return

    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with ThreadedServer((host, port), Handler) as srv:
        print(f"glean web listening on {host}:{port}", flush=True)
        srv.serve_forever()
