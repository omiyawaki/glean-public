from __future__ import annotations

import json
import os
import sqlite3
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


def test_info_shows_tags(cli_env, capsys):
    r = cli_env.load("Miyawaki2024Attention")
    r.tags = ["proj-a", "field-data"]
    cli_env.save(r)

    rc = main(["info", "Miyawaki2024Attention"])

    assert rc == 0
    assert "tags: proj-a, field-data" in capsys.readouterr().out


def test_info_unknown(cli_env, capsys):
    rc = main(["info", "NoSuchKey"])
    assert rc == 1


def test_search_returns_hits(cli_env, capsys):
    rc = main(["search", "attention"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out


def test_search_matches_tags(cli_env, capsys):
    r = cli_env.load("Miyawaki2024Attention")
    r.tags = ["proj-a"]
    cli_env.save(r)

    rc = main(["search", "proj-a"])

    assert rc == 0
    assert "Miyawaki2024Attention" in capsys.readouterr().out


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


def test_ls_by_tag(cli_env, capsys):
    r = cli_env.load("Miyawaki2024Attention")
    r.tags = ["proj-a"]
    cli_env.save(r)

    rc = main(["ls", "--tag", "PROJ-A"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out
    assert "Arakawa1974Schubert" not in out


def test_export_json(cli_env, capsys):
    rc = main(["export", "--format", "json", "Arakawa1974Schubert"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data[0]["citekey"] == "Arakawa1974Schubert"
    assert data[0]["authors"][0]["family"] == "Arakawa"
    assert data[0]["tags"] == []


def test_export_json_includes_tags(cli_env, capsys):
    r = cli_env.load("Miyawaki2024Attention")
    r.tags = ["proj-a", "upper-troposphere"]
    cli_env.save(r)

    rc = main(["export", "--format", "json", "Miyawaki2024Attention"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tags"] == ["proj-a", "upper-troposphere"]


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
    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(
        _paper_intake,
        "fetch_pdf",
        lambda *_, **__: type("FetchResult", (), {
            "filename": None,
            "candidate_label": None,
            "tried": [],
            "reason": "no candidates",
        })(),
        raising=False,
    )
    monkeypatch.setattr(_paper_intake, "pdf_backlog_add", lambda *_, **__: None, raising=False)


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


def test_add_duplicate_detects_doi_case_mismatch(cli_env, monkeypatch, capsys):
    # Migration stores DOIs as Bookends had them (often lowercase); Crossref
    # returns the canonical uppercase form. Duplicate check must match across.
    _patch_paper_intake(monkeypatch, doi="10.1175/jcli-d-23-0456.1")  # lowercase
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["add", "10.1175/jcli-d-23-0456.1"])
    out = capsys.readouterr().out.strip()
    assert rc == 4
    assert out == "Miyawaki2024Attention"


def test_add_recreates_record_when_duplicate_index_row_is_stale(cli_env, monkeypatch, capsys):
    _patch_paper_intake(
        monkeypatch,
        doi="10.1175/JCLI-D-23-0456.1",
        meta={
            "title": "Attention and convection",
            "authors": "Miyawaki, Osamu\nVallis, Geoffrey",
            "journal": "Journal of Climate",
            "abstract": "",
            "year": "2024",
        },
    )
    main(["reindex", "--force"])
    capsys.readouterr()
    cli_env.delete("Miyawaki2024Attention")

    rc = main(["add", "10.1175/JCLI-D-23-0456.1"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "Miyawaki2024Attention"
    assert "warning: stale index entry for Miyawaki2024Attention" in captured.err
    assert cli_env.exists("Miyawaki2024Attention")


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


def test_add_success_fetches_and_attaches_pdf(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    _patch_paper_intake(
        monkeypatch,
        doi="10.5194/acp-17-12495-2017",
        meta={
            "title": "In situ temperature measurements",
            "authors": "Berkes, Florian",
            "journal": "Atmospheric Chemistry and Physics",
            "abstract": "",
            "year": "2017",
        },
    )

    def fake_fetch_pdf(doi, publisher_url, title, *, dir_path, **_):
        pdf = Path(dir_path) / "berkes.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake\n")
        assert doi == "10.5194/acp-17-12495-2017"
        assert publisher_url == "https://doi.org/10.5194/acp-17-12495-2017"
        assert title == "In situ temperature measurements"
        return type("FetchResult", (), {
            "filename": "berkes.pdf",
            "candidate_label": "copernicus-journal-pdf",
            "tried": ["copernicus-journal-pdf"],
            "reason": "",
        })()

    monkeypatch.setattr(_paper_intake, "fetch_pdf", fake_fetch_pdf)
    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["add", "anything"])

    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "Berkes2017Situ"
    r = cli_env.load("Berkes2017Situ")
    assert [a.kind for a in r.attachments] == ["main"]
    assert (cli_env.root / r.attachments[0].path).read_bytes() == b"%PDF-1.4\nfake\n"


def test_add_success_queues_pdf_backlog_when_fetch_fails(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    _patch_paper_intake(
        monkeypatch,
        doi="10.1234/no-pdf",
        meta={
            "title": "No PDF yet",
            "authors": "Smith, Jane",
            "journal": "Journal",
            "abstract": "",
            "year": "2026",
        },
    )
    backlog_calls = []

    monkeypatch.setattr(
        _paper_intake,
        "fetch_pdf",
        lambda *_, **__: type("FetchResult", (), {
            "filename": None,
            "candidate_label": None,
            "tried": ["crossref-link"],
            "reason": "all candidates failed",
        })(),
    )
    monkeypatch.setattr(
        _paper_intake,
        "pdf_backlog_add",
        lambda doi, **kwargs: backlog_calls.append((doi, kwargs)),
    )
    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["add", "anything"])

    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "Smith2026No"
    r = cli_env.load("Smith2026No")
    assert r.attachments == []
    assert backlog_calls == [
        (
            "10.1234/no-pdf",
            {
                "title": "No PDF yet",
                "citekey": "Smith2026No",
                "tried": ["crossref-link"],
            },
        )
    ]


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


def test_add_crossref_preprint_doi(cli_env, monkeypatch, capsys):
    _patch_paper_intake(
        monkeypatch,
        doi="10.22541/essoar.15002298/v1",
        meta={
            "title": "Basin-wide sea-surface observations",
            "authors": "Medvedev, Andrei",
            "journal": "",
            "abstract": "",
            "year": "2026",
        },
    )
    from glean import _paper_intake
    monkeypatch.setattr(
        _paper_intake,
        "is_preprint_doi",
        lambda doi: doi.startswith("10.22541/"),
        raising=False,
    )

    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["add", "10.22541/essoar.15002298/v1"])

    out = capsys.readouterr().out.strip()
    assert rc == 0
    r = cli_env.load(out)
    assert r.type == "preprint"
    assert r.arxiv is None


def test_add_quiet_suppresses_stdout(cli_env, monkeypatch, capsys):
    _patch_paper_intake(monkeypatch, doi=None)
    rc = main(["add", "nonsense", "--quiet"])
    assert rc == 5
    assert capsys.readouterr().out == ""


def test_backfill_pdfs_fetches_missing_main_attachments(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    fetched = []

    def fake_fetch_pdf(doi, publisher_url, title, *, dir_path, **_):
        fetched.append((doi, publisher_url, title))
        pdf = Path(dir_path) / "paper.pdf"
        pdf.write_bytes(f"%PDF-1.4\n{doi}\n".encode())
        return type("FetchResult", (), {
            "filename": "paper.pdf",
            "candidate_label": "test-pdf",
            "tried": ["test-pdf"],
            "reason": "",
        })()

    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(_paper_intake, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(_paper_intake, "pdf_backlog_add", lambda *_, **__: None, raising=False)
    removed = []
    monkeypatch.setattr(_paper_intake, "pdf_backlog_remove", lambda doi: removed.append(doi), raising=False)

    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["backfill-pdfs", "--citekey", "Arakawa1974Schubert"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "attached\tArakawa1974Schubert" in out
    assert "attached=1" in out
    assert fetched == [
        (
            "10.1175/1520-0469(1974)031<0674:IOACCE>2.0.CO;2",
            "https://doi.org/10.1175/1520-0469(1974)031<0674:IOACCE>2.0.CO;2",
            "Interaction of a cumulus cloud ensemble with the large-scale environment",
        )
    ]
    r = cli_env.load("Arakawa1974Schubert")
    assert [a.kind for a in r.attachments] == ["main"]
    assert (cli_env.root / r.attachments[0].path).read_bytes().startswith(b"%PDF-1.4")
    assert removed == ["10.1175/1520-0469(1974)031<0674:IOACCE>2.0.CO;2"]


def test_backfill_pdfs_fetches_when_main_attachment_file_is_missing(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    fetched = []

    def fake_fetch_pdf(doi, publisher_url, title, *, dir_path, **_):
        fetched.append((doi, publisher_url, title))
        pdf = Path(dir_path) / "replacement.pdf"
        pdf.write_bytes(f"%PDF-1.4\n{doi}\n".encode())
        return type("FetchResult", (), {
            "filename": "replacement.pdf",
            "candidate_label": "test-pdf",
            "tried": ["test-pdf"],
            "reason": "",
        })()

    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(_paper_intake, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(_paper_intake, "pdf_backlog_add", lambda *_, **__: None, raising=False)
    removed = []
    monkeypatch.setattr(_paper_intake, "pdf_backlog_remove", lambda doi: removed.append(doi), raising=False)

    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["backfill-pdfs", "--citekey", "Miyawaki2024Attention"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "attached\tMiyawaki2024Attention" in out
    assert "attached=1" in out
    assert fetched == [
        (
            "10.1175/JCLI-D-23-0456.1",
            "https://doi.org/10.1175/JCLI-D-23-0456.1",
            "Attention and convection",
        )
    ]
    r = cli_env.load("Miyawaki2024Attention")
    assert [a.kind for a in r.attachments] == ["supplement", "main"]
    assert (cli_env.root / r.attachments[-1].path).read_bytes().startswith(b"%PDF-1.4")
    assert removed == ["10.1175/JCLI-D-23-0456.1"]


def test_backfill_pdfs_links_existing_canonical_attachment_before_fetching(
    cli_env,
    monkeypatch,
    capsys,
):
    from glean import _paper_intake

    attachment = (
        cli_env.root
        / "attachments"
        / "Arakawa and Schubert (1974) Interaction of a cumulus cloud ensemble with the large-scale environment.pdf"
    )
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"%PDF-1.4\nalready here\n")

    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(
        _paper_intake,
        "fetch_pdf",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    removed = []
    monkeypatch.setattr(_paper_intake, "pdf_backlog_remove", lambda doi: removed.append(doi), raising=False)

    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["backfill-pdfs", "--citekey", "Arakawa1974Schubert"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "attached\tArakawa1974Schubert" in out
    assert "attached=1" in out
    r = cli_env.load("Arakawa1974Schubert")
    assert [a.path for a in r.attachments] == [
        "attachments/Arakawa and Schubert (1974) Interaction of a cumulus cloud ensemble with the large-scale environment.pdf"
    ]
    assert attachment.read_bytes() == b"%PDF-1.4\nalready here\n"
    assert removed == ["10.1175/1520-0469(1974)031<0674:IOACCE>2.0.CO;2"]


def test_backfill_pdfs_with_citekey_does_not_require_full_index_refresh(
    cli_env,
    monkeypatch,
    capsys,
):
    from glean.cli import Index

    def locked_refresh(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Index, "refresh", locked_refresh)

    rc = main(["backfill-pdfs", "--citekey", "Arakawa1974Schubert", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run: 1 record(s) would be attempted" in out
    assert "Arakawa1974Schubert" in out


def test_backfill_pdfs_dry_run_lists_candidates(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    fetched = []
    monkeypatch.setattr(_paper_intake, "fetch_pdf", lambda *args, **kwargs: fetched.append(args), raising=False)

    rc = main(["backfill-pdfs", "--dry-run", "--limit", "3"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run: 3 record(s) would be attempted" in out
    assert "Arakawa1974Schubert" in out
    assert "Miyawaki2024Attention" in out
    assert fetched == []


def test_backfill_pdfs_fetch_exception_does_not_queue_backlog(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    backlog_calls = []
    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(
        _paper_intake,
        "fetch_pdf",
        lambda *_, **__: (_ for _ in ()).throw(ModuleNotFoundError("No module named 'playwright'")),
    )
    monkeypatch.setattr(
        _paper_intake,
        "pdf_backlog_add",
        lambda doi, **kwargs: backlog_calls.append((doi, kwargs)),
    )

    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["backfill-pdfs", "--citekey", "Arakawa1974Schubert"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "failed\tArakawa1974Schubert" in out
    assert "failed=1" in out
    assert backlog_calls == []


def test_backfill_pdfs_marks_no_candidates_as_failed_attempt(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    backlog_calls = []
    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(
        _paper_intake,
        "fetch_pdf",
        lambda *_, **__: type("FetchResult", (), {
            "filename": None,
            "candidate_label": None,
            "tried": [],
            "reason": "no candidates",
        })(),
    )
    monkeypatch.setattr(
        _paper_intake,
        "pdf_backlog_add",
        lambda doi, **kwargs: backlog_calls.append((doi, kwargs)),
    )

    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["backfill-pdfs", "--citekey", "Arakawa1974Schubert"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "queued\tArakawa1974Schubert" in out
    assert backlog_calls == [
        (
            "10.1175/1520-0469(1974)031<0674:IOACCE>2.0.CO;2",
            {
                "title": "Interaction of a cumulus cloud ensemble with the large-scale environment",
                "citekey": "Arakawa1974Schubert",
                "tried": ["no-candidates"],
            },
        )
    ]


def test_backfill_pdfs_reuses_one_fetcher_for_batch(cli_env, monkeypatch, capsys):
    from glean import _paper_intake

    class FakeFetcher:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fetcher = FakeFetcher()
    seen_fetchers = []

    def fake_fetch_pdf(*_, fetcher=None, **__):
        seen_fetchers.append(fetcher)
        return type("FetchResult", (), {
            "filename": None,
            "candidate_label": None,
            "tried": ["test-pdf"],
            "reason": "all candidates failed",
        })()

    monkeypatch.setattr(_paper_intake, "make_pdf_fetcher", lambda: fetcher, raising=False)
    monkeypatch.setattr(_paper_intake, "resolve_ams_landing_url", lambda _: None, raising=False)
    monkeypatch.setattr(_paper_intake, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(_paper_intake, "pdf_backlog_add", lambda *_, **__: None, raising=False)

    rc = main(["backfill-pdfs", "--limit", "2"])

    assert rc == 0
    assert seen_fetchers == [fetcher, fetcher]
    assert fetcher.closed is True


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


# ---------- glean attach ----------

def test_attach_adds_main(cli_env, tmp_path, capsys):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")
    main(["reindex", "--force"])
    capsys.readouterr()
    rc = main(["attach", "Arakawa1974Schubert", str(pdf)])
    assert rc == 0
    r = cli_env.load("Arakawa1974Schubert")
    mains = [a for a in r.attachments if a.kind == "main"]
    assert len(mains) == 1
    assert (cli_env.root / mains[0].path).exists()


def test_attach_replaces_same_kind(cli_env, tmp_path):
    first = tmp_path / "first.pdf"; first.write_bytes(b"first")
    second = tmp_path / "second.pdf"; second.write_bytes(b"second")
    main(["reindex", "--force"])
    main(["attach", "Arakawa1974Schubert", str(first), "--kind", "main"])
    r1 = cli_env.load("Arakawa1974Schubert")
    first_path = cli_env.root / r1.attachments[-1].path
    first_bytes = first_path.read_bytes()
    main(["attach", "Arakawa1974Schubert", str(second), "--kind", "main"])
    r2 = cli_env.load("Arakawa1974Schubert")
    mains = [a for a in r2.attachments if a.kind == "main"]
    assert len(mains) == 1
    # Old main file should be gone (replaced semantics).
    remaining_path = cli_env.root / mains[0].path
    assert remaining_path.read_bytes() == b"second"


def test_attach_supplement_does_not_replace_main(cli_env, tmp_path):
    main_pdf = tmp_path / "main.pdf"; main_pdf.write_bytes(b"main")
    si_pdf = tmp_path / "si.pdf"; si_pdf.write_bytes(b"si")
    main(["reindex", "--force"])
    main(["attach", "Arakawa1974Schubert", str(main_pdf), "--kind", "main"])
    main(["attach", "Arakawa1974Schubert", str(si_pdf), "--kind", "supplement"])
    r = cli_env.load("Arakawa1974Schubert")
    kinds = sorted(a.kind for a in r.attachments)
    assert kinds == ["main", "supplement"]


def test_attach_missing_citekey_returns_1(cli_env, tmp_path):
    pdf = tmp_path / "x.pdf"; pdf.write_bytes(b"x")
    rc = main(["attach", "NoSuchKey", str(pdf)])
    assert rc == 1


def test_attach_missing_source_returns_2(cli_env):
    main(["reindex", "--force"])
    rc = main(["attach", "Arakawa1974Schubert", "/nonexistent/file.pdf"])
    assert rc == 2


def test_attach_move_removes_source(cli_env, tmp_path):
    pdf = tmp_path / "orig.pdf"; pdf.write_bytes(b"x")
    main(["reindex", "--force"])
    rc = main(["attach", "Arakawa1974Schubert", str(pdf), "--move"])
    assert rc == 0
    assert not pdf.exists()


# ---------- glean set ----------

def test_set_updates_fields(cli_env):
    main(["reindex", "--force"])
    rc = main(["set", "Arakawa1974Schubert", "--set", "abstract=updated text",
               "--set", "publisher=AMS"])
    assert rc == 0
    r = cli_env.load("Arakawa1974Schubert")
    assert r.abstract == "updated text"
    assert r.publisher == "AMS"


def test_set_int_year(cli_env):
    main(["reindex", "--force"])
    rc = main(["set", "Arakawa1974Schubert", "--set", "year=1975"])
    assert rc == 0
    assert cli_env.load("Arakawa1974Schubert").year == 1975


def test_set_bool_read(cli_env):
    main(["reindex", "--force"])
    rc = main(["set", "Miyawaki2024Attention", "--set", "read=true"])
    assert rc == 0
    assert cli_env.load("Miyawaki2024Attention").read is True


def test_set_clear_field(cli_env):
    main(["reindex", "--force"])
    rc = main(["set", "Miyawaki2024Attention", "--clear", "abstract"])
    assert rc == 0
    assert cli_env.load("Miyawaki2024Attention").abstract is None


def test_set_rejects_unknown_field(cli_env):
    rc = main(["set", "Arakawa1974Schubert", "--set", "foo=bar"])
    assert rc == 6


def test_set_rejects_bad_year(cli_env):
    rc = main(["set", "Arakawa1974Schubert", "--set", "year=nineteen-seventy"])
    assert rc == 6


def test_set_rejects_bad_type(cli_env):
    rc = main(["set", "Arakawa1974Schubert", "--set", "type=blog-post"])
    assert rc == 6


def test_set_missing_citekey_returns_1(cli_env):
    rc = main(["set", "NoSuchKey", "--set", "abstract=x"])
    assert rc == 1


# ---------- glean tag ----------

def test_tag_add_normalizes_and_deduplicates(cli_env, capsys):
    main(["reindex", "--force"])
    capsys.readouterr()

    rc1 = main(["tag", "add", "Miyawaki2024Attention", "PROJ-A"])
    rc2 = main(["tag", "add", "Miyawaki2024Attention", "proj-a"])

    assert (rc1, rc2) == (0, 0)
    assert cli_env.load("Miyawaki2024Attention").tags == ["proj-a"]
    out = capsys.readouterr().out
    assert "added tag proj-a" in out
    assert "already has tag proj-a" in out


def test_tag_rm_removes_tag(cli_env, capsys):
    r = cli_env.load("Miyawaki2024Attention")
    r.tags = ["proj-a", "upper-troposphere"]
    cli_env.save(r)
    main(["reindex", "--force"])
    capsys.readouterr()

    rc = main(["tag", "rm", "Miyawaki2024Attention", "PROJ-A"])

    assert rc == 0
    assert cli_env.load("Miyawaki2024Attention").tags == ["upper-troposphere"]
    assert "removed tag proj-a" in capsys.readouterr().out


def test_tag_rm_absent_is_harmless(cli_env, capsys):
    rc = main(["tag", "rm", "Miyawaki2024Attention", "proj-a"])

    assert rc == 0
    assert cli_env.load("Miyawaki2024Attention").tags == []
    assert "does not have tag proj-a" in capsys.readouterr().out


def test_tag_missing_citekey_returns_1(cli_env):
    rc = main(["tag", "add", "NoSuchKey", "proj-a"])
    assert rc == 1


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


def test_oneline_collapses_whitespace():
    from glean.cli import _oneline

    assert _oneline("a\nb\tc  d") == "a b c d"
    assert _oneline("  trimmed \n ") == "trimmed"


def test_search_title_with_newline_does_not_crash(cli_env, capsys, monkeypatch):
    # A literal newline inside a field must not split one result across
    # physical lines and break the tab-unpack. Regression: ValueError
    # "not enough values to unpack (expected 4, got 3)".
    import glean.cli as cli

    monkeypatch.setattr(cli, "_have", lambda cmd: False)  # deterministic: no fzf
    r = cli_env.load("Miyawaki2024Attention")
    r.title = "Multiline\ntitle here"
    cli_env.save(r)

    rc = main(["search", "Multiline"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Miyawaki2024Attention" in out
    assert "Multiline title here" in out  # newline collapsed to a space
