"""YAML read/write for records, plus attachment naming policy.

Source of truth is one YAML per record under <library>/records/.
Writes always bump the modified_at field; callers rely on it for index staleness.

See DESIGN.md "Data model" and "Attachment filenames".
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from glean.schema import Attachment, Author, Record, validate


CANONICAL_ORDER = [
    "citekey", "type", "title", "authors", "year",
    "doi", "arxiv", "journal", "volume", "issue", "pages", "publisher", "url",
    "abstract", "keywords", "attachments",
    "added_at", "modified_at", "read",
    "notes", "references", "source", "source_fetched_at",
]

_KIND_SUFFIX = {
    "main": "",
    "supplement": " - SI",
    "erratum": " - erratum",
    "corrigendum": " - corrigendum",
    "slides": " - slides",
    "response": " - response",
    "dataset": " - dataset",
    "other": " - other",
}

_FS_HOSTILE = re.compile(r"[\x00-\x1f/]")


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.allow_unicode = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _multiline(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    if "\n" in s:
        return LiteralScalarString(s)
    return s


def _record_to_commented_map(r: Record) -> CommentedMap:
    m = CommentedMap()
    m["citekey"] = r.citekey
    m["type"] = r.type
    m["title"] = r.title

    authors = CommentedSeq()
    for a in r.authors:
        am = CommentedMap()
        am["family"] = a.family
        if a.given is not None:
            am["given"] = a.given
        authors.append(am)
    m["authors"] = authors

    m["year"] = r.year

    for field_name in ("doi", "arxiv", "journal", "volume", "issue", "pages", "publisher", "url"):
        v = getattr(r, field_name)
        if v is not None:
            m[field_name] = v

    if r.abstract is not None:
        m["abstract"] = _multiline(r.abstract)

    if r.keywords:
        kw = CommentedSeq(r.keywords)
        kw.fa.set_flow_style()
        m["keywords"] = kw

    if r.attachments:
        atts = CommentedSeq()
        for a in r.attachments:
            am = CommentedMap()
            am["path"] = a.path
            am["kind"] = a.kind
            atts.append(am)
        m["attachments"] = atts

    m["added_at"] = r.added_at
    m["modified_at"] = r.modified_at
    if r.read:
        m["read"] = True

    if r.notes is not None:
        m["notes"] = _multiline(r.notes)

    if r.references:
        refs = CommentedSeq(list(r.references))
        m["references"] = refs

    if r.source is not None:
        m["source"] = r.source
    if r.source_fetched_at is not None:
        m["source_fetched_at"] = r.source_fetched_at

    return m


def _record_from_dict(d: dict) -> Record:
    authors_raw = d.get("authors") or []
    authors = [Author(family=a["family"], given=a.get("given")) for a in authors_raw]

    attachments_raw = d.get("attachments") or []
    attachments = [Attachment(path=a["path"], kind=a["kind"]) for a in attachments_raw]

    added_at = d.get("added_at")
    if isinstance(added_at, str):
        added_at = date.fromisoformat(added_at)
    modified_at = d.get("modified_at")
    if isinstance(modified_at, str):
        modified_at = date.fromisoformat(modified_at)
    source_fetched_at = d.get("source_fetched_at")
    if isinstance(source_fetched_at, str):
        source_fetched_at = date.fromisoformat(source_fetched_at)

    return Record(
        citekey=d["citekey"],
        type=d["type"],
        title=d["title"],
        authors=authors,
        year=d["year"],
        added_at=added_at,
        modified_at=modified_at,
        doi=d.get("doi"),
        arxiv=str(d["arxiv"]) if d.get("arxiv") is not None else None,
        journal=d.get("journal"),
        volume=str(d["volume"]) if d.get("volume") is not None else None,
        issue=str(d["issue"]) if d.get("issue") is not None else None,
        pages=str(d["pages"]) if d.get("pages") is not None else None,
        publisher=d.get("publisher"),
        url=d.get("url"),
        abstract=d.get("abstract"),
        keywords=list(d.get("keywords") or []),
        attachments=attachments,
        read=bool(d.get("read", False)),
        notes=d.get("notes"),
        references=list(d.get("references") or []),
        source=d.get("source"),
        source_fetched_at=source_fetched_at,
    )


def dumps(record: Record) -> str:
    validate(record)
    buf = io.StringIO()
    _yaml().dump(_record_to_commented_map(record), buf)
    return buf.getvalue()


def loads(text: str) -> Record:
    data = _yaml().load(text)
    record = _record_from_dict(dict(data))
    validate(record)
    return record


def _ascii_family(family: str) -> str:
    # Keep diacritics per policy; only strip fs-hostile characters.
    return _FS_HOSTILE.sub("", family).strip()


def _truncate_title(title: str) -> str:
    cut = title.find(":")
    if cut == -1:
        cut = len(title)
    cut = min(cut, 80)
    return title[:cut].rstrip()


def attachment_filename(
    record: Record,
    kind: str = "main",
    collision_index: Optional[int] = None,
) -> str:
    if kind not in _KIND_SUFFIX:
        raise ValueError(f"unknown attachment kind: {kind!r}")

    families = [_ascii_family(a.family) for a in record.authors]
    if len(families) == 1:
        author_part = families[0]
    elif len(families) == 2:
        author_part = f"{families[0]} and {families[1]}"
    else:
        author_part = f"{families[0]} et al"

    title_part = _FS_HOSTILE.sub("", _truncate_title(record.title))
    base = f"{author_part} ({record.year}) {title_part}"
    if collision_index is not None and collision_index >= 2:
        base = f"{base} ({collision_index})"
    return f"{base}{_KIND_SUFFIX[kind]}.pdf"


class Library:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()
        self.records_dir = self.root / "records"
        self.attachments_dir = self.root / "attachments"

    def ensure_dirs(self) -> None:
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, citekey: str) -> Path:
        return self.records_dir / f"{citekey}.yaml"

    def exists(self, citekey: str) -> bool:
        return self.path_for(citekey).is_file()

    def load(self, citekey: str) -> Record:
        path = self.path_for(citekey)
        return loads(path.read_text(encoding="utf-8"))

    def save(self, record: Record, *, today: Optional[date] = None) -> None:
        self.ensure_dirs()
        record.modified_at = today or date.today()
        if record.added_at is None:
            record.added_at = record.modified_at
        self.path_for(record.citekey).write_text(dumps(record), encoding="utf-8")

    def delete(self, citekey: str) -> None:
        self.path_for(citekey).unlink()

    def iter_records(self) -> Iterator[Record]:
        if not self.records_dir.is_dir():
            return
        for p in sorted(self.records_dir.glob("*.yaml")):
            yield loads(p.read_text(encoding="utf-8"))
