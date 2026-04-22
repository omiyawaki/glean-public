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
        "type": "article",
        "authorships": [{"author": {"display_name": "Jane Smith"}}],
        "referenced_works": [f"https://openalex.org/{rid}" for rid in ref_ids],
    }


def test_openalex_fetch_combines_main_and_refs(cache_root):
    responses = {
        "https://api.openalex.org/works/doi:10.1/foo?mailto=user@example.com":
            _openalex_work_response("10.1/foo", ["W1", "W2"]),
        "https://api.openalex.org/works/W1?select=doi,title,publication_year&mailto=user@example.com":
            {"doi": "https://doi.org/10.2/a"},
        "https://api.openalex.org/works/W2?select=doi,title,publication_year&mailto=user@example.com":
            {"doi": "https://doi.org/10.2/b"},
    }

    def http_get(url: str):
        return responses.get(url)

    out = openalex.fetch("10.1/foo", http_get=http_get)
    assert out is not None
    assert out["metadata"]["title"] == "A fake paper"
    assert out["metadata"]["authors"] == [{"family": "Smith", "given": "Jane"}]
    assert out["references"] == ["10.2/a", "10.2/b"]


def test_openalex_fetch_drops_refs_without_dois(cache_root):
    responses = {
        "https://api.openalex.org/works/doi:10.1/foo?mailto=user@example.com":
            _openalex_work_response("10.1/foo", ["W1", "W2"]),
        "https://api.openalex.org/works/W1?select=doi,title,publication_year&mailto=user@example.com":
            {"doi": None},
        "https://api.openalex.org/works/W2?select=doi,title,publication_year&mailto=user@example.com":
            {"doi": "https://doi.org/10.2/b"},
    }
    out = openalex.fetch("10.1/foo", http_get=lambda url: responses.get(url))
    assert out["references"] == ["10.2/b"]


def test_openalex_caches(cache_root):
    calls = []

    def http_get(url: str):
        calls.append(url)
        if "W1" in url:
            return {"doi": "https://doi.org/10.2/a"}
        return _openalex_work_response("10.1/foo", ["W1"])

    openalex.fetch("10.1/foo", http_get=http_get)
    first = len(calls)
    openalex.fetch("10.1/foo", http_get=http_get)
    # Second call should be served from cache — no new HTTP hits.
    assert len(calls) == first


def test_openalex_returns_none_on_http_failure(cache_root):
    assert openalex.fetch("10.1/foo", http_get=lambda _: None) is None


def test_crossref_fetch_extracts_refs(cache_root):
    payload = {
        "message": {
            "DOI": "10.1/foo",
            "title": ["A paper"],
            "author": [{"family": "Smith", "given": "Jane"}],
            "issued": {"date-parts": [[2024]]},
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


def test_crossref_returns_none_on_http_failure(cache_root):
    assert crossref.fetch("10.1/foo", http_get=lambda _: None) is None
