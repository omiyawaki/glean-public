from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from glean.index import Index
from glean.store import Library


FIXTURE_LIBRARY = Path(__file__).parent / "fixtures" / "library"

_PAPER_INTAKE_DIR = Path(
    os.environ.get("PAPER_INTAKE_DIR", "~/tools/paper_intake")
).expanduser()

# A few tests exercise the optional sibling project that provides DOI
# resolution (see glean/_paper_intake.py). Skip them when it is not checked out.
requires_paper_intake = pytest.mark.skipif(
    not (_PAPER_INTAKE_DIR / "resolver.py").exists(),
    reason="optional paper_intake checkout not found (set PAPER_INTAKE_DIR)",
)


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    dest = tmp_path / "library"
    shutil.copytree(FIXTURE_LIBRARY, dest)
    return Library(dest)


@pytest.fixture
def index(lib: Library, tmp_path: Path) -> Index:
    idx = Index(lib, tmp_path / "index.sqlite")
    idx.connect()
    return idx
