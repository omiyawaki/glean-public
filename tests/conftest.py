from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from glean.index import Index
from glean.store import Library


FIXTURE_LIBRARY = Path(__file__).parent / "fixtures" / "library"


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
