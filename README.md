# glean

A command-line and terminal-UI reference manager for academic papers. Each
paper is one plain-text YAML file; a SQLite full-text index is derived from
those files; citation data comes from OpenAlex and Crossref.

I built it to replace a closed-source reference manager with something I can
script, diff, sync and back up like source code. The YAML files are the source
of truth, so the library survives any tool change, and the index can always be
rebuilt from them.

## Features

- **Plain-text records.** One YAML file per paper, keyed by a citekey, with
  a fixed key order so diffs stay small. The exact schema is in `DESIGN.md`.
- **Fast search.** SQLite FTS5 index kept outside the synced folder and
  rebuilt on demand; staleness is detected from each record's `modified_at`.
- **Citation graph.** A background worker fetches references from OpenAlex
  and Crossref and answers queries such as "unread papers cited by papers I
  have read".
- **Attachments.** PDFs are stored with human-readable filenames
  (author, year, title) so they are browsable in Finder and Files.
- **Terminal UI** built with Textual, and a small HTTP service used by the
  companion macOS app ([glean-mac](https://github.com/omiyawaki/glean-mac-public)).
- **Import and export.** BibTeX export, JSON export for other tools, and an
  importer from a Bookends dump.

## Install and run

Requires Python 3.11 or newer.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
export GLEAN_LIBRARY=$PWD/tests/fixtures/library   # or any directory of records
export GLEAN_INDEX=/tmp/glean-index.sqlite          # derived index; safe to delete
glean reindex
glean ls
glean search convection
glean tui
```

`install/install.sh` is the installer I use on macOS (shared Pixi environment,
Playwright Chromium for PDF fetching, LaunchAgent for the web service). It
assumes that layout; set `GLEAN_WEB_HOST` to the Mac that should run the web
service.

### Configuration

| Variable | Purpose | Default |
|---|---|---|
| `GLEAN_LIBRARY` | directory of record YAML files | `~/Documents/glean` |
| `GLEAN_INDEX` | derived SQLite index | `~/Library/Caches/glean/index.sqlite` |
| `GLEAN_MAILTO` | contact address sent to Crossref and OpenAlex (polite pool); set your own | `user@example.com` |
| `GLEAN_HOST`, `GLEAN_REMOTE_RECORDS` | optional SSH host and records path for a remote library | `glean-server`, `/srv/glean/records` |
| `PAPER_INTAKE_DIR` | optional checkout of the sibling intake project | `~/tools/paper_intake` |

`glean serve` exposes read-only HTTP endpoints for the Mac app with no
authentication; run it only on a network you trust.

## Tests

```bash
pytest
```

The suite has about 200 tests. Six of them (the `glean add` paths) need the
optional intake project that provides DOI resolution (see `PAPER_INTAKE_DIR`)
and are skipped when it is not present.

## Layout

```
glean/
  cli.py          argparse entry points
  store.py        YAML read/write, attachment naming
  index.py        SQLite FTS5 build and query
  schema.py       dataclasses and validation
  citekey.py      citekey generation
  graph/          OpenAlex / Crossref clients, queue worker, queries
  tui/app.py      Textual UI
  web.py          HTTP service for the Mac app
tests/            pytest suite with a small fixture library
DESIGN.md         data model and interface decisions
```

## License

MIT. See `LICENSE`.
