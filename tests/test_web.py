from datetime import date
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
    _relation_response,
    _retry_relation,
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


def test_list_papers_orders_each_year_by_publication_date(tmp_path: Path):
    lib = Library(tmp_path / "library")
    records = [
        Record(
            citekey="Alpha2026Older",
            type="journal-article",
            title="Older 2026 addition",
            authors=[Author(family="Alpha", given="A.")],
            year=2026,
            published_at=date(2026, 4, 1),
            added_at=date(2026, 6, 1),
            modified_at=None,
        ),
        Record(
            citekey="Zulu2026Newer",
            type="journal-article",
            title="Newer 2026 addition",
            authors=[Author(family="Zulu", given="Z.")],
            year=2026,
            published_at=date(2026, 5, 1),
            added_at=date(2026, 4, 1),
            modified_at=None,
        ),
        Record(
            citekey="Beta2025NewestAddition",
            type="journal-article",
            title="Newest addition from an older year",
            authors=[Author(family="Beta", given="B.")],
            year=2025,
            published_at=date(2025, 12, 1),
            added_at=date(2026, 6, 1),
            modified_at=None,
        ),
    ]
    for record in records:
        lib.save(record)
    index = Index(lib, tmp_path / "ordered-index.sqlite")
    index.rebuild()

    body = _list_papers(index)

    assert [entry["citekey"] for entry in body["entries"]] == [
        "Zulu2026Newer",
        "Alpha2026Older",
        "Beta2025NewestAddition",
    ]
    assert body["entries"][0]["published_at"] == "2026-05-01"


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


def test_papers_snapshot_publishes_intact_index_when_yaml_refresh_fails(
    tmp_path: Path,
    monkeypatch,
):
    lib = Library(tmp_path / "library")
    record = Record(
        citekey="Alpha2026First",
        type="journal-article",
        title="First",
        authors=[Author(family="Alpha", given="A.")],
        year=2026,
        doi="10.1234/alpha",
    )
    lib.save(record)
    index = Index(lib, tmp_path / "index.sqlite")
    index.rebuild()
    snapshot = PapersSnapshot.from_index(index)
    assert snapshot.body()["entries"][0]["published_at"] is None

    record = lib.load(record.citekey)
    record.published_at = date(2026, 5, 3)
    lib.save(record)
    index._upsert(record)
    monkeypatch.setattr(
        index,
        "refresh",
        lambda: (_ for _ in ()).throw(RuntimeError("protected library unavailable")),
    )

    assert snapshot.refresh_from_index(index, phase="test") is False
    assert snapshot.body()["entries"][0]["published_at"] == "2026-05-03"


def test_references_response_is_ordered_and_normalized(lib: Library, tmp_path: Path):
    index = Index(lib, tmp_path / "relations.sqlite")
    index.rebuild()

    status, body = _relation_response(index, "Wills2023Citation", "references")

    assert status == 200
    assert body["source"]["citekey"] == "Wills2023Citation"
    assert body["count"] == 4
    assert body["entries"][0]["citekey"] == "Miyawaki2024Attention"
    assert body["entries"][0]["in_library"] is True
    assert body["entries"][0]["doi"] == "10.1175/JCLI-D-23-0456.1"
    assert body["entries"][2]["in_library"] is False


def test_cited_by_is_library_only_and_includes_coverage(lib: Library, tmp_path: Path):
    index = Index(lib, tmp_path / "incoming.sqlite")
    index.rebuild()

    status, body = _relation_response(
        index,
        "Miyawaki2024Attention",
        "cited-by",
    )

    assert status == 200
    assert [entry["citekey"] for entry in body["entries"]] == ["Wills2023Citation"]
    assert all(entry["in_library"] for entry in body["entries"])
    assert body["coverage"]["eligible"] == 4
    assert body["coverage"]["unsupported"] == 1


def test_relation_retry_is_idempotent(lib: Library, tmp_path: Path):
    index = Index(lib, tmp_path / "retry.sqlite")
    index.rebuild()

    first_status, first = _retry_relation(index, "Miyawaki2024Attention")
    second_status, second = _retry_relation(index, "Miyawaki2024Attention")

    assert first_status == second_status == 202
    assert first["queued"] is True
    assert second["queued"] is True
    count = index.connect().execute(
        "SELECT COUNT(*) FROM graph_jobs WHERE citekey = ?",
        ("Miyawaki2024Attention",),
    ).fetchone()[0]
    assert count == 1


def test_relation_retry_reports_doi_less_records_as_unsupported(
    lib: Library,
    tmp_path: Path,
):
    index = Index(lib, tmp_path / "unsupported.sqlite")
    index.rebuild()

    status, body = _retry_relation(index, "Vallis2019AOFD")

    assert status == 422
    assert body["code"] == "unsupported"
