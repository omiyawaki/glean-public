# glean — v1 design

A CLI/TUI reference manager with a plain-text source of truth and a citation-graph layer. Replaces Bookends for my workflow. Python, YAML-per-record, SQLite FTS5 index, Textual TUI, OpenAlex/Crossref for citation data.

This document fixes the shape of the v1. It does not cover features past the seven required ones in the spec.

## Data model

One YAML file per record. The filename is the citekey plus `.yaml`. The citekey is also the primary key throughout the rest of the system.

### Schema

```yaml
citekey: Miyawaki2024Attention      # required, unique, [A-Za-z0-9_-]+
type: journal-article               # required, enum
title: "Attention and convection"   # required
authors:                            # required, ordered
  - family: Miyawaki
    given: Osamu
  - family: Vallis
    given: Geoffrey
year: 2024                          # required, int
doi: 10.1175/JCLI-D-23-0456.1       # optional but strongly preferred
arxiv: "2401.12345"                 # optional, string (preserves leading zeros)
journal: Journal of Climate         # optional
volume: 37
issue: 4
pages: 1234-1245
publisher: AMS
url: https://journals.ametsoc.org/...
abstract: |
  Free-form multi-line text.
keywords: [attention, convection, transformers]
attachments:                        # optional, list; main is the primary PDF
  - path: attachments/Miyawaki (2024) Attention and convection.pdf
    kind: main
  - path: attachments/Miyawaki (2024) Attention and convection - SI.pdf
    kind: supplement
added_at: 2026-04-20                # required, ISO date
modified_at: 2026-04-20             # required, ISO date; authoritative for index staleness
read: false                         # optional, bool; drives "unread cited by read" query
notes: |
  Free-form markdown. Survives round-trips through the editor.
references:                         # outgoing citations; each entry is DOI or citekey
  - 10.1126/science.1234567         # DOI (contains a "/")
  - Sellers1965PhysicalClimatology  # citekey (for DOI-less sources like textbooks)
source: openalex                    # optional, provenance of references field
source_fetched_at: 2026-04-18       # optional, ISO date
```

Enum for `type`: `journal-article`, `preprint`, `book`, `chapter`, `thesis`, `report`, `conference`, `other`. Same names OpenAlex uses, with `preprint` carved out because it matters for supersession logic that paper_intake already does. OpenAlex itself has no `preprint` type, so the importer and graph fetcher detect preprints by DOI prefix (`10.48550/arXiv.*`, `10.21203/*`, `10.1101/*` for bioRxiv, and so on). This logic already lives in `paper_intake/resolver.py` and will be reused, not reimplemented.

Required fields are the six that every record must have to be useful: `citekey`, `type`, `title`, `authors`, `year`, `added_at`. `modified_at` is also required but the store maintains it automatically. Everything else is optional and simply absent when unknown. The importer from Bookends will populate what it can and leave the rest out rather than emitting empty strings.

Attachment kinds: `main`, `supplement`, `erratum`, `corrigendum`, `slides`, `response`, `dataset`, `other`. At most one `main` per record. See the "Attachment filenames" section for the naming policy.

### Citekey rules

Generated as `<FirstAuthorFamily><Year><FirstContentWord>`, stripped of accents and non-ASCII, CamelCased. On collision: append `a`, `b`, `c`. Auto-generated keys are sometimes clunky (truncated titles, missing diacritics) or wrong (wrong author ordering in the source), so citekeys are renameable through an explicit subcommand: `glean rename <old> <new>`. Renames are not a side effect of editing the YAML; the `citekey:` field in the YAML is conceptually derived from the filename.

`glean rename` is transactional: it validates that the new key is well-formed and unique, renames `records/<old>.yaml` to `records/<new>.yaml`, scans every other record's `references:` for citekey-form pointers to `<old>` and rewrites them to `<new>`, and updates the SQLite index. It does not touch attachment filenames, which follow their own author/year/title policy and are unaffected by citekey changes.

### Why YAML, not JSON or TOML

YAML round-trips through an editor cleanly: multi-line abstracts and notes stay readable, the diff in git is line-oriented, and the field order is stable if I pick a canonical serializer (`ruamel.yaml` with a fixed key order). JSON would need escaping for every newline in the abstract; TOML is worse for nested lists like authors.

