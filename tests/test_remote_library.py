from __future__ import annotations

from pathlib import Path

import pytest

from glean.remote_library import (
    attachment_relpath,
    local_attachment_path,
    should_delegate,
)


RECORD = """\
citekey: Gilbert2025Observed
attachments:
  - path: attachments/Gilbert et al (2025) Observed winds.pdf
    kind: main
  - path: attachments/notes.pdf
    kind: supplement
"""


def test_attachment_relpath_picks_kind():
    assert attachment_relpath(RECORD, "main").endswith("Observed winds.pdf")
    assert attachment_relpath(RECORD, "supplement").endswith("notes.pdf")
    assert attachment_relpath(RECORD, "missing") is None


def test_local_attachment_path_stays_inside_library(tmp_path: Path):
    path = local_attachment_path("attachments/paper.pdf", root=tmp_path)
    assert path == (tmp_path / "attachments" / "paper.pdf").resolve()
    assert local_attachment_path("../outside.pdf", root=tmp_path) is None


def test_delegate_catalog_commands_on_the_mac(monkeypatch):
    monkeypatch.delenv("GLEAN_LIBRARY", raising=False)
    monkeypatch.delenv("GLEAN_LOCAL", raising=False)
    monkeypatch.setattr("glean.remote_library.sys.platform", "darwin")
    assert should_delegate(["add", "10.1029/2025gl116175"])
    assert should_delegate(["search", "arctic"])
    assert not should_delegate(["open", "Gilbert2025Observed"])
    assert not should_delegate(["--help"])


def test_explicit_library_stays_local(monkeypatch):
    monkeypatch.setenv("GLEAN_LIBRARY", "/tmp/glean")
    monkeypatch.setattr("glean.remote_library.sys.platform", "darwin")
    assert not should_delegate(["add", "10.1029/2025gl116175"])


def test_linux_host_stays_local(monkeypatch):
    monkeypatch.delenv("GLEAN_LIBRARY", raising=False)
    monkeypatch.delenv("GLEAN_LOCAL", raising=False)
    monkeypatch.setattr("glean.remote_library.sys.platform", "linux")
    assert not should_delegate(["add", "10.1029/2025gl116175"])


@pytest.mark.parametrize("argv", [["serve"], ["tui"], ["attach", "Key", "a.pdf"]])
def test_local_only_commands_are_not_delegated(monkeypatch, argv):
    monkeypatch.delenv("GLEAN_LIBRARY", raising=False)
    monkeypatch.delenv("GLEAN_LOCAL", raising=False)
    monkeypatch.setattr("glean.remote_library.sys.platform", "darwin")
    assert not should_delegate(argv)
