# glean

A CLI/TUI reference manager. Plain-text YAML per record, SQLite FTS5 index, Textual TUI, OpenAlex/Crossref for citation data. Replaces Bookends for the author's workflow.

Design: [`DESIGN.md`](DESIGN.md).

## Install

```
./install/install.sh
```

This installs `glean` into a pipx venv, installs Playwright's Chromium browser for PDF fetching, and exposes the CLI on PATH.

## Status

Under construction. Build order:

1. Storage layer (`store.py`, `index.py`) with a fixture library
2. Bookends importer with dry-run
3. CLI
4. TUI
5. Citation-graph fetcher and queries
6. paper_intake worker.py swap to `glean add --quiet`
