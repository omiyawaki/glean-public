from __future__ import annotations

from pathlib import Path

import pytest

from glean.index import Index
from glean.store import Library
from glean.tui.app import GleanApp


@pytest.fixture
def app(lib: Library, tmp_path: Path):
    idx = Index(lib, tmp_path / "index.sqlite")
    idx.rebuild()
    return GleanApp(lib, idx)


async def test_app_mounts_and_shows_rows(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#results", DataTable)
        assert table.row_count > 0  # fixture library has records


async def test_search_filters_results(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#results", DataTable)
        all_count = table.row_count
        # Default focus is the table; press `/` to move into the search input.
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press(*list("convection"))
        # Wait past the 150ms debounce + threaded FTS5 work.
        await pilot.pause(0.5)
        filtered = table.row_count
        assert filtered > 0
        assert filtered <= all_count


async def test_quit_exits(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        # Default focus is the table, so `q` triggers the quit binding directly.
        await pilot.press("q")
        await pilot.pause()


async def test_n_opens_notes_modal(app):
    """Regression: pressing `n` from the default view must open the notes modal.
    Was broken when startup focused the search Input (which absorbed 'n' as text)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        from glean.tui.app import NotesModal
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NotesModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NotesModal)


async def test_t_opens_keyword_modal(app):
    """Same focus-absorption class of bug: `t` must reach the app-level binding."""
    async with app.run_test() as pilot:
        await pilot.pause()
        from glean.tui.app import PromptModal
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, PromptModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PromptModal)


async def test_shift_r_opens_rename_modal(app):
    """Same focus-absorption class of bug: `R` must reach the app-level binding."""
    async with app.run_test() as pilot:
        await pilot.pause()
        from glean.tui.app import PromptModal
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, PromptModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PromptModal)
