"""HTTP API for browsing the glean library.

Designed for consumption by the native macOS frontend (glean.app).
The paper list and details are read-only except for the attachment
endpoints the Mac uses to record an iCloud PDF path and to pull a PDF
that was fetched on the server. Citation relations expose one idempotent
retry mutation for failed background enrichment jobs.

The list endpoint queries the SQLite index — fast (~ms for thousands
of records), independent of YAML reparse cost. A background thread
calls Index.refresh() every REFRESH_INTERVAL_S so YAML edits show up
in the API without per-request reparse latency. (refresh() is itself
the expensive thing — see PARKED.md on the dual-mtime fix.)
"""
from __future__ import annotations

import http.server
import gzip
import hashlib
import json
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from glean.index import Index
from glean.graph.worker import GraphWorker
from glean.publication import PublicationWorker
from glean.schema import Record
from glean.store import Library


REFRESH_INTERVAL_S = 60


def _refresh_index(idx: Index, *, phase: str) -> bool:
    try:
        idx.refresh()
    except Exception as exc:
        print(f"glean web {phase} refresh failed: {exc}", file=sys.stderr, flush=True)
        return False
    return True


def _list_entries(idx: Index) -> list[dict]:
    """All records for the library browse view, joined with authors."""
    conn = idx.connect()
    rows = conn.execute(
        "SELECT citekey, title, year, published_at, doi, added_at FROM records "
        "ORDER BY year DESC NULLS LAST, published_at DESC NULLS LAST, "
        "added_at DESC NULLS LAST, citekey ASC"
    ).fetchall()
    by_ck: dict[str, list[dict]] = {}
    for ck, family, given in conn.execute(
        "SELECT citekey, family, given FROM authors ORDER BY citekey, ord"
    ):
        by_ck.setdefault(ck, []).append({"family": family, "given": given or ""})
    entries = []
    for ck, title, year, published_at, doi, added_at in rows:
        entries.append({
            "citekey": ck,
            "title": title,
            "year": year,
            "published_at": published_at,
            "doi": doi,
            "added_at": added_at,
            "authors": by_ck.get(ck, []),
        })
    return entries


def _version_for_entries(entries: list[dict]) -> str:
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _versioned_list_body(entries: list[dict]) -> dict:
    version = _version_for_entries(entries)
    return {
        "count": len(entries),
        "entries": entries,
        "version": version,
        "etag": version,
    }


def _list_papers(idx: Index) -> dict:
    return _versioned_list_body(_list_entries(idx))


def _list_delta(entries: list[dict], *, since: str | None) -> dict:
    current = _versioned_list_body(entries)
    return _delta_for_body(current, since=since)


def _delta_for_body(current: dict, *, since: str | None) -> dict:
    if since and since == current["version"]:
        return {
            "count": 0,
            "entries": [],
            "version": current["version"],
            "etag": current["etag"],
            "unchanged": True,
        }
    current = dict(current)
    current["unchanged"] = False
    return current


class PapersSnapshot:
    """Published /api/papers response from the last complete index refresh."""

    def __init__(self, body: dict) -> None:
        self._body = body
        self._lock = threading.RLock()

    @classmethod
    def from_index(cls, idx: Index) -> "PapersSnapshot":
        return cls(_list_papers(idx))

    def body(self) -> dict:
        with self._lock:
            return self._body

    def delta(self, *, since: str | None) -> dict:
        with self._lock:
            return _delta_for_body(self._body, since=since)

    def refresh_from_index(self, idx: Index, *, phase: str) -> bool:
        refreshed = _refresh_index(idx, phase=phase)
        try:
            body = _list_papers(idx)
        except Exception as exc:
            print(
                f"glean web {phase} snapshot publish failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        with self._lock:
            self._body = body
        return refreshed


def _json_response(
    body: dict | list,
    *,
    request_headers,
    status: int = 200,
) -> tuple[int, dict[str, str], bytes]:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "ETag": etag,
        "Cache-Control": "no-cache, no-store",
        "Vary": "Accept-Encoding",
    }
    if status == 200 and _header_value(request_headers, "If-None-Match") == etag:
        return 304, headers, b""
    if _accepts_gzip(request_headers):
        payload = gzip.compress(payload)
        headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(payload))
    return status, headers, payload


def _header_value(headers, name: str) -> str | None:
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        lower_name = name.lower()
        for key, candidate in headers.items():
            if key.lower() == lower_name:
                value = candidate
                break
    return value


def _accepts_gzip(headers) -> bool:
    encoding = _header_value(headers, "Accept-Encoding") or ""
    return "gzip" in {item.split(";", 1)[0].strip().lower()
                      for item in encoding.split(",")}


