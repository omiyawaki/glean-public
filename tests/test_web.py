from pathlib import Path
import gzip
import json

from glean.store import Library
from glean.web import (
    _json_response,
    _list_delta,
    _record_detail,
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
