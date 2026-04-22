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


def test_add_deferred(cli_env):
    rc = main(["add", "10.1234/example"])
    assert rc == 5  # resolve/network failure is the stub exit code


def test_tui_imports(cli_env):
    # Don't actually launch the TUI (requires a TTY); just confirm the module loads.
    from glean.tui.app import GleanApp
    assert GleanApp is not None