## Directory layout

### Library (iCloud-synced)

```
~/Documents/glean/
├── records/            # one <citekey>.yaml per paper; ~5,600 files at migration
└── attachments/        # PDFs, human-readable filenames; see "Attachment filenames"
```

The library root is configurable. Default `~/Documents/glean/` because `~/Documents/` is already where my synced infra docs live and the Files app on iOS already browses it. No nested year/author folders: the flat `records/` directory is easier to grep, rsync, and reason about. At 30K entries lifetime (the upper bound I'd ever hit), a flat directory is still fine on APFS.

v1 has no library-level config file. I'll add `.glean/config.yaml` if and when a setting needs to vary per-library.

### Attachment filenames

PDFs are named for humans, not citekeys. iOS Files.app browsing and Finder Spotlight are first-class access modes, and search-as-you-type on "Miyawaki 2024" is what I actually want.

Rules:

- Single author: `<Family> (<Year>) <Title>.pdf`
- Two authors: `<Family1> and <Family2> (<Year>) <Title>.pdf`
- Three or more: `<Family1> et al (<Year>) <Title>.pdf`
- Title truncated at 80 chars, or at the first colon, whichever comes first
- Strip filesystem-hostile characters (`/`, control chars); keep parens, spaces, hyphens, diacritics
- Collision on the bare base: append ` (2)`, ` (3)` before `.pdf`
- Multi-attachment: append ` - SI`, ` - erratum`, ` - corrigendum`, ` - slides`, ` - response`, ` - dataset`, ` - other` before `.pdf` (the `main` kind has no suffix)

Drift is the obvious risk: rename in Finder, YAML goes stale. Mitigation is `glean reconcile-attachments [--dry-run]`, which walks `records/`, verifies each `attachments[].path` exists, regenerates the filename from YAML metadata when a file is missing, and offers to adopt orphan PDFs in `attachments/` by fuzzy match on family name, year, and title.

### Derived state (local, not synced)

```
~/Library/Caches/glean/
├── index.sqlite        # FTS5 + metadata tables, rebuilt from YAMLs
├── openalex/           # per-DOI JSON cache, 30-day TTL
└── crossref/
```

SQLite in iCloud Drive is unreliable: iCloud can sync a half-written journal file mid-transaction. Keeping the index local and rebuilding from YAMLs on demand avoids that class of bug.

### Repo layout

```
~/tools/glean/
├── DESIGN.md           # this file
├── README.md
├── pyproject.toml
├── install/
│   └── install.sh
├── glean/              # package
│   ├── __init__.py
│   ├── cli.py          # argparse/click entrypoints
│   ├── store.py        # YAML read/write
│   ├── index.py        # SQLite FTS5 build/query
│   ├── schema.py       # dataclasses + validation
│   ├── citekey.py
│   ├── bibtex.py       # pybtex wrapper
│   ├── import_bookends.py
│   ├── tui/
│   │   └── app.py      # Textual app
│   └── graph/
│       ├── __init__.py
│       ├── openalex.py
│       ├── crossref.py
│       └── queries.py
└── tests/
    ├── fixtures/       # ~20 hand-crafted YAMLs
    └── test_*.py
```

## SQLite index schema

Derived, rebuilt from YAMLs. The authoritative staleness signal is the YAML's `modified_at` field, not filesystem mtime. Mtimes get clobbered by rsync, git checkout, and occasionally iCloud sync, so the store writes `modified_at` into the YAML body on every save and the index compares that against its stored value.

```sql
CREATE TABLE records (
  citekey         TEXT PRIMARY KEY,
  yaml_path       TEXT NOT NULL UNIQUE,
  yaml_modified   TEXT NOT NULL,   -- from YAML's modified_at, ISO date
  title           TEXT NOT NULL,
  year            INTEGER,
  type            TEXT,
  doi             TEXT,
  arxiv           TEXT,
  added_at        TEXT,
  read            INTEGER DEFAULT 0
);
CREATE INDEX idx_records_doi ON records(doi) WHERE doi IS NOT NULL;
CREATE INDEX idx_records_year ON records(year);

CREATE TABLE authors (
  citekey TEXT, ord INTEGER, family TEXT, given TEXT,
  PRIMARY KEY (citekey, ord),
  FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX idx_authors_family ON authors(family);

CREATE TABLE keywords (
  citekey TEXT, keyword TEXT,
  PRIMARY KEY (citekey, keyword),
  FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);

CREATE TABLE attachments (
  citekey TEXT, ord INTEGER, path TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY (citekey, ord),
  FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX idx_attachments_kind ON attachments(citekey, kind);

-- Outgoing refs. target is either a DOI (contains "/") or a citekey (does not).
CREATE TABLE outgoing (
  citekey TEXT NOT NULL,
  target  TEXT NOT NULL,
  PRIMARY KEY (citekey, target),
  FOREIGN KEY (citekey) REFERENCES records(citekey) ON DELETE CASCADE
);
CREATE INDEX idx_outgoing_target ON outgoing(target);

-- External papers referenced by my library but not in it. Populated from
-- OpenAlex responses and from DOI-less citekey references in the importer.
-- Exactly one of (doi, citekey) is non-null.
CREATE TABLE stubs (
  doi          TEXT UNIQUE,
  citekey      TEXT UNIQUE,        -- for synthetic-citekey stubs (no DOI)
  title        TEXT,
  year         INTEGER,
  authors_flat TEXT,                -- "Miyawaki O; Vallis G"
  CHECK (doi IS NOT NULL OR citekey IS NOT NULL)
);

-- Full-text search over the stuff I actually search for.
CREATE VIRTUAL TABLE fts USING fts5(
  citekey UNINDEXED, title, abstract, keywords, authors_flat,
  tokenize='porter unicode61'
);
```

Incoming citations for record `R` with DOI `D` and citekey `C` are `SELECT citekey FROM outgoing WHERE target IN (?, ?)`, passing `D` and `C`. Not a stored table; cheap with the index on `target`.

Resolving a reference target: if the string contains `/` it's a DOI; look it up in `records.doi`, then fall back to `stubs.doi`. Otherwise it's a citekey; look it up in `records.citekey`, then fall back to `stubs.citekey`.

## CLI surface

```
glean add <doi | arxiv-id | url> [--quiet]    # fetch + write YAML
glean add --bib <file>                        # import a .bib
glean rm <citekey>                            # delete YAML + cascade
glean rename <old> <new>                      # transactional; rewrites ref pointers
glean edit <citekey>                          # open YAML in $EDITOR
glean info <citekey>                          # pretty-print metadata
glean open <citekey> [--kind main|supplement|erratum|...]   # macOS `open`
glean search <query>                          # FTS5 + fzf, one-line-per-hit
glean ls [--year YYYY] [--author FAMILY] [--unread]
glean export [--format bibtex|ris|json] [--all | <citekey>...]
glean reindex [--force]
glean reconcile-attachments [--dry-run]
glean tui                                     # launch Textual TUI

glean graph fetch <citekey>                   # populate references: via OpenAlex
glean graph fetch --all [--since DATE]        # batch fill
glean graph out <citekey>                     # list outgoing, resolve stubs
glean graph in  <citekey>                     # list incoming
glean graph viz <citekey> [--depth N]         # pyvis HTML
glean graph suggest [--min-citations N]       # unread cited by read

glean import bookends <export.bib> [--dry-run] [--attachments DIR]
```

Exit codes:

- `0` success
- `1` not found (citekey lookup failed)
- `4` duplicate (add found an existing record with the same DOI or equivalent identifier)
- `5` resolve or network failure (couldn't turn input into metadata; OpenAlex/Crossref unreachable)
- `6` validation error (bad input format, YAML schema violation)

`glean add --quiet` suppresses stdout and relies entirely on exit codes. This is the contract paper_intake will depend on (see "Integration contract" below).

`glean reindex` without flags scans for YAMLs not in the index, drops rows for YAMLs that no longer exist, and refreshes rows whose `modified_at` changed. `--force` drops the index and rebuilds from scratch. Per-query staleness checks mean `reindex` is rarely needed by hand; it exists for recovery and for post-migration sanity.

The CLI is a thin layer over the package; every command has a Python entrypoint in `glean/cli.py` that tests can drive directly.

## TUI layout

One screen, Textual. Three regions:

```
┌─ glean ────────────────────────────────────────────────────┐
│ /: attention convection                                     │
├──────────────────────────────┬──────────────────────────────┤
│ Miyawaki2024Attention  2024  │ Attention and convection     │
│ Vallis2019AOFD         2019  │ Miyawaki O, Vallis G (2024)  │
│ Held2019Radiative      2019  │ Journal of Climate 37(4)     │
│ ...                          │                              │
│                              │ Abstract:                    │
│                              │   We examine ...             │
│                              │                              │
│                              │ [Keywords] attention, ...    │
├──────────────────────────────┴──────────────────────────────┤
│ ↵ open · e edit · r refs · c cited-by · / search · q quit  │
└─────────────────────────────────────────────────────────────┘
```

Search is live. Each keystroke runs an FTS5 `MATCH` query with a prefix token, then pipes the candidate set (one line per hit, tab-separated: `citekey \t title \t authors_flat \t year`) through `fzf --filter <query>` as a subprocess. The final ranking is fzf's; FTS5's job is to narrow the candidate pool so fzf isn't scoring the whole library on every keystroke. Both stages always run, even when FTS5 returns many hits. Selection moves the preview pane. Enter calls `open` on the `main` attachment. `r` and `c` swap the result list for the selected paper's outgoing or incoming citations, with stubs shown in a dimmer style.

No multi-pane splits, no tabs. A single flat view is enough.

## Citation graph module

```python
# glean/graph/__init__.py
from glean.graph.openalex import fetch as openalex_fetch
from glean.graph.crossref import fetch as crossref_fetch
from glean.graph.queries import (
    incoming_for, outgoing_for, unread_cited_by_read, shortest_path,
)
```

`openalex_fetch(doi)` returns a normalized dict of metadata plus a list of outgoing reference DOIs. Crossref is the fallback when OpenAlex has no record or no references for the DOI. Both cache raw JSON under `~/Library/Caches/glean/<source>/<doi>.json` with a 30-day TTL.

`incoming_for(citekey)` and `outgoing_for(citekey)` return a mix of `Record` (in library) and `Stub` (not in library) objects. Stubs carry whatever metadata the source gave us, usually title and year.

`unread_cited_by_read(min_citations=2)` is the query that motivates the graph layer. It returns stubs that are cited by at least `min_citations` records where `read=true`, sorted by citation count. This is the "what should I read next" query.

`shortest_path(a, b)` uses networkx over the outgoing edges. Optional, cheap, useful for visualizations.

Reference population is lazy: `glean add` does not fetch references by default. The user (or paper_intake, later) calls `glean graph fetch` when the graph matters. This keeps `add` fast and keeps network failures out of the critical path.

## Integration contract

Two downstream tools depend on glean. Their interfaces are fixed now even though the code swap happens in step 7.

**paper_intake/worker.py** currently calls Bookends via AppleScript and relies on a duplicate-returns-empty-string convention. The replacement is `glean add --quiet <doi-or-arxiv>`. Exit codes drive the caller: `0` means added, `4` means duplicate (existing record found — this is the normal case paper_intake sees for already-known papers), `5` means resolve failure (network or not-found), `6` means validation error. paper_intake's dedup, supersession, PDF fetch, and relevance filter layers all stay; only the ~300 LOC in `worker.py` that calls Bookends gets swapped for subprocess calls to `glean add --quiet`.

**bibmatch** (`~/Documents/Work/reviews/tools/bibmatch.py`) currently indexes from Bookends. Rather than letting it read `records/*.yaml` directly — which would couple it to glean's file layout — it consumes `glean export --format json --all`. The JSON format is part of glean's stable interface: one object per record with the full metadata, suitable for bibmatch to reindex on its own cadence. If I change the YAML schema later, the JSON export can stay stable.

## Migration from Bookends

Path: Bookends → BibTeX via AppleScript → `glean import bookends`.

```applescript
tell application "Bookends"
  set theRefs to publication items of library window 1
  set bibtex to «event ToySRIS» theRefs given string:"BibTeX"
end tell
```

The importer does not touch the `.bdb` file. It reads the BibTeX export and the attachments directory.

Before writing the importer I need to verify one thing against a real Bookends export: does Bookends' BibTeX emit a `file = {path}` field that ties each record to its PDF? If yes, attachment mapping is deterministic. If no, the importer falls back to fuzzy matching on author/year/title against filenames in the attachments directory, which is lossy. The safe fallback in that case is a small companion AppleScript that emits `<bookends-id>\t<attachment-filename>` alongside the BibTeX dump, so the importer has an authoritative mapping. I'll test the BibTeX output on one record before committing to an importer design.

Steps:

1. Export all Bookends records to `~/Documents/glean/migration/bookends-2026-04-20.bib` via AppleScript. Check whether the export includes `file = {...}`; if not, emit the companion tab-separated map.
2. Copy Bookends' attachments folder to `~/Documents/glean/migration/attachments-src/`. (Actual path confirmed at migration time; on my MacBook it's likely under `~/Library/Mobile Documents/iCloud~com~sonnysoftware~bot/Documents/Attachments/`.)
3. Run `glean import bookends migration/bookends-2026-04-20.bib --attachments migration/attachments-src/ --dry-run`. This prints a report: how many records parsed, how many citekeys collided, how many attachments matched, which records have no DOI, which have fields glean doesn't know how to map.
4. Inspect the report, fix anything systematic in the importer or the BibTeX, then run without `--dry-run`. The importer writes YAMLs into `records/` and copies attachments into `attachments/` under the author/year/title naming policy.
5. Run `glean reindex --force` and spot-check with the TUI and a few `glean search` queries.

Bookends stays untouched through this. The migration directory is a staging area; I can delete it after verification. paper_intake keeps writing to Bookends until step 7 of the build.

## Git workflow

The library is "git-trackable," but the working tree does not live inside a git repo in iCloud Drive. `.git/` is a pile of small object files that iCloud's optimize-storage eviction and re-fetch loop handles badly, and the repo would constantly have merge conflicts on the SQLite-like internal state files.

Setup: library at `~/Documents/glean/` (iCloud-synced, no `.git/`). A separate mirror at `~/repos/glean-library/` holds the version history. A launchd job runs daily (or on demand via `glean snapshot`) and does, roughly:

```
rsync -a --delete ~/Documents/glean/ ~/repos/glean-library/working/
cd ~/repos/glean-library && git add -A && git commit -m "snapshot $(date -I)"
```

Snapshot granularity is daily, not per-change. That's enough for "undo my mistake from last week" and for pushing to a private GitHub remote as backup. Finer granularity would require hooking every `glean` write, which isn't worth it for v1.

## Caveats

- **iCloud "optimize storage" eviction.** iCloud can evict local copies of YAMLs and re-fetch on access, causing short stalls the first time a record is touched. With ~5,600 small files this is probably imperceptible, but if first-access latency shows up as a TUI hitch, the workaround is marking the library root with `brctl evict` off or pinning via the Files app.
- **modified_at authority vs filesystem mtime.** The index compares YAML `modified_at` against its stored value. Rewriting a YAML without updating `modified_at` (hand-editing and saving, for example) leaves the index stale. The store's write path always bumps `modified_at`; `glean edit` opens the YAML in `$EDITOR`, so hand edits also trigger the bump via a post-save hook that reads the new file and updates `modified_at` before the index refresh. Raw edits from outside `glean` require a `glean reindex` (or `--force`) to pick up.
- **Bookends attachment path discovery.** The `~/Library/Application Support/Bookends/Bookends/Attachments/` path above is a placeholder; recent Bookends versions store attachments under iCloud Drive. I'll confirm the real path during step 5 (importer work).

## What v1 does not include

No web UI. No ingestion from email or RSS (that stays in paper_intake). No automatic PDF download (paper_intake's `pdf_fetch.py` stays authoritative). No collaborative editing. No conflict resolution beyond what git gives me. No iPad editing UI; iOS access is read-only browsing via Files.app.
