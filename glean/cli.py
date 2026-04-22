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
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from glean.citekey import is_valid as citekey_is_valid
from glean.index import Index
from glean.schema import Attachment, Author, Record, ValidationError, validate
from glean.store import Library, attachment_filename


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

def cmd_open(args: argparse.Namespace) -> int:
    lib = _library()
    if not lib.exists(args.citekey):
        _err(f"not found: {args.citekey}")
        return 1
    r = lib.load(args.citekey)
    kind = args.kind or "main"
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
    # Load + rewrite the renamed record.
    r = lib.load(old)
    r.citekey = new
    lib.save(r)
    lib.delete(old)
    # Rewrite back-refs.
    for ck in cited_by:
        other = lib.load(ck)
        other.references = [new if t == old else t for t in other.references]
        lib.save(other)
    # Refresh index.
    idx.refresh()
    print(f"renamed {old} -> {new}  (rewrote refs in {len(cited_by)} records)")
    return 0


# ---------- search ----------

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def cmd_search(args: argparse.Namespace) -> int:
    lib = _library()
    idx = _ensure_indexed(lib)
    hits = idx.search(args.query, limit=args.limit)
    if not hits:
        return 1
    lines = [f"{ck}\t{title}\t{authors}\t{year or ''}" for ck, title, authors, year in hits]
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
        ck, title, authors, year = line.split("\t")
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
            "year": r.year, "doi": r.doi, "arxiv": r.arxiv,
            "isbn": r.isbn, "pmid": r.pmid, "pmcid": r.pmcid,
            "legacy_citekey": r.legacy_citekey,
            "journal": r.journal, "volume": r.volume, "issue": r.issue,
            "pages": r.pages, "publisher": r.publisher, "url": r.url,
            "abstract": r.abstract, "keywords": r.keywords,
            "attachments": [{"path": a.path, "kind": a.kind} for a in r.attachments],
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "modified_at": r.modified_at.isoformat() if r.modified_at else None,
            "read": r.read, "notes": r.notes,
            "references": r.references,
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
    _err("glean add (DOI/URL resolver) is not yet wired. Lands with the graph layer in step 6c.")
    return 5


def cmd_tui(args: argparse.Namespace) -> int:
    from glean.tui.app import GleanApp
    lib = _library()
    # Assume the index is current. Run `glean reindex` before launching if stale.
    idx = _index(lib)
    idx.connect()  # initialize the DB file without refreshing from YAMLs
    GleanApp(lib, idx).run()
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    _err("glean graph is not yet implemented. Lands in step 6c.")
    return 1


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

    addp = sub.add_parser("add", help="(deferred) resolve DOI/URL and add record")
    addp.add_argument("identifier", nargs="?")
    addp.add_argument("--quiet", action="store_true")
    addp.set_defaults(func=cmd_add)

    tuip = sub.add_parser("tui", help="(deferred) Textual TUI")
    tuip.set_defaults(func=cmd_tui)

    gp = sub.add_parser("graph", help="(deferred) citation graph operations")
    gp.add_argument("subcommand", nargs="?")
    gp.set_defaults(func=cmd_graph)

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
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
