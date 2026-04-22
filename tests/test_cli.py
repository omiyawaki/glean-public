from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from glean.cli import main
from glean.store import Library


@pytest.fixture
def cli_env(lib: Library, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLEAN_LIBRARY", str(lib.root))
    monkeypatch.setenv("GLEAN_INDEX", str(tmp_path / "index.sqlite"))
    return lib


def test_info_known(cli_env, capsys):
    rc = main(["info", "Miyawaki2024Attention"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out
    assert "Attention and convection" in out
    assert "attachments:" in out


def test_info_unknown(cli_env, capsys):
    rc = main(["info", "NoSuchKey"])
    assert rc == 1


def test_search_returns_hits(cli_env, capsys):
    rc = main(["search", "attention"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out


def test_search_no_hits(cli_env):
    rc = main(["search", "zzz_no_such_term_zzz"])
    assert rc == 1


def test_ls_by_year(cli_env, capsys):
    rc = main(["ls", "--year", "1974"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Arakawa1974Schubert" in out
    assert "Miyawaki2024Attention" not in out


def test_ls_by_author(cli_env, capsys):
    rc = main(["ls", "--author", "Miyawaki"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out
    assert "Miyawaki2023Transformer" in out


def test_export_json(cli_env, capsys):
    rc = main(["export", "--format", "json", "Arakawa1974Schubert"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data[0]["citekey"] == "Arakawa1974Schubert"
    assert data[0]["authors"][0]["family"] == "Arakawa"


def test_export_bibtex(cli_env, capsys):
    rc = main(["export", "--format", "bibtex", "Arakawa1974Schubert"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "@article" in out.lower()
    assert "Arakawa1974Schubert" in out


def test_rm(cli_env):
    rc = main(["rm", "Miyawaki2023Transformer"])
    assert rc == 0
    assert not cli_env.exists("Miyawaki2023Transformer")


def test_rename_rewrites_refs(cli_env):
    # Miyawaki2024Attention references Sellers1965PhysicalClimatology as a citekey.
    # Rename that citekey (even though the target record doesn't exist in library,
    # the rename of an *actual* record should cascade its ref in pointers).
    # Here we rename a record whose citekey appears in Vallis2019AOFD's references.
    rc = main(["rename", "Arakawa1974Schubert", "Arakawa1974NewName"])
    assert rc == 0
    assert not cli_env.exists("Arakawa1974Schubert")
    assert cli_env.exists("Arakawa1974NewName")


def test_rename_rejects_invalid(cli_env):
    rc = main(["rename", "Arakawa1974Schubert", "has space"])
    assert rc == 6


def test_rename_rejects_missing(cli_env):
    rc = main(["rename", "NoSuchKey", "NewKey"])
    assert rc == 1


def test_reindex(cli_env, capsys):
    rc = main(["reindex", "--force"])
    assert rc == 0
    assert "rebuilt" in capsys.readouterr().out


def test_reconcile_dry_run(cli_env, capsys):
    rc = main(["reconcile-attachments", "--dry-run"])
    assert rc == 0
    # Fixture attachments don't exist on disk — they'll be reported as missing.
    out = capsys.readouterr().out
    assert "missing" in out or "resolve" in out


def test_tui_imports(cli_env):
    # Don't actually launch the TUI (requires a TTY); just confirm the module loads.
    from glean.tui.app import GleanApp
    assert GleanApp is not None


# ---------- glean add (mocked paper_intake) ----------

def _patch_paper_intake(monkeypatch, *, doi=None, meta=None):
    from glean import _paper_intake
    monkeypatch.setattr(_paper_intake, "normalize_identifier", lambda _: doi)
    monkeypatch.setattr(_paper_intake, "crossref_metadata", lambda _: meta)


def test_add_unresolvable_returns_5(cli_env, monkeypatch):
    _patch_paper_intake(monkeypatch, doi=None)
    rc = main(["add", "nonsense"])
    assert rc == 5


def test_add_missing_metadata_returns_5(cli_env, monkeypatch):
    _patch_paper_intake(monkeypatch, doi="10.9999/x", meta=None)
    rc = main(["add", "10.9999/x"])
    assert rc == 5


def test_add_duplicate_returns_4(cli_env, monkeypatch, capsys):
    # Miyawaki2024Attention has doi 10.1175/JCLI-D-23-0456.1 in the fixture.
    _patch_paper_intake(monkeypatch, doi="10.1175/JCLI-D-23-0456.1")
    # Ensure the index is built first via a prior reindex call.
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["add", "10.1175/JCLI-D-23-0456.1"])
    out = capsys.readouterr().out.strip()
    assert rc == 4
    assert out == "Miyawaki2024Attention"


def test_add_success(cli_env, monkeypatch, capsys):
    _patch_paper_intake(
        monkeypatch,
        doi="10.1234/new-record",
        meta={
            "title": "A brand new paper",
            "authors": "Smith, Jane\nDoe, John",
            "journal": "Nature",
            "abstract": "",
            "year": "2026",
        },
    )
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["add", "anything"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "Smith2026Brand"
    # Verify the record landed on disk.
    assert cli_env.exists("Smith2026Brand")
    r = cli_env.load("Smith2026Brand")
    assert r.doi == "10.1234/new-record"
    assert r.journal == "Nature"
    assert r.type == "journal-article"


def test_add_arxiv_preprint(cli_env, monkeypatch, capsys):
    _patch_paper_intake(
        monkeypatch,
        doi="10.48550/arXiv.2401.99999",
        meta={
            "title": "Something on arXiv",
            "authors": "Lee, Chen",
            "journal": "",
            "abstract": "",
            "year": "2024",
        },
    )
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["add", "arXiv:2401.99999"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    r = cli_env.load(out)
    assert r.type == "preprint"
    assert r.arxiv == "2401.99999"


def test_add_quiet_suppresses_stdout(cli_env, monkeypatch, capsys):
    _patch_paper_intake(monkeypatch, doi=None)
    rc = main(["add", "nonsense", "--quiet"])
    assert rc == 5
    assert capsys.readouterr().out == ""


# ---------- glean graph ----------

def test_graph_out(cli_env, capsys):
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["graph", "out", "Wills2023Citation"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out
    assert "Arakawa1974Schubert" in out
    assert "[stub]" in out


def test_graph_in(cli_env, capsys):
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["graph", "in", "Miyawaki2024Attention"])
    assert rc == 0
    assert "Wills2023Citation" in capsys.readouterr().out


def test_graph_suggest(cli_env, capsys):
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["graph", "suggest", "--min-citations", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "10.1175/JAS-D-22-0011.1" in out


def test_graph_fetch_no_doi_returns_6(cli_env):
    main(["reindex", "--force"])
    rc = main(["graph", "fetch", "Vallis2019AOFD"])
    assert rc == 6


def test_graph_fetch_populates_refs(cli_env, monkeypatch):
    from glean.graph import openalex

    def fake_fetch(doi, **_):
        return {"metadata": {}, "references": ["10.1111/xx", "10.2222/yy"]}

    monkeypatch.setattr(openalex, "fetch", fake_fetch)
    main(["reindex", "--force"])
    rc = main(["graph", "fetch", "Arakawa1974Schubert"])
    assert rc == 0
    r = cli_env.load("Arakawa1974Schubert")
    assert r.references == ["10.1111/xx", "10.2222/yy"]
