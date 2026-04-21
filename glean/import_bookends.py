"""Importer: Bookends JSONL (from migration/dump/normalize.py) -> glean records.

Does not read Library.bdb. See DESIGN.md "Migration from Bookends".
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from glean.citekey import disambiguate, generate, is_valid
from glean.schema import Attachment, Author, Record, ValidationError, validate
from glean.store import Library, attachment_filename


BOOKENDS_ATTACHMENTS_DIR = Path(
    "~/Library/Mobile Documents/iCloud~com~sonnysoftware~bot/Documents/Attachments"
).expanduser()


# Case-insensitive substring patterns for 2nd+ attachments. First match wins.
_KIND_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("supplement",  ("supplement", "supporting", " si.pdf", "si -", "-si.", "_si.")),
    ("erratum",     ("erratum",)),
    ("corrigendum", ("corrigendum",)),
    ("slides",      ("slides", "presentation")),
    ("response",    ("response", "reviewer response")),
    ("dataset",     ("dataset", "data availability")),
]


def classify_extra_kind(filename: str) -> str:
    """Classify a non-first attachment by filename substring match."""
    lower = filename.lower()
    for kind, patterns in _KIND_PATTERNS:
        for p in patterns:
            if p in lower:
                return kind
    return "other"


_TRAIL_ID_RE = re.compile(r"\s+\d+$")


def _dedup_norm(name: str) -> str:
    """Normalize a filename for duplicate detection: strip .pdf, strip
    a trailing ' <digits>' (Bookends internal attachment id), case-fold."""
    stem = name
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    stem = _TRAIL_ID_RE.sub("", stem)
    return stem.casefold()


def dedup_extras(attachments: list[str]) -> list[str]:
    """Drop extra attachments that duplicate the main or an earlier-kept
    extra by normalized-stem equality or either-direction substring."""
    if len(attachments) <= 1:
        return list(attachments)
    kept = [attachments[0]]
    kept_norms = [_dedup_norm(attachments[0])]
    for extra in attachments[1:]:
        en = _dedup_norm(extra)
        duplicate = False
        for kn in kept_norms:
            if en == kn or en in kn or kn in en:
                duplicate = True
                break
        if not duplicate:
            kept.append(extra)
            kept_norms.append(en)
    return kept


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _pick_citekey(rec: dict, taken: set[str]) -> str:
    authors = rec.get("authors") or []
    family = authors[0]["family"] if authors else "Anon"
    year = rec.get("year") or 0
    title = rec.get("title") or ""
    base = generate(family, year, title)
    if not is_valid(base):
        base = f"Record{rec['_bookends_id']}"
    return disambiguate(base, taken)


def _build_record(rec: dict, citekey: str, today: date) -> Record:
    authors = [Author(family=a["family"], given=a.get("given")) for a in rec.get("authors", [])]
    attachments = [
        Attachment(path=a["path"], kind=a["kind"]) for a in rec.get("_resolved_attachments", [])
    ]
    added_at_str = rec.get("added_at")
    added_at = date.fromisoformat(added_at_str) if added_at_str else today
    return Record(
        citekey=citekey,
        type=rec["type"],
        title=rec["title"],
        authors=authors,
        year=rec["year"],
        added_at=added_at,
        modified_at=today,
        doi=rec.get("doi"),
        arxiv=rec.get("arxiv"),
        isbn=rec.get("isbn"),
        pmid=rec.get("pmid"),
        pmcid=rec.get("pmcid"),
        legacy_citekey=rec.get("legacy_citekey"),
        journal=rec.get("journal"),
        volume=rec.get("volume"),
        issue=rec.get("issue"),
        pages=rec.get("pages"),
        publisher=rec.get("publisher"),
        url=rec.get("url"),
        abstract=rec.get("abstract"),
        keywords=list(rec.get("keywords") or []),
        attachments=attachments,
        notes=rec.get("notes"),
        source="bookends-migration",
        source_fetched_at=today,
    )


def _assign_attachments(rec: dict, stub: Record, attach_names_in_use: dict[str, int]) -> list[Attachment]:
    """Resolve basename list from Bookends into glean-policy Attachment list.

    Does not copy files. Returns what the YAML should say; the real-run
    step that does the copying will use the same function to pick target
    names.
    """
    raw = rec.get("attachments") or []
    result: list[Attachment] = []
    for i, bookends_basename in enumerate(raw):
        kind = "main" if i == 0 else classify_extra_kind(bookends_basename)
        # Use collision_index only if the base filename is already in use.
        base = attachment_filename(stub, kind=kind)
        collision_index = None
        candidate = base
        while candidate in attach_names_in_use:
            attach_names_in_use[candidate] += 1
            collision_index = (collision_index or 1) + 1
            candidate = attachment_filename(stub, kind=kind, collision_index=collision_index)
        attach_names_in_use[candidate] = 1
        result.append(Attachment(
            path=f"attachments/{candidate}",
            kind=kind,
        ))
    return result


def import_jsonl(
    jsonl_path: Path,
    out_library: Library,
    *,
    dry_run: bool = True,
    copy_attachments: bool = False,
    bookends_attachments_dir: Path = BOOKENDS_ATTACHMENTS_DIR,
    today: Optional[date] = None,
) -> dict:
    """Convert a JSONL dump into glean records.

    Missing attachments are dropped from the resulting YAML (per migration
    decision) and logged in the returned `missing_attachments` list so the
    user can reconcile them later.
    """
    today = today or date.today()
    taken: set[str] = set()
    attach_names_in_use: dict[str, int] = {}
    counts = {"written": 0, "citekey_collisions": 0, "attachments_missing": 0,
              "validation_errors": 0}
    collisions: list[tuple[str, str]] = []
    problems: list[str] = []
    missing_attachments: list[tuple[str, str]] = []

    if not dry_run:
        out_library.ensure_dirs()

    for rec in _iter_jsonl(jsonl_path):
        citekey = _pick_citekey(rec, taken)
        base_for_citekey = generate(
            (rec.get("authors") or [{}])[0].get("family", "Anon"),
            rec.get("year") or 0,
            rec.get("title") or "",
        )
        if citekey != base_for_citekey:
            collisions.append((base_for_citekey, citekey))
            counts["citekey_collisions"] += 1
        taken.add(citekey)

        # Filter attachments: drop those whose source PDF doesn't exist.
        orig_basenames = rec.get("attachments") or []
        kept_basenames: list[str] = []
        for basename in orig_basenames:
            src = bookends_attachments_dir / basename
            if src.exists():
                kept_basenames.append(basename)
            else:
                counts["attachments_missing"] += 1
                missing_attachments.append((citekey, basename))

        # Drop Bookends-duplicate extras (same paper stored under multiple filenames).
        deduped = dedup_extras(kept_basenames)
        if len(deduped) < len(kept_basenames):
            counts.setdefault("attachments_deduped", 0)
            counts["attachments_deduped"] += len(kept_basenames) - len(deduped)
        rec_for_build = {**rec, "attachments": deduped}

        # Build a stub record (no attachments yet) to feed into attachment_filename.
        stub = _build_record({**rec_for_build, "_resolved_attachments": []}, citekey, today)
        resolved = _assign_attachments(rec_for_build, stub, attach_names_in_use)

        record = _build_record({**rec_for_build, "_resolved_attachments":
                                [{"path": a.path, "kind": a.kind} for a in resolved]},
                               citekey, today)
        try:
            validate(record)
        except ValidationError as e:
            counts["validation_errors"] += 1
            problems.append(f"{citekey}: validation failed: {e}")
            continue

        if not dry_run:
            out_library.save(record, today=today)
            if copy_attachments:
                for i, att in enumerate(resolved):
                    src = bookends_attachments_dir / (kept_basenames[i])
                    dst = out_library.root / att.path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        counts["written"] += 1

    return {
        "counts": counts,
        "collisions": collisions,
        "problems": problems,
        "missing_attachments": missing_attachments,
    }