def _record_detail(lib: Library, citekey: str) -> dict | None:
    """Full YAML-backed record. Reads disk; called on-demand when the
    Mac client opens a row, so per-request cost is fine."""
    try:
        record = lib.load(citekey)
    except FileNotFoundError:
        return None
    return {
        "citekey": record.citekey,
        "type": record.type,
        "title": record.title,
        "year": record.year,
        "published_at": (
            record.published_at.isoformat() if record.published_at else None
        ),
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


def _record_detail_response(lib: Library, citekey: str) -> tuple[int, dict]:
    """Return a structured result even when protected-library reads fail."""
    try:
        detail = _record_detail(lib, citekey)
    except Exception as exc:
        print(
            f"glean web detail read failed for {citekey}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 503, {
            "error": "library unavailable",
            "code": "library_unavailable",
            "detail": str(exc),
            "citekey": citekey,
        }
    if detail is None:
        return 404, {"error": "not found", "citekey": citekey}
    return 200, detail


def _attachment_action(suffix: str) -> tuple[str, str] | None:
    for action in (
        "/pdf",
        "/attachment-plan",
        "/attachment-commit",
        "/attachment-release",
    ):
        if suffix.endswith(action):
            citekey = urllib.parse.unquote(suffix[: -len(action)])
            return citekey, action[1:]
    return None


def _load_record_response(lib: Library, citekey: str) -> tuple[int, dict] | Record:
    try:
        return lib.load(citekey)
    except FileNotFoundError:
        return 404, {"ok": False, "error": "not found", "citekey": citekey}


def attachment_plan(lib: Library, citekey: str) -> tuple[int, dict]:
    loaded = _load_record_response(lib, citekey)
    if isinstance(loaded, tuple):
        return loaded
    try:
        relative_path = lib.planned_main_attachment_path(loaded)
    except RuntimeError as exc:
        return 409, {"ok": False, "error": str(exc), "citekey": citekey}
    return 200, {
        "ok": True,
        "citekey": citekey,
        "relative_path": relative_path,
    }


def attachment_commit(
    lib: Library, idx: Index, citekey: str, relative_path: str
) -> tuple[int, dict]:
    loaded = _load_record_response(lib, citekey)
    if isinstance(loaded, tuple):
        return loaded
    try:
        recorded = lib.set_main_attachment_path(loaded, relative_path)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc), "citekey": citekey}
    idx._upsert(loaded)
    return 200, {
        "ok": True,
        "citekey": citekey,
        "relative_path": recorded,
    }


def attachment_release(lib: Library, citekey: str) -> tuple[int, dict]:
    loaded = _load_record_response(lib, citekey)
    if isinstance(loaded, tuple):
        return loaded
    removed = lib.release_main_attachment_file(loaded)
    return 200, {"ok": True, "citekey": citekey, "removed": removed}


def main_attachment_pdf(lib: Library, citekey: str) -> tuple[int, Path | dict]:
    loaded = _load_record_response(lib, citekey)
    if isinstance(loaded, tuple):
        return loaded
    path = lib.main_attachment_file(loaded)
    if path is None or not path.is_file():
        return 404, {"ok": False, "error": "pdf not on server", "citekey": citekey}
    return 200, path


def _relation_response(idx: Index, citekey: str, relation: str) -> tuple[int, dict]:
    state = idx.graph_relation_state(citekey)
    if state is None:
        return 404, {"error": "not found", "citekey": citekey}
    if relation == "references":
        entries = idx.reference_entries(citekey)
    elif relation == "cited-by":
        entries = idx.cited_by_entries(citekey)
    else:
        raise ValueError(f"unknown relation: {relation}")
    body = {
        **state,
        "relation": relation,
        "count": len(entries),
        "entries": entries,
    }
    if relation == "cited-by":
        body["coverage"] = idx.graph_status()
    return 200, body


def _retry_relation(idx: Index, citekey: str) -> tuple[int, dict]:
    state = idx.graph_relation_state(citekey)
    if state is None:
        return 404, {"error": "not found", "citekey": citekey}
    graph_state = state["graph"]["state"]
    if graph_state == "unsupported":
        return 422, {
            "error": "automatic reference lookup requires a DOI",
            "code": "unsupported",
            "citekey": citekey,
        }
    if graph_state not in ("complete", "empty"):
        idx.enqueue_graph_job(citekey, priority=100)
        state = idx.graph_relation_state(citekey) or state
    return 202, {
        **state,
        "queued": state["graph"]["state"] == "queued",
    }


def _library_health(lib: Library, snapshot: PapersSnapshot) -> tuple[int, dict]:
    """Probe a real YAML record so health reflects Documents access."""
    body = snapshot.body()
    entries = body.get("entries") or []
    result = {
        "ok": False,
        "ready": False,
        "library_access": False,
        "count": int(body.get("count") or 0),
        "record_probe": "",
    }
    if not entries:
        result["error"] = "no indexed record available for protected-library probe"
        return 503, result
    errors = []
    for entry in entries[:10]:
        probe_citekey = entry.get("citekey") or ""
        if not probe_citekey:
            continue
        result["record_probe"] = probe_citekey
        try:
            lib.load(probe_citekey)
        except PermissionError as exc:
            result["error"] = str(exc)
            return 503, result
        except Exception as exc:
            errors.append(f"{probe_citekey}: {exc}")
            continue
        result.update({"ok": True, "ready": True, "library_access": True})
        return 200, result
    result["error"] = "; ".join(errors) or "no readable record available for probe"
    return 503, result


