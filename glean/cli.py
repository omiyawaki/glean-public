"""CLI entry point.

See DESIGN.md "CLI surface" for the command table and exit codes.
Exit codes:
  0  success
  1  not found
  4  duplicate
  5  resolve/network failure
  6  validation error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from glean.citekey import is_valid as citekey_is_valid
from glean.index import Index
from glean.schema import (
    ATTACHMENT_KIND_ENUM, Attachment, Author, Record,
    ReferenceGraphState, ReferenceID, TYPE_ENUM, ValidationError,
    normalize_tag, validate,
)
from glean.remote_library import (
    LOCAL_PDF_ROOT,
    attachment_relpath,
    delegate,
    fetch_record_text,
    local_attachment_path,
    push_record_file,
    remote_catalog_enabled,
    should_delegate,
)
from glean.store import Library, attachment_filename, loads


DEFAULT_LIBRARY_ROOT = Path("~/Documents/glean").expanduser()
DEFAULT_INDEX_PATH = Path("~/Library/Caches/glean/index.sqlite").expanduser()


def _library() -> Library:
    root = Path(os.environ.get("GLEAN_LIBRARY", str(DEFAULT_LIBRARY_ROOT))).expanduser()
    return Library(root)


def _index_path() -> Path:
    p = os.environ.get("GLEAN_INDEX")
    return Path(p).expanduser() if p else DEFAULT_INDEX_PATH


def _index(lib: Library) -> Index:
    return Index(lib, _index_path())


def _ensure_indexed(lib: Library) -> Index:
    idx = _index(lib)
    idx.refresh()
    return idx


def _format_authors(authors: list[Author]) -> str:
    parts = []
    for a in authors:
        if a.given:
            parts.append(f"{a.family}, {a.given}")
        else:
            parts.append(a.family)
    return "; ".join(parts)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------- info ----------

def cmd_info(args: argparse.Namespace) -> int:
    lib = _library()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    r = lib.load(args.citekey)
    print(f"{r.citekey}  [{r.type}]")
    print(f"  {r.title}")
    print(f"  {_format_authors(r.authors)}  ({r.year})")
    if r.published_at:
        print(f"  published: {r.published_at.isoformat()}")
    bib = " ".join(filter(None, [
        r.journal,
        f"{r.volume}" if r.volume else None,
        f"({r.issue})" if r.issue else None,
        f", {r.pages}" if r.pages else None,
    ]))
    if bib.strip(): print(f"  {bib}")
    if r.publisher: print(f"  publisher: {r.publisher}")
    for key in ("doi", "arxiv", "isbn", "pmid", "pmcid", "legacy_citekey", "url"):
        v = getattr(r, key)
        if v: print(f"  {key}: {v}")
    if r.keywords:
        print(f"  keywords: {', '.join(r.keywords)}")
    if r.tags:
        print(f"  tags: {', '.join(r.tags)}")
    if r.attachments:
        print("  attachments:")
        for a in r.attachments:
            print(f"    [{a.kind}] {a.path}")
    if r.abstract:
        print("\n" + r.abstract)
    if r.notes:
        print(f"\nnotes:\n{r.notes}")
    return 0


# ---------- open ----------

def _open_remote(citekey: str, kind: str) -> int:
    if not citekey_is_valid(citekey):
        _err(f"not found: {citekey}")
        return 1
    text = fetch_record_text(citekey)
    if text is None:
        _err(f"not found: {citekey}")
        return 1
    rel = attachment_relpath(text, kind)
    if rel is None:
        _err(f"{citekey}: no attachment of kind {kind!r}")
        return 1
    path = local_attachment_path(rel)
    if path is None or not path.exists():
        shown = path if path is not None else rel
        _err(f"attachment file missing: {shown}")
        return 1
    subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    kind = args.kind or "main"
    if remote_catalog_enabled():
        return _open_remote(args.citekey, kind)
    lib = _library()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    r = lib.load(args.citekey)
    match = next((a for a in r.attachments if a.kind == kind), None)
    if match is None:
        _err(f"{r.citekey}: no attachment of kind {kind!r}")
        return 1
    path = lib.root / match.path
    if not path.exists():
        _err(f"attachment file missing: {path}")
        return 1
    subprocess.run(["open", str(path)], check=False)
    return 0


# ---------- edit ----------

def cmd_edit(args: argparse.Namespace) -> int:
    lib = _library()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    path = lib.path_for(args.citekey)
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)
    # Reload to refresh modified_at and re-save in canonical form.
    try:
        r = lib.load(args.citekey)
        lib.save(r)
    except ValidationError as e:
        _err(f"warning: {args.citekey} failed validation after edit: {e}")
        return 6
    _ensure_indexed(lib)
    return 0


# ---------- rm ----------

def cmd_rm(args: argparse.Namespace) -> int:
    lib = _library()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    lib.delete(args.citekey)
    _ensure_indexed(lib)
    print(f"removed {args.citekey}")
    return 0


# ---------- rename ----------

def cmd_rename(args: argparse.Namespace) -> int:
    lib = _library()
    old, new = args.old, args.new
    if not citekey_is_valid(new):
        _err(f"invalid citekey format: {new!r}")
        return 6
    if not lib.exists(old):
        _err(f"not found: {old}")
        return 1
    if lib.exists(new):
        _err(f"target already exists: {new}")
        return 4
    idx = _ensure_indexed(lib)
    # Find records whose references include the old citekey.
    conn = idx.connect()
    cited_by = [row[0] for row in conn.execute(
        "SELECT citekey FROM outgoing WHERE target = ?", (old,)
    )]
    with lib.mutation_lock():
        # Load + rewrite the renamed record.
        r = lib.load(old)
        r.citekey = new
        lib._save_unlocked(r)
        lib.path_for(old).unlink()
        # Rewrite back-refs.
        for ck in cited_by:
            other = lib.load(ck)
            other.references = [
                ReferenceID("citekey", new)
                if reference.kind == "citekey" and reference.value == old
                else reference
                for reference in other.references
            ]
            lib._save_unlocked(other)
    # Refresh index.
    idx.refresh()
    print(f"renamed {old} -> {new}  (rewrote refs in {len(cited_by)} records)")
    return 0


# ---------- search ----------

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _oneline(s: object) -> str:
    """Collapse internal whitespace (newlines, tabs, runs of spaces) to single
    spaces so a value is safe to embed in a tab-delimited, newline-joined line.
    Some records (e.g. IPCC reports) carry literal newlines inside the title,
    which would otherwise split one logical row across several physical lines."""
    return " ".join(str(s).split())


def cmd_search(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    hits = idx.search(args.query, limit=args.limit)
    if not hits:
        return 1
    lines = [
        f"{_oneline(ck)}\t{_oneline(title)}\t{_oneline(authors)}\t{year or ''}"
        for ck, title, authors, year in hits
    ]
    blob = "\n".join(lines)
    if _have("fzf"):
        try:
            res = subprocess.run(
                ["fzf", "--filter", args.query],
                input=blob, capture_output=True, text=True, check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                blob = res.stdout.rstrip("\n")
        except OSError:
            pass
    for line in blob.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) < 4:
            parts += [""] * (4 - len(parts))
        ck, title, authors, year = parts
        print(f"{ck:<40} [{year}]  {authors[:40]:<40}  {title[:80]}")
    return 0


# ---------- ls ----------

def cmd_ls(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    conn = idx.connect()
    sql = ["SELECT r.citekey, r.title, r.year FROM records r"]
    params: list = []
    joins: list[str] = []
    wheres: list[str] = []
    if args.author:
        joins.append("JOIN authors a ON a.citekey = r.citekey")
        wheres.append("lower(a.family) = lower(?)")
        params.append(args.author)
    if args.year:
        wheres.append("r.year = ?")
        params.append(int(args.year))
    if args.unread:
        wheres.append("r.read = 0")
    if args.tag:
        try:
            tag = normalize_tag(args.tag)
        except ValidationError as e:
            _err(f"invalid tag: {e}")
            return 6
        joins.append("JOIN tags t ON t.citekey = r.citekey")
        wheres.append("t.tag = ?")
        params.append(tag)
    if joins:
        sql.extend(joins)
    if wheres:
        sql.append("WHERE " + " AND ".join(wheres))
    sql.append("ORDER BY r.year DESC, r.citekey")
    for ck, title, year in conn.execute(" ".join(sql), params):
        print(f"{ck:<40} [{year or '----'}]  {title[:90]}")
    return 0


# ---------- export ----------

def _export_json(records: Iterable[Record]) -> str:
    out = []
    for r in records:
        out.append({
            "citekey": r.citekey, "type": r.type, "title": r.title,
            "authors": [{"family": a.family, "given": a.given} for a in r.authors],
            "year": r.year,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "doi": r.doi, "arxiv": r.arxiv,
            "isbn": r.isbn, "pmid": r.pmid, "pmcid": r.pmcid,
            "legacy_citekey": r.legacy_citekey,
            "journal": r.journal, "volume": r.volume, "issue": r.issue,
            "pages": r.pages, "publisher": r.publisher, "url": r.url,
            "abstract": r.abstract, "keywords": r.keywords, "tags": r.tags,
            "attachments": [{"path": a.path, "kind": a.kind} for a in r.attachments],
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "modified_at": r.modified_at.isoformat() if r.modified_at else None,
            "read": r.read, "notes": r.notes,
            "references": [reference.legacy_value for reference in r.references],
            "reference_graph": (
                {
                    "status": r.reference_graph.status,
                    "providers": r.reference_graph.providers,
                    "fetched_at": r.reference_graph.fetched_at.isoformat(),
                }
                if r.reference_graph else None
            ),
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


def _export_bibtex(records: Iterable[Record]) -> str:
    from pybtex.database import BibliographyData, Entry, Person

    type_to_bibtex = {
        "journal-article": "article",
        "preprint": "article",
        "book": "book",
        "chapter": "incollection",
        "thesis": "phdthesis",
        "report": "techreport",
        "conference": "inproceedings",
        "other": "misc",
    }
    entries = {}
    for r in records:
        fields = {}
        if r.title: fields["title"] = r.title
        if r.year: fields["year"] = str(r.year)
        if r.journal: fields["journal"] = r.journal
        if r.volume: fields["volume"] = r.volume
        if r.issue: fields["number"] = r.issue
        if r.pages: fields["pages"] = r.pages
        if r.publisher: fields["publisher"] = r.publisher
        if r.doi: fields["doi"] = r.doi
        if r.url: fields["url"] = r.url
        if r.isbn: fields["isbn"] = r.isbn
        persons = {"author": [
            Person(first=(a.given or ""), last=a.family) for a in r.authors
        ]}
        entries[r.citekey] = Entry(type_to_bibtex.get(r.type, "misc"), fields=fields,
                                    persons=persons)
    return BibliographyData(entries=entries).to_string("bibtex")


def cmd_export(args: argparse.Namespace) -> int:
    lib = _library()
    records: list[Record] = []
    if args.all:
        records = list(lib.iter_records())
    else:
        for ck in args.citekeys:
            if not lib.exists(ck):
                _err(f"not found: {ck}")
                return 1
            records.append(lib.load(ck))
    if args.format == "json":
        print(_export_json(records))
    elif args.format == "bibtex":
        print(_export_bibtex(records))
    else:
        _err(f"format not yet implemented: {args.format}")
        return 6
    return 0


# ---------- reindex ----------

def cmd_reindex(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _index(lib)
    if args.force:
        idx.rebuild()
        conn = idx.connect()
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        print(f"rebuilt: {n} records")
    else:
        added, updated, deleted = idx.refresh()
        print(f"refreshed: +{added} added, {updated} updated, -{deleted} deleted")
    return 0


# ---------- reconcile-attachments ----------

def cmd_reconcile_attachments(args: argparse.Namespace) -> int:
    lib = _library()
    dry = args.dry_run
    missing: list[tuple[str, str]] = []
    for r in lib.iter_records():
        for a in r.attachments:
            path = lib.root / a.path
            if not path.exists():
                missing.append((r.citekey, a.path))
    if missing:
        print(f"{len(missing)} missing attachment(s):")
        for ck, p in missing:
            print(f"  {ck}  {p}")
    else:
        print("all attachment paths resolve")
    if dry:
        return 0
    # Orphan-adoption not implemented in v1; reconcile-attachments in v1
    # only reports. The proposal in DESIGN.md for fuzzy adoption can land later.
    return 0


# ---------- import bookends ----------

def cmd_import_bookends(args: argparse.Namespace) -> int:
    from glean.import_bookends import import_jsonl
    lib = _library()
    jsonl = Path(args.jsonl).expanduser()
    if not jsonl.is_file():
        _err(f"jsonl not found: {jsonl}")
        return 1
    result = import_jsonl(
        jsonl, lib,
        dry_run=args.dry_run,
        copy_attachments=not args.dry_run and args.copy_attachments,
    )
    print(json.dumps(result["counts"], indent=2))
    if result["problems"]:
        print(f"problems: {len(result['problems'])}", file=sys.stderr)
        for p in result["problems"][:20]:
            print(f"  {p}", file=sys.stderr)
    if not args.dry_run:
        idx = _index(lib)
        idx.refresh()
    return 0


# ---------- deferred commands ----------

def cmd_add(args: argparse.Namespace) -> int:
    from glean import _paper_intake
    from glean.citekey import disambiguate, generate

    raw = args.identifier
    if not raw:
        if not args.quiet:
            _err("glean add: expected a DOI, arXiv ID, or URL")
        return 6

    doi = _paper_intake.normalize_identifier(raw)
    if not doi:
        if not args.quiet:
            _err(f"could not resolve identifier: {raw!r}")
        return 5

    lib = _library()
    idx = _index(lib)
    conn = idx.connect()
    # Duplicate check by DOI (case-insensitive per RFC 3986 resolution rules:
    # Bookends stores DOIs lowercase, Crossref often returns uppercase).
    existing = conn.execute(
        "SELECT citekey FROM records WHERE lower(doi) = lower(?)", (doi,)
    ).fetchone()
    if existing:
        if lib.exists(existing[0]):
            existing_record = lib.load(existing[0])
            if existing_record.reference_graph is None:
                idx.enqueue_graph_job(existing[0], priority=100)
            if existing_record.published_at is None:
                idx.enqueue_publication_job(existing[0], priority=100)
            print(existing[0])
            return 4
        # The index points at a citekey whose YAML is missing on disk. Refresh
        # before generating a citekey so the orphan row cannot influence the new
        # record or survive beside it.
        if not args.quiet:
            _err(
                f"warning: stale index entry for {existing[0]} "
                f"(yaml missing); re-creating record"
            )
        idx.refresh()

    meta = _paper_intake.crossref_metadata(doi)
    if not meta:
        if not args.quiet:
            _err(f"no metadata for {doi} (Crossref miss or network)")
        return 5

    authors_raw = meta.get("authors") or ""
    authors: list[Author] = []
    for line in authors_raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "," in line:
            fam, giv = line.split(",", 1)
            authors.append(Author(family=fam.strip(), given=giv.strip() or None))
        else:
            authors.append(Author(family=line, given=None))

    year_raw = meta.get("year") or ""
    try:
        year = int(year_raw)
    except (TypeError, ValueError):
        if not args.quiet:
            _err(f"no usable year for {doi}")
        return 6

    title = (meta.get("title") or "").strip()
    if not title or not authors:
        if not args.quiet:
            _err(f"insufficient metadata for {doi} (missing title or authors)")
        return 6

    is_arxiv = doi.startswith("10.48550/arXiv.")
    record_type = (
        "preprint" if _paper_intake.is_preprint_doi(doi) else "journal-article"
    )
    arxiv = doi.split("arXiv.", 1)[1] if is_arxiv else None
    published_at = None
    if meta.get("published_at"):
        try:
            published_at = date.fromisoformat(str(meta["published_at"]))
        except ValueError:
            if not args.quiet:
                _err(f"invalid publication date for {doi}: {meta['published_at']!r}")
            return 6

    existing_citekeys = {
        row[0] for row in conn.execute("SELECT citekey FROM records")
    }
    base = generate(authors[0].family, year, title)
    if not citekey_is_valid(base):
        base = f"Record{abs(hash(doi)) % 1_000_000}"
    citekey = disambiguate(base, existing_citekeys)

    record = Record(
        citekey=citekey,
        type=record_type,
        title=title,
        authors=authors,
        year=year,
        published_at=published_at,
        doi=doi,
        arxiv=arxiv,
        journal=meta.get("journal") or None,
        abstract=meta.get("abstract") or None,
        source="crossref",
        source_fetched_at=date.today(),
    )
    try:
        validate(record)
    except ValidationError as e:
        if not args.quiet:
            _err(f"validation error: {e}")
        return 6

    lib.save(record)
    idx._upsert(record)
    idx.enqueue_graph_job(citekey, priority=100)
    if record.published_at is None:
        idx.enqueue_publication_job(citekey, priority=100)
    _try_fetch_pdf_for_record(lib, idx, record, doi, quiet=args.quiet)
    print(citekey)
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from glean.tui.app import GleanApp
    lib = _library()
    # Assume the index is current. Run `glean reindex` before launching if stale.
    idx = _index(lib)
    idx.connect()  # initialize the DB file without refreshing from YAMLs
    GleanApp(lib, idx).run()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from glean.web import serve
    serve(host=args.host, port=args.port)
    return 0


# ---------- attach ----------

def _unique_attachment_name(lib: Library, record: Record, kind: str) -> str:
    """Return an attachment filename under the naming policy that doesn't
    collide with any existing file in lib/attachments/."""
    for i in [None, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
        name = attachment_filename(record, kind=kind, collision_index=i)
        if not (lib.root / "attachments" / name).exists():
            return name
    raise RuntimeError(f"too many attachment-name collisions for {record.citekey}")


def _attach_existing_file(
    lib: Library,
    idx: Index,
    record: Record,
    src: Path,
    *,
    kind: str = "main",
    move: bool = False,
) -> Path:
    """Attach an existing file to record and return the library path."""
    keep: list[Attachment] = []
    for a in record.attachments:
        if a.kind == kind:
            old = lib.root / a.path
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        else:
            keep.append(a)
    record.attachments = keep

    name = _unique_attachment_name(lib, record, kind)
    dst = lib.root / "attachments" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if move:
        try:
            src.unlink()
        except OSError:
            pass
    record.attachments.append(Attachment(path=f"attachments/{name}", kind=kind))
    lib.save(record)
    idx._upsert(record)
    return dst


def _attachment_collision_indices() -> list[Optional[int]]:
    return [None, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def _looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(5).startswith(b"%PDF-")
    except OSError:
        return False


def _existing_canonical_attachment_file(
    lib: Library,
    record: Record,
    *,
    kind: str = "main",
) -> Path | None:
    for collision_index in _attachment_collision_indices():
        candidate = lib.attachments_dir / attachment_filename(
            record,
            kind=kind,
            collision_index=collision_index,
        )
        if candidate.is_file() and os.access(candidate, os.R_OK) and _looks_like_pdf(candidate):
            return candidate
    return None


def _link_existing_library_attachment(
    lib: Library,
    idx: Index,
    record: Record,
    path: Path,
    *,
    kind: str = "main",
) -> Path:
    keep = [a for a in record.attachments if a.kind != kind]
    rel = path.relative_to(lib.root).as_posix()
    record.attachments = keep + [Attachment(path=rel, kind=kind)]
    lib.save(record)
    idx._upsert(record)
    return path


def _try_fetch_pdf_for_record(
    lib: Library,
    idx: Index,
    record: Record,
    doi: str,
    *,
    quiet: bool,
    fetcher: object | None = None,
) -> str:
    """Best-effort PDF fetch for records without a readable main PDF.

    PDF failures never change the add exit code; the record remains useful and
    paper_intake's backlog tracks the manual follow-up when available.
    """
    main = record.main_attachment()
    if main and _attachment_file_exists(lib, main):
        return "skipped"

    existing = _existing_canonical_attachment_file(lib, record)
    if existing is not None:
        dst = _link_existing_library_attachment(lib, idx, record, existing, kind="main")
        _clear_pdf_backlog(doi)
        if not quiet:
            _err(f"PDF linked for {record.citekey}: {dst}")
        return "attached"

    from glean import _paper_intake

    staging = Path(tempfile.mkdtemp(prefix="glean_pdf_"))
    tried: list[str] = []
    try:
        try:
            publisher_url = _paper_intake.resolve_ams_landing_url(doi) or f"https://doi.org/{doi}"
            result = _paper_intake.fetch_pdf(
                doi,
                publisher_url,
                record.title,
                fetcher=fetcher,
                dir_path=str(staging),
            )
            tried = list(getattr(result, "tried", []) or [])
            reason = getattr(result, "reason", "") or ""
            if not tried and reason == "no candidates":
                tried = ["no-candidates"]
        except Exception as exc:
            if not quiet:
                _err(f"warning: PDF fetch failed for {doi}: {exc}")
            return "failed"

        filename = getattr(result, "filename", None)
        if filename:
            src = staging / filename
            if src.is_file():
                dst = _attach_existing_file(lib, idx, record, src, kind="main", move=True)
                _clear_pdf_backlog(doi)
                if not quiet:
                    label = getattr(result, "candidate_label", None) or "unknown"
                    _err(f"PDF fetched for {record.citekey}: {dst} via {label}")
                return "attached"

        _queue_pdf_backlog(doi, record, tried)
        if not quiet:
            reason = getattr(result, "reason", "") or "no PDF candidate succeeded"
            _err(f"PDF not fetched for {doi}: {reason}")
        return "queued"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _queue_pdf_backlog(doi: str, record: Record, tried: list[str]) -> None:
    try:
        from glean import _paper_intake
        _paper_intake.pdf_backlog_add(
            doi,
            title=record.title,
            citekey=record.citekey,
            tried=tried,
        )
    except Exception:
        pass


def _clear_pdf_backlog(doi: str) -> None:
    try:
        from glean import _paper_intake
        _paper_intake.pdf_backlog_remove(doi)
    except Exception:
        pass


def _attachment_file_exists(lib: Library, attachment: Attachment) -> bool:
    path = Path(attachment.path).expanduser()
    full = path if path.is_absolute() else lib.root / path
    return full.is_file()


def _record_missing_readable_main_pdf(lib: Library, record: Record) -> bool:
    main = record.main_attachment()
    return bool(record.doi and (main is None or not _attachment_file_exists(lib, main)))


def _missing_main_pdf_records(lib: Library) -> list[Record]:
    return [
        record
        for record in lib.iter_records()
        if _record_missing_readable_main_pdf(lib, record)
    ]


def cmd_backfill_pdfs(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _index(lib)

    if args.limit is not None and args.limit < 0:
        _err("--limit must be non-negative")
        return 6

    if args.citekey:
        idx.connect()
        if not lib.exists(args.citekey):
            _err(f"not found: {args.citekey}")
            return 1
        record = lib.load(args.citekey)
        records = [record] if _record_missing_readable_main_pdf(lib, record) else []
    else:
        idx.refresh()
        records = _missing_main_pdf_records(lib)

    if args.limit is not None:
        records = records[:args.limit]

    if args.dry_run:
        print(f"dry-run: {len(records)} record(s) would be attempted")
        for record in records:
            print(f"{record.citekey}\t{record.doi}\t{record.title}")
        return 0

    counts = {"attached": 0, "queued": 0, "failed": 0, "skipped": 0}
    fetcher = None
    if records:
        try:
            from glean import _paper_intake
            fetcher = _paper_intake.make_pdf_fetcher()
        except Exception as exc:
            _err(f"warning: could not initialize reusable PDF fetcher: {exc}")
    try:
        for record in records:
            assert record.doi is not None
            status = _try_fetch_pdf_for_record(
                lib,
                idx,
                record,
                record.doi,
                quiet=False,
                fetcher=fetcher,
            )
            counts[status] = counts.get(status, 0) + 1
            print(f"{status}\t{record.citekey}\t{record.doi}", flush=True)
    finally:
        if fetcher is not None:
            try:
                fetcher.close()
            except Exception:
                pass

    print(
        "summary: "
        f"attempted={len(records)} "
        f"attached={counts['attached']} "
        f"queued={counts['queued']} "
        f"failed={counts['failed']} "
        f"skipped={counts['skipped']}"
    )
    return 0


def _attach_remote(args: argparse.Namespace) -> int:
    """Put the PDF in the Mac attachment folder and the record on glean-server."""
    citekey = args.citekey
    if not citekey_is_valid(citekey):
        _err(f"not found: {citekey}")
        return 1
    text = fetch_record_text(citekey)
    if text is None:
        _err(f"not found: {citekey}")
        return 1
    src = Path(args.path).expanduser()
    if not src.is_file():
        _err(f"source file not found: {src}")
        return 2
    record = loads(text)
    name = _unique_attachment_name(Library(LOCAL_PDF_ROOT), record, args.kind)
    dst = LOCAL_PDF_ROOT / "attachments" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if args.move:
        try:
            src.unlink()
        except OSError:
            pass
    record.attachments = [
        a for a in record.attachments if a.kind != args.kind
    ]
    record.attachments.append(Attachment(path=f"attachments/{name}", kind=args.kind))
    with tempfile.TemporaryDirectory() as tmp:
        lib = Library(tmp)
        lib.save(record)
        rc = push_record_file(citekey, lib.path_for(citekey))
    if rc != 0:
        _err("failed to update the library on glean-server")
        return 1
    print(str(dst))
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    if remote_catalog_enabled():
        return _attach_remote(args)
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    src = Path(args.path).expanduser()
    if not src.is_file():
        _err(f"source file not found: {src}")
        return 2
    r = lib.load(args.citekey)
    dst = _attach_existing_file(lib, idx, r, src, kind=args.kind, move=args.move)
    print(str(dst))
    return 0


# ---------- set ----------

_SETTABLE = {
    "doi", "arxiv", "isbn", "pmid", "pmcid", "legacy_citekey",
    "journal", "volume", "issue", "pages", "publisher", "url",
    "abstract", "notes", "year", "published_at", "title", "read", "type",
}


def cmd_set(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    r = lib.load(args.citekey)

    for pair in args.set_ or []:
        if "=" not in pair:
            _err(f"--set expects key=value, got {pair!r}")
            return 6
        key, value = pair.split("=", 1)
        if key not in _SETTABLE:
            _err(f"field not settable: {key!r} (allowed: {sorted(_SETTABLE)})")
            return 6
        if key == "year":
            try:
                value = int(value)
            except ValueError:
                _err(f"year must be an integer, got {value!r}")
                return 6
        elif key == "published_at":
            try:
                value = date.fromisoformat(value)
            except ValueError:
                _err(f"published_at must be an ISO date, got {value!r}")
                return 6
        elif key == "read":
            value = value.lower() in ("true", "1", "yes", "y")
        elif key == "type":
            if value not in TYPE_ENUM:
                _err(f"invalid type {value!r}; allowed: {sorted(TYPE_ENUM)}")
                return 6
        setattr(r, key, value)

    for key in args.clear or []:
        if key not in _SETTABLE:
            _err(f"field not settable: {key!r}")
            return 6
        setattr(r, key, False if key == "read" else None)

    try:
        validate(r)
    except ValidationError as e:
        _err(f"validation error: {e}")
        return 6
    lib.save(r)
    idx._upsert(r)
    return 0


# ---------- tag ----------

def _mutate_tag(args: argparse.Namespace, *, add: bool) -> int:
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    try:
        tag = normalize_tag(args.tag)
    except ValidationError as e:
        _err(f"invalid tag: {e}")
        return 6

    r = lib.load(args.citekey)
    if add:
        if tag in r.tags:
            print(f"{r.citekey} already has tag {tag}")
            return 0
        r.tags.append(tag)
        lib.save(r)
        idx._upsert(r)
        print(f"added tag {tag} to {r.citekey}")
        return 0

    if tag not in r.tags:
        print(f"{r.citekey} does not have tag {tag}")
        return 0
    r.tags = [existing for existing in r.tags if existing != tag]
    lib.save(r)
    idx._upsert(r)
    print(f"removed tag {tag} from {r.citekey}")
    return 0


def cmd_tag_add(args: argparse.Namespace) -> int:
    return _mutate_tag(args, add=True)


def cmd_tag_rm(args: argparse.Namespace) -> int:
    return _mutate_tag(args, add=False)


# ---------- graph ----------

def cmd_graph_out(args: argparse.Namespace) -> int:
    from glean.graph.queries import outgoing_for, Stub
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    for item in outgoing_for(lib, idx, args.citekey):
        if isinstance(item, Stub):
            title = item.title or "(no metadata yet; run: glean graph fetch)"
            print(f"  [stub] {item.target:<50}  {title[:60]}")
        else:
            print(f"  [lib]  {item.citekey:<50}  {item.title[:60]}")
    return 0


def cmd_graph_in(args: argparse.Namespace) -> int:
    from glean.graph.queries import incoming_for
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    r = lib.load(args.citekey)
    for other in incoming_for(lib, idx, r):
        print(f"  {other.citekey:<50}  {other.title[:60]}")
    return 0


def cmd_graph_suggest(args: argparse.Namespace) -> int:
    from glean.graph.queries import unread_cited_by_read
    lib = _library()
    idx = _index(lib)
    idx.connect()
    results = unread_cited_by_read(lib, idx, min_citations=args.min_citations)
    for stub, n in results:
        title = stub.title or "(no metadata yet)"
        print(f"  ×{n:<3} {stub.target:<45}  {title[:60]}")
    return 0


def _do_graph_fetch_one(citekey: str, lib: Library, idx: Index) -> int:
    """Populate references: for a single record. 0=ok, 5=network, 1=not found, 6=no DOI."""
    from glean.graph import openalex, crossref
    from glean.graph.worker import GraphWorker
    if not lib.exists(citekey):
        return 1
    r = lib.load(citekey)
    if not r.doi:
        return 6
    worker = GraphWorker(
        lib,
        idx,
        fetch_openalex=openalex.fetch,
        fetch_crossref=crossref.fetch,
    )
    try:
        worker.enrich(citekey)
    except Exception:
        return 5
    idx.finish_graph_job(citekey)
    return 0


def cmd_graph_fetch(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _index(lib)
    idx.connect()
    if args.all:
        _err(
            "warning: graph fetch --all is deprecated; use "
            "glean graph backfill and the background worker"
        )
        cutoff = None
        if args.since:
            cutoff = date.fromisoformat(args.since)
        errors = 0
        for r in lib.iter_records():
            if not r.doi:
                continue
            if (
                cutoff
                and r.reference_graph
                and r.reference_graph.fetched_at >= cutoff
            ):
                continue  # already fetched recently
            rc = _do_graph_fetch_one(r.citekey, lib, idx)
            if rc == 0:
                print(f"  ok   {r.citekey}")
            elif rc == 5:
                errors += 1
                print(f"  miss {r.citekey}", file=sys.stderr)
        return 5 if errors else 0
    else:
        if not args.citekey:
            _err("glean graph fetch: pass <citekey> or --all")
            return 6
        rc = _do_graph_fetch_one(args.citekey, lib, idx)
        if rc == 1:
            _err(f"not found: {args.citekey}")
        elif rc == 5:
            _err(f"network/resolve failure for {args.citekey}")
        elif rc == 6:
            _err(f"{args.citekey} has no DOI; cannot fetch")
        return rc


def cmd_graph_status(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    idx.reconcile_graph_jobs()
    print(json.dumps(idx.graph_status(), indent=2, sort_keys=True))
    return 0


def cmd_graph_backfill(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    query = (
        "SELECT citekey FROM records WHERE doi IS NOT NULL "
        "AND reference_status IS NULL ORDER BY added_at DESC, citekey"
    )
    params: tuple[object, ...] = ()
    if args.limit is not None:
        query += " LIMIT ?"
        params = (args.limit,)
    citekeys = [row[0] for row in idx.connect().execute(query, params)]
    if args.dry_run:
        print(f"would queue {len(citekeys)} paper(s)")
        return 0
    for citekey in citekeys:
        idx.enqueue_graph_job(citekey, priority=10)
    print(f"queued {len(citekeys)} paper(s)")
    return 0


def cmd_graph_pause(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    idx.set_graph_paused(True)
    print("citation graph worker paused")
    return 0


def cmd_graph_resume(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    idx.set_graph_paused(False)
    idx.reconcile_graph_jobs()
    print("citation graph worker resumed")
    return 0


def cmd_graph_retry(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    if not idx.enqueue_graph_job(args.citekey, priority=100):
        _err(f"{args.citekey} has no DOI; cannot fetch")
        return 6
    print(f"queued {args.citekey}")
    return 0


def cmd_dates_status(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    idx.reconcile_publication_jobs()
    print(json.dumps(idx.publication_status(), indent=2, sort_keys=True))
    return 0


def cmd_dates_backfill(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    query = (
        "SELECT citekey FROM records WHERE doi IS NOT NULL "
        "AND published_at IS NULL ORDER BY year DESC, added_at DESC, citekey"
    )
    params: tuple[object, ...] = ()
    if args.limit is not None:
        query += " LIMIT ?"
        params = (args.limit,)
    citekeys = [row[0] for row in idx.connect().execute(query, params)]
    if args.dry_run:
        print(f"would queue {len(citekeys)} paper(s)")
        return 0
    for citekey in citekeys:
        idx.enqueue_publication_job(citekey, priority=10)
    print(f"queued {len(citekeys)} paper(s)")
    return 0


def cmd_dates_pause(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    idx.set_publication_paused(True)
    print("publication-date worker paused")
    return 0


def cmd_dates_resume(args: argparse.Namespace) -> int:
    idx = _ensure_indexed(_library())
    idx.set_publication_paused(False)
    idx.reconcile_publication_jobs()
    print("publication-date worker resumed")
    return 0


def cmd_dates_retry(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    if not idx.enqueue_publication_job(args.citekey, priority=100):
        record = lib.load(args.citekey)
        if record.published_at is not None:
            _err(f"{args.citekey} already has a publication date")
        else:
            _err(f"{args.citekey} has no DOI; cannot fetch")
        return 6
    print(f"queued {args.citekey}")
    return 0


def cmd_graph_viz(args: argparse.Namespace) -> int:
    from pyvis.network import Network
    from glean.graph.queries import outgoing_for, Stub

    lib = _library()
    idx = _index(lib)
    idx.connect()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1

    depth = max(1, args.depth)
    seen: set[str] = set()
    frontier: list[tuple[str, int]] = [(args.citekey, 0)]
    net = Network(height="750px", width="100%", directed=True, notebook=False)
    while frontier:
        node, d = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        try:
            r = lib.load(node)
            net.add_node(node, label=node, title=r.title, color="#4e79a7")
        except FileNotFoundError:
            net.add_node(node, label=node, color="#bab0ab")
        if d >= depth:
            continue
        for item in outgoing_for(lib, idx, node):
            if isinstance(item, Stub):
                net.add_node(item.target, label=item.target[:30],
                             title=item.title or "", color="#e15759")
                net.add_edge(node, item.target)
            else:
                net.add_edge(node, item.citekey)
                frontier.append((item.citekey, d + 1))
    out_path = Path(args.output).expanduser() if args.output else Path(f"{args.citekey}.html")
    net.write_html(str(out_path), open_browser=False, notebook=False)
    print(str(out_path))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    # Routed via subparser; this is a fallback.
    _err("glean graph: use `out`, `in`, `fetch`, `suggest`, or `viz`")
    return 6


# ---------- argparse wiring ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glean", description="plain-text reference manager")
    sub = p.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="pretty-print a record")
    info.add_argument("citekey")
    info.set_defaults(func=cmd_info)

    openp = sub.add_parser("open", help="open an attachment via macOS `open`")
    openp.add_argument("citekey")
    openp.add_argument("--kind", default="main")
    openp.set_defaults(func=cmd_open)

    editp = sub.add_parser("edit", help="open the YAML in $EDITOR")
    editp.add_argument("citekey")
    editp.set_defaults(func=cmd_edit)

    rmp = sub.add_parser("rm", help="delete a record")
    rmp.add_argument("citekey")
    rmp.set_defaults(func=cmd_rm)

    renp = sub.add_parser("rename", help="rename a citekey, rewriting ref pointers")
    renp.add_argument("old")
    renp.add_argument("new")
    renp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("search", help="full-text search, ranked")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_search)

    lsp = sub.add_parser("ls", help="list records with optional filters")
    lsp.add_argument("--year")
    lsp.add_argument("--author", help="family name, case-insensitive exact match")
    lsp.add_argument("--tag", help="tag slug, normalized before matching")
    lsp.add_argument("--unread", action="store_true")
    lsp.set_defaults(func=cmd_ls)

    exp = sub.add_parser("export", help="export records to bibtex/json")
    exp.add_argument("--format", choices=("bibtex", "ris", "json"), default="json")
    exp.add_argument("--all", action="store_true")
    exp.add_argument("citekeys", nargs="*")
    exp.set_defaults(func=cmd_export)

    rix = sub.add_parser("reindex", help="refresh the SQLite index")
    rix.add_argument("--force", action="store_true", help="drop and rebuild from scratch")
    rix.set_defaults(func=cmd_reindex)

    rec = sub.add_parser("reconcile-attachments", help="verify attachment paths exist")
    rec.add_argument("--dry-run", action="store_true")
    rec.set_defaults(func=cmd_reconcile_attachments)

    atp = sub.add_parser("attach", help="attach a file to a record")
    atp.add_argument("citekey")
    atp.add_argument("path")
    atp.add_argument("--kind", default="main", choices=sorted(ATTACHMENT_KIND_ENUM))
    atp.add_argument("--move", action="store_true",
                     help="delete the source file after copy")
    atp.set_defaults(func=cmd_attach)

    setp = sub.add_parser("set", help="set or clear fields on a record")
    setp.add_argument("citekey")
    setp.add_argument("--set", dest="set_", action="append", metavar="key=value",
                      help="set a field; repeatable")
    setp.add_argument("--clear", action="append", metavar="field",
                      help="null out a field; repeatable")
    setp.set_defaults(func=cmd_set)

    tagp = sub.add_parser("tag", help="add or remove tags on a record")
    tagsub = tagp.add_subparsers(dest="tag_sub", required=True)
    tagadd = tagsub.add_parser("add", help="add a tag to a record")
    tagadd.add_argument("citekey")
    tagadd.add_argument("tag")
    tagadd.set_defaults(func=cmd_tag_add)
    tagrm = tagsub.add_parser("rm", aliases=["remove"], help="remove a tag from a record")
    tagrm.add_argument("citekey")
    tagrm.add_argument("tag")
    tagrm.set_defaults(func=cmd_tag_rm)

    addp = sub.add_parser("add", help="(deferred) resolve DOI/URL and add record")
    addp.add_argument("identifier", nargs="?")
    addp.add_argument("--quiet", action="store_true")
    addp.set_defaults(func=cmd_add)

    bfp = sub.add_parser(
        "backfill-pdfs",
        help="attempt PDF fetch for DOI records missing a readable main PDF",
    )
    bfp.add_argument("--dry-run", action="store_true")
    bfp.add_argument("--limit", type=int, help="maximum records to attempt")
    bfp.add_argument("--citekey", help="attempt one eligible record")
    bfp.set_defaults(func=cmd_backfill_pdfs)

    tuip = sub.add_parser("tui", help="(deferred) Textual TUI")
    tuip.set_defaults(func=cmd_tui)

    srvp = sub.add_parser("serve", help="run the HTTP API for the macOS frontend")
    srvp.add_argument("--host", default="0.0.0.0")
    srvp.add_argument("--port", type=int, default=8766)
    srvp.set_defaults(func=cmd_serve)

    dp = sub.add_parser("dates", help="publication-date metadata operations")
    dsub = dp.add_subparsers(dest="dates_sub", required=True)

    dstatus = dsub.add_parser("status", help="show publication-date coverage")
    dstatus.set_defaults(func=cmd_dates_status)

    dbackfill = dsub.add_parser("backfill", help="queue missing publication dates")
    dbackfill.add_argument("--dry-run", action="store_true")
    dbackfill.add_argument("--limit", type=int)
    dbackfill.set_defaults(func=cmd_dates_backfill)

    dpause = dsub.add_parser("pause", help="pause automatic publication-date lookup")
    dpause.set_defaults(func=cmd_dates_pause)

    dresume = dsub.add_parser("resume", help="resume automatic publication-date lookup")
    dresume.set_defaults(func=cmd_dates_resume)

    dretry = dsub.add_parser("retry", help="queue one paper for publication-date lookup")
    dretry.add_argument("citekey")
    dretry.set_defaults(func=cmd_dates_retry)

    gp = sub.add_parser("graph", help="citation graph operations")
    gpsub = gp.add_subparsers(dest="graph_sub", required=True)

    gout = gpsub.add_parser("out", help="list outgoing references")
    gout.add_argument("citekey")
    gout.set_defaults(func=cmd_graph_out)

    gin = gpsub.add_parser("in", help="list incoming citations")
    gin.add_argument("citekey")
    gin.set_defaults(func=cmd_graph_in)

    gsug = gpsub.add_parser("suggest", help="unread papers cited by read ones")
    gsug.add_argument("--min-citations", type=int, default=2)
    gsug.set_defaults(func=cmd_graph_suggest)

    gfetch = gpsub.add_parser("fetch", help="populate references: via OpenAlex/Crossref")
    gfetch.add_argument("citekey", nargs="?")
    gfetch.add_argument("--all", action="store_true")
    gfetch.add_argument("--since", help="skip records fetched on or after this ISO date")
    gfetch.set_defaults(func=cmd_graph_fetch)

    gstatus = gpsub.add_parser("status", help="show citation enrichment coverage")
    gstatus.set_defaults(func=cmd_graph_status)

    gbackfill = gpsub.add_parser("backfill", help="queue missing citation graphs")
    gbackfill.add_argument("--dry-run", action="store_true")
    gbackfill.add_argument("--limit", type=int)
    gbackfill.set_defaults(func=cmd_graph_backfill)

    gpause = gpsub.add_parser("pause", help="pause automatic citation enrichment")
    gpause.set_defaults(func=cmd_graph_pause)

    gresume = gpsub.add_parser("resume", help="resume automatic citation enrichment")
    gresume.set_defaults(func=cmd_graph_resume)

    gretry = gpsub.add_parser("retry", help="queue one paper for citation enrichment")
    gretry.add_argument("citekey")
    gretry.set_defaults(func=cmd_graph_retry)

    gviz = gpsub.add_parser("viz", help="export a pyvis HTML graph")
    gviz.add_argument("citekey")
    gviz.add_argument("--depth", type=int, default=1)
    gviz.add_argument("--output", help="output HTML path (default: <citekey>.html)")
    gviz.set_defaults(func=cmd_graph_viz)

    ipp = sub.add_parser("import", help="batch import from external sources")
    ipsub = ipp.add_subparsers(dest="source", required=True)
    bkp = ipsub.add_parser("bookends", help="import from a Bookends-dumped JSONL file")
    bkp.add_argument("jsonl")
    bkp.add_argument("--dry-run", action="store_true")
    bkp.add_argument("--copy-attachments", action="store_true",
                     help="also copy PDFs from Bookends attachments dir")
    bkp.set_defaults(func=cmd_import_bookends)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args_list = sys.argv[1:] if argv is None else list(argv)
    if should_delegate(args_list):
        return delegate(args_list)
    parser = _build_parser()
    args = parser.parse_args(args_list)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
