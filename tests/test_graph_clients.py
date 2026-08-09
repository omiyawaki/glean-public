"""Tests for openalex.py and crossref.py with injected mock http_get."""

from __future__ import annotations

from pathlib import Path

import pytest

from glean.graph import openalex, crossref


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(openalex, "CACHE_ROOT", tmp_path / "openalex")
    monkeypatch.setattr(crossref, "CACHE_ROOT", tmp_path / "crossref")
    return tmp_path


def _openalex_work_response(doi: str, ref_ids: list[str]) -> dict:
    return {
        "doi": f"https://doi.org/{doi}",
        "title": "A fake paper",
        "publication_year": 2024,
        "publication_date": "2024-03-14",
        "type": "article",
        "authorships": [{"author": {"display_name": "Jane Smith"}}],
        "referenced_works": [f"https://openalex.org/{rid}" for rid in ref_ids],
    }


def test_openalex_fetch_combines_main_and_refs(cache_root):
    responses = {
        "https://api.openalex.org/works/doi:10.1/foo?mailto=user@example.com":
            _openalex_work_response("10.1/foo", ["W1", "W2"]),
    }

    def http_get(url: str):
        if url.startswith("https://api.openalex.org/works?filter=openalex:"):
            return {"results": [
                {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.2/a"},
                {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.2/b"},
            ]}
        return responses.get(url)

    out = openalex.fetch("10.1/foo", http_get=http_get)
    assert out is not None
    assert out["metadata"]["title"] == "A fake paper"
    assert out["metadata"]["authors"] == [{"family": "Smith", "given": "Jane"}]
    assert out["metadata"]["published_at"] == "2024-03-14"
    assert out["references"] == ["10.2/a", "10.2/b"]


def test_openalex_fetch_preserves_refs_without_dois(cache_root):
    responses = {
        "https://api.openalex.org/works/doi:10.1/foo?mailto=user@example.com":
            _openalex_work_response("10.1/foo", ["W1", "W2"]),
    }

    def http_get(url: str):
        if url.startswith("https://api.openalex.org/works?filter=openalex:"):
            return {"results": [
                {"id": "https://openalex.org/W1", "doi": None},
                {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.2/b"},
            ]}
        return responses.get(url)

    out = openalex.fetch("10.1/foo", http_get=http_get)
    assert out["references"] == ["openalex:W1", "10.2/b"]


def test_openalex_caches(cache_root):
    calls = []

    def http_get(url: str):
        calls.append(url)
        if url.startswith("https://api.openalex.org/works?filter=openalex:"):
            return {"results": [{
                "id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.2/a",
            }]}
        return _openalex_work_response("10.1/foo", ["W1"])

    openalex.fetch("10.1/foo", http_get=http_get)
    first = len(calls)
    openalex.fetch("10.1/foo", http_get=http_get)
    assert len(calls) == first


def test_openalex_batches_reference_metadata(cache_root):
    calls: list[str] = []

    def http_get(url: str):
        calls.append(url)
        if "/works/doi:" in url:
            return _openalex_work_response(
                "10.1/foo",
                [f"W{index}" for index in range(51)],
            )
        ids = url.split("filter=openalex:", 1)[1].split("&", 1)[0].split("|")
        return {"results": [
            {"id": f"https://openalex.org/{identifier}", "doi": None}
            for identifier in ids
        ]}

    out = openalex.fetch("10.1/foo", http_get=http_get)

    batch_calls = [url for url in calls if "filter=openalex:" in url]
    assert len(batch_calls) == 2
    assert len(out["references"]) == 51


def test_openalex_returns_none_on_http_failure(cache_root):
    assert openalex.fetch("10.1/foo", http_get=lambda _: None) is None


def test_crossref_fetch_extracts_refs(cache_root):
    payload = {
        "message": {
            "DOI": "10.1/foo",
            "title": ["A paper"],
            "author": [{"family": "Smith", "given": "Jane"}],
            "issued": {"date-parts": [[2024]]},
            "published-online": {"date-parts": [[2024, 5, 2]]},
            "type": "journal-article",
            "reference": [
                {"DOI": "10.2/a"},
                {"key": "no-doi-here"},
                {"DOI": "10.2/b"},
            ],
        }
    }
    out = crossref.fetch("10.1/foo", http_get=lambda _: payload)
    assert out["references"] == ["10.2/a", "10.2/b"]
    assert out["metadata"]["authors"] == [{"family": "Smith", "given": "Jane"}]
    assert out["metadata"]["year"] == 2024
    assert out["metadata"]["published_at"] == "2024-05-02"


def test_crossref_returns_none_on_http_failure(cache_root):
    assert crossref.fetch("10.1/foo", http_get=lambda _: None) is None