def _start_refresh_thread(idx: Index, snapshot: PapersSnapshot) -> None:
    """Background daemon that re-reads YAML mtimes every minute so the
    API serves fresh data without paying refresh() cost on each request.

    Requests serve an immutable response snapshot from the last complete
    refresh, so they neither block behind the slow YAML scan nor observe the
    SQLite index while it is being updated row-by-row.
    """
    def loop() -> None:
        while True:
            time.sleep(REFRESH_INTERVAL_S)
            snapshot.refresh_from_index(idx, phase="background")
    threading.Thread(target=loop, daemon=True, name="glean-web-refresh").start()


def _start_graph_worker(lib: Library, db_path: Path) -> None:
    """Run one durable enrichment worker owned by the server web service."""
    def loop() -> None:
        worker_index = Index(lib, db_path)
        worker = GraphWorker(lib, worker_index)
        worker.run_forever()

    threading.Thread(target=loop, daemon=True, name="glean-graph-worker").start()


def _start_publication_worker(lib: Library, db_path: Path) -> None:
    """Run one durable publication-date worker owned by the server service."""
    def loop() -> None:
        worker_index = Index(lib, db_path)
        worker = PublicationWorker(lib, worker_index)
        worker.run_forever()

    threading.Thread(
        target=loop,
        daemon=True,
        name="glean-publication-worker",
    ).start()


def serve(host: str = "0.0.0.0", port: int = 8766) -> None:
    lib = Library(Path.home() / "Documents" / "glean")
    db_path = Path.home() / "Library" / "Caches" / "glean" / "index.sqlite"
    idx = Index(lib, db_path)
    _refresh_index(idx, phase="startup")
    idx.reconcile_graph_jobs()
    idx.reconcile_publication_jobs()
    snapshot = PapersSnapshot.from_index(idx)
    _start_refresh_thread(idx, snapshot)
    _start_graph_worker(lib, db_path)
    _start_publication_worker(lib, db_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 1_000_000:
                return b""
            return self.rfile.read(length)

        def _send_file(self, path: Path, content_type: str) -> None:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, body: dict | list) -> None:
            response_status, headers, payload = _json_response(
                body,
                request_headers=self.headers,
                status=status,
            )
            self.send_response(response_status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                status, body = _library_health(lib, snapshot)
                return self._json(status, body)
            if parsed.path == "/api/papers":
                return self._json(200, snapshot.body())
            if parsed.path == "/api/papers/delta":
                query = urllib.parse.parse_qs(parsed.query)
                since = query.get("since", [None])[0]
                return self._json(
                    200,
                    snapshot.delta(since=since),
                )
            if parsed.path == "/api/graph/status":
                return self._json(200, idx.graph_status())
            if parsed.path == "/api/publication-dates/status":
                return self._json(200, idx.publication_status())
            if parsed.path.startswith("/api/papers/"):
                suffix = parsed.path[len("/api/papers/"):]
                action = _attachment_action(suffix)
                if action is not None and action[1] == "pdf":
                    status, payload = main_attachment_pdf(lib, action[0])
                    if isinstance(payload, dict):
                        return self._json(status, payload)
                    return self._send_file(payload, "application/pdf")
                if suffix.endswith("/references"):
                    citekey = urllib.parse.unquote(suffix[:-len("/references")])
                    status, body = _relation_response(idx, citekey, "references")
                    return self._json(status, body)
                if suffix.endswith("/cited-by"):
                    citekey = urllib.parse.unquote(suffix[:-len("/cited-by")])
                    status, body = _relation_response(idx, citekey, "cited-by")
                    return self._json(status, body)
                citekey = urllib.parse.unquote(suffix)
                status, body = _record_detail_response(lib, citekey)
                return self._json(status, body)
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/papers/"):
                action = _attachment_action(parsed.path[len("/api/papers/"):])
                if action is not None:
                    citekey, name = action
                    if name == "attachment-plan":
                        status, body = attachment_plan(lib, citekey)
                        return self._json(status, body)
                    if name == "attachment-commit":
                        form = urllib.parse.parse_qs(self._read_body().decode())
                        relative_path = form.get("relative_path", [""])[0]
                        status, body = attachment_commit(
                            lib, idx, citekey, relative_path
                        )
                        return self._json(status, body)
                    if name == "attachment-release":
                        status, body = attachment_release(lib, citekey)
                        return self._json(status, body)
            prefix = "/api/papers/"
            suffix = "/references/retry"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                citekey = urllib.parse.unquote(
                    parsed.path[len(prefix):-len(suffix)]
                )
                status, body = _retry_relation(idx, citekey)
                return self._json(status, body)
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
