from pathlib import Path
import gzip
import json
import threading
import time

from glean.index import Index
from glean.schema import Author, Record
from glean.store import Library
from glean.web import (
    PapersSnapshot,
    _json_response,
    _list_delta,
    _list_papers,
    _library_health,
    _record_detail,
    _record_detail_response,
    _refresh_index,
    _versioned_list_body,
)


class RefreshingIndex:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> tuple[int, int, int]:
        self.calls += 1
        return (1, 2, 3)


class FailingIndex:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> tuple[int, int, int]:
        self.calls += 1
        raise OSError(11, "Resource deadlock avoided")


def test_refresh_index_returns_true_when_refresh_succeeds(capsys):
    idx = RefreshingIndex()

    assert _refresh_index(idx, phase="startup") is True

    assert idx.calls == 1
    assert capsys.readouterr().err == ""


def test_refresh_index_logs_and_returns_false_when_refresh_fails(capsys):
    idx = FailingIndex()

    assert _refresh_index(idx, phase="startup") is False

    captured = capsys.readouterr()
    assert idx.calls == 1
    assert "glean web startup refresh failed" in captured.err
    assert "Resource deadlock avoided" in captured.err


def test_record_detail_retries_transient_deadlock(lib: Library, monkeypatch):
    original_read_text = Path.read_text
    calls = 0

    def flaky_read_text(path: Path, *args, **kwargs):
        nonlocal calls
        if path.name == "Miyawaki2024Attention.yaml" and calls == 0:
            calls += 1
            raise OSError(11, "Resource deadlock avoided")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    detail = _record_detail(lib, "Miyawaki2024Attention")

    assert detail is not None
    assert detail["citekey"] == "Miyawaki2024Attention"
    assert calls == 1


def test_record_detail_permission_failure_is_structured_503(lib: Library, monkeypatch):
    def denied(_citekey: str):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(lib, "load", denied)

    status, body = _record_detail_response(lib, "Miyawaki2024Attention")

    assert status == 503
    assert body["code"] == "library_unavailable"
    assert body["citekey"] == "Miyawaki2024Attention"
    assert "Operation not permitted" in body["detail"]


def test_health_probes_an_actual_record(lib: Library, tmp_path: Path):
    index = Index(lib, tmp_path / "health-index.sqlite")
    _refresh_index(index, phase="test")
    snapshot = PapersSnapshot.from_index(index)

    status, body = _library_health(lib, snapshot)

    assert status == 200
    assert body["ready"] is True
    assert body["library_access"] is True
    assert body["record_probe"]


def test_health_reports_permission_loss(lib: Library, tmp_path: Path, monkeypatch):
    index = Index(lib, tmp_path / "denied-index.sqlite")
    _refresh_index(index, phase="test")
    snapshot = PapersSnapshot.from_index(index)

    def denied(_citekey: str):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(lib, "load", denied)
    status, body = _library_health(lib, snapshot)

    assert status == 503
    assert body["ready"] is False
    assert body["library_access"] is False
    assert "Operation not permitted" in body["error"]


def test_json_response_sets_etag_and_supports_not_modified():
    status, headers, body = _json_response(
        {"count": 0, "entries": []},
        request_headers={},
    )

    assert status == 200
    assert headers["ETag"]
    assert json.loads(body.decode("utf-8")) == {"count": 0, "entries": []}

    status, not_modified_headers, not_modified_body = _json_response(
        {"count": 0, "entries": []},
        request_headers={"If-None-Match": headers["ETag"]},
    )

    assert status == 304
    assert not_modified_headers["ETag"] == headers["ETag"]
    assert not_modified_body == b""


def test_json_response_gzips_when_client_accepts_gzip():
    status, headers, body = _json_response(
        {"entries": [{"title": "x" * 1024}]},
        request_headers={"Accept-Encoding": "br, gzip"},
    )

    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    decoded = gzip.decompress(body)
    assert json.loads(decoded.decode("utf-8"))["entries"][0]["title"] == "x" * 1024


def test_versioned_list_body_includes_stable_version():
    entries = [
        {"citekey": "A", "title": "A", "year": 2026, "doi": None, "authors": []},
    ]

    body = _versioned_list_body(entries)

    assert body["count"] == 1
    assert body["entries"] == entries
    assert body["version"]
    assert body["etag"] == body["version"]


def test_list_delta_returns_empty_when_since_matches_current_version():
    entries = [
        {"citekey": "A", "title": "A", "year": 2026, "doi": None, "authors": []},
    ]
    current = _versioned_list_body(entries)

    delta = _list_delta(entries, since=current["version"])

    assert delta["count"] == 0
    assert delta["entries"] == []
    assert delta["unchanged"] is True
    assert delta["version"] == current["version"]


def test_list_delta_returns_snapshot_when_version_changed():
    entries = [
        {"citekey": "A", "title": "A", "year": 2026, "doi": None, "authors": []},
    ]

    delta = _list_delta(entries, since="old")

    assert delta["count"] == 1
    assert delta["entries"] == entries
    assert delta["unchanged"] is False


def test_papers_snapshot_serves_last_complete_body_during_refresh(tmp_path: Path):
    lib = Library(tmp_path / "library")
    first = Record(
        citekey="Alpha2026First",
        type="journal-article",
        title="First",
        authors=[Author(family="Alpha", given="A.")],
        year=2026,
        added_at=None,
        modified_at=None,
    )
    second = Record(
        citekey="Beta2025Second",
        type="journal-article",
        title="Second",
        authors=[Author(family="Beta", given="B.")],
        year=2025,
        added_at=None,
        modified_at=None,
    )
    lib.save(first)
    index = Index(lib, tmp_path / "index.sqlite")
    _refresh_index(index, phase="startup")
    snapshot = PapersSnapshot.from_index(index)
    assert snapshot.body()["count"] == 1

    lib.save(second)
    second_row_inserted = threading.Event()
    finish_refresh = threading.Event()
    original_upsert = index._upsert

    def slow_upsert(record):
        original_upsert(record)
        if record.citekey == "Beta2025Second":
            second_row_inserted.set()
            assert finish_refresh.wait(timeout=2)

    index._upsert = slow_upsert
    refresh_thread = threading.Thread(
        target=lambda: snapshot.refresh_from_index(index, phase="background")
    )
    refresh_thread.start()
    assert second_row_inserted.wait(timeout=2)

    start = time.perf_counter()
    during_refresh = snapshot.body()
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05
    assert during_refresh["count"] == 1
    assert [entry["citekey"] for entry in during_refresh["entries"]] == [
        "Alpha2026First",
    ]

    finish_refresh.set()
    refresh_thread.join(timeout=2)
    assert not refresh_thread.is_alive()

    after_refresh = snapshot.body()
    assert after_refresh["count"] == 2
    assert [entry["citekey"] for entry in after_refresh["entries"]] == [
        "Alpha2026First",
        "Beta2025Second",
    ]
