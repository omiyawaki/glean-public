"""YAML read/write for records, plus attachment naming policy.

Source of truth is one YAML per record under <library>/records/.
Writes always bump the modified_at field; callers rely on it for index staleness.

See DESIGN.md "Data model" and "Attachment filenames".
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import io
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from glean.schema import (
    Attachment,
    Author,
    Record,
    ReferenceGraphState,
    ReferenceID,
    normalize_tag,
    normalize_tags,
    validate,
)


CANONICAL_ORDER = [
    "citekey", "type", "title", "authors", "year", "published_at",
    "doi", "arxiv", "isbn", "pmid", "pmcid", "legacy_citekey",
    "journal", "volume", "issue", "pages", "publisher", "url",
    "abstract", "keywords", "tags", "attachments",
    "added_at", "modified_at", "read",
    "notes", "references", "reference_graph", "source", "source_fetched_at",
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
_TRANSIENT_READ_ERRNOS = {
    errno.EAGAIN,
    errno.EBUSY,
    getattr(errno, "EDEADLK", errno.EAGAIN),
}
_READ_ATTEMPTS = 4
_READ_RETRY_DELAY_S = 0.05
_SF_DATALESS = 0x40000000
_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_MUTATION_LOCKS_GUARD = threading.Lock()


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.allow_unicode = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    y.representer.ignore_aliases = lambda *_: True
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
    if r.published_at is not None:
        m["published_at"] = r.published_at

    for field_name in (
        "doi", "arxiv", "isbn", "pmid", "pmcid", "legacy_citekey",
        "journal", "volume", "issue", "pages", "publisher", "url",
    ):
        v = getattr(r, field_name)
        if v is not None:
            m[field_name] = v

    if r.abstract is not None:
        m["abstract"] = _multiline(r.abstract)

    if r.keywords:
        kw = CommentedSeq(r.keywords)
        kw.fa.set_flow_style()
        m["keywords"] = kw

    if r.tags:
        tags = CommentedSeq(r.tags)
        tags.fa.set_flow_style()
        m["tags"] = tags

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
        refs = CommentedSeq([reference.serialized for reference in r.references])
        m["references"] = refs

    if r.reference_graph is not None:
        graph = CommentedMap()
        graph["status"] = r.reference_graph.status
        providers = CommentedSeq(r.reference_graph.providers)
        providers.fa.set_flow_style()
        graph["providers"] = providers
        graph["fetched_at"] = r.reference_graph.fetched_at
        m["reference_graph"] = graph

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

    published_at = d.get("published_at")
    if isinstance(published_at, str):
        published_at = date.fromisoformat(published_at)
    added_at = d.get("added_at")
    if isinstance(added_at, str):
        added_at = date.fromisoformat(added_at)
    modified_at = d.get("modified_at")
    if isinstance(modified_at, str):
        modified_at = date.fromisoformat(modified_at)
    source_fetched_at = d.get("source_fetched_at")
    if isinstance(source_fetched_at, str):
        source_fetched_at = date.fromisoformat(source_fetched_at)
    graph_raw = d.get("reference_graph")
    reference_graph = None
    if graph_raw:
        fetched_at = graph_raw.get("fetched_at")
        if isinstance(fetched_at, str):
            fetched_at = date.fromisoformat(fetched_at)
        reference_graph = ReferenceGraphState(
            status=str(graph_raw["status"]),
            providers=list(graph_raw.get("providers") or []),
            fetched_at=fetched_at,
        )

    return Record(
        citekey=d["citekey"],
        type=d["type"],
        title=d["title"],
        authors=authors,
        year=d["year"],
        published_at=published_at,
        added_at=added_at,
        modified_at=modified_at,
        doi=d.get("doi"),
        arxiv=str(d["arxiv"]) if d.get("arxiv") is not None else None,
        isbn=d.get("isbn"),
        pmid=str(d["pmid"]) if d.get("pmid") is not None else None,
        pmcid=d.get("pmcid"),
        legacy_citekey=d.get("legacy_citekey"),
        journal=d.get("journal"),
        volume=str(d["volume"]) if d.get("volume") is not None else None,
        issue=str(d["issue"]) if d.get("issue") is not None else None,
        pages=str(d["pages"]) if d.get("pages") is not None else None,
        publisher=d.get("publisher"),
        url=d.get("url"),
        abstract=d.get("abstract"),
        keywords=list(d.get("keywords") or []),
        tags=normalize_tags(list(d.get("tags") or [])),
        attachments=attachments,
        read=bool(d.get("read", False)),
        notes=d.get("notes"),
        references=[ReferenceID.parse(value) for value in d.get("references") or []],
        reference_graph=reference_graph,
        source=d.get("source"),
        source_fetched_at=source_fetched_at,
    )


def dumps(record: Record) -> str:
    record.tags = normalize_tags(record.tags)
    validate(record)
    buf = io.StringIO()
    _yaml().dump(_record_to_commented_map(record), buf)
    return buf.getvalue()


def loads(text: str) -> Record:
    data = _yaml().load(text)
    record = _record_from_dict(dict(data))
    validate(record)
    return record


def _read_record_text(path: Path) -> str:
    """Read YAML text, retrying transient iCloud/file-provider errors."""
    delay = _READ_RETRY_DELAY_S
    for attempt in range(_READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            if exc.errno not in _TRANSIENT_READ_ERRNOS or attempt == _READ_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


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
    if not families:
        author_part = "Anon"
    elif len(families) == 1:
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
        return loads(_read_record_text(path))

    @property
    def mutation_lock_path(self) -> Path:
        identity = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f"glean-{identity}.mutation.lock"

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Serialize library mutations across threads and local processes."""
        key = str(self.mutation_lock_path)
        with _MUTATION_LOCKS_GUARD:
            process_lock = _MUTATION_LOCKS.setdefault(key, threading.RLock())
        with process_lock:
            with self.mutation_lock_path.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, record: Record, *, today: Optional[date] = None) -> None:
        with self.mutation_lock():
            destination = self.path_for(record.citekey)
            if destination.is_file():
                current = loads(_read_record_text(destination))
                if current.published_at is not None and record.published_at is None:
                    record.published_at = current.published_at
                incoming_graph = record.reference_graph
                current_graph = current.reference_graph
                if current_graph is not None and (
                    incoming_graph is None
                    or current_graph.fetched_at > incoming_graph.fetched_at
                ):
                    record.references = current.references
                    record.reference_graph = current_graph
            self._save_unlocked(record, today=today)

    def _save_unlocked(self, record: Record, *, today: Optional[date] = None) -> None:
        self.ensure_dirs()
        record.modified_at = today or date.today()
        if record.added_at is None:
            record.added_at = record.modified_at
        destination = self.path_for(record.citekey)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.records_dir,
                prefix=f".{record.citekey}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(dumps(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def delete(self, citekey: str) -> None:
        with self.mutation_lock():
            path = self.path_for(citekey)
            record = None
            try:
                record = loads(_read_record_text(path))
            except FileNotFoundError:
                raise
            except Exception:
                record = None
            path.unlink()
            if record is not None:
                self._delete_attachments_unlocked(record)

    def _delete_attachments_unlocked(self, record: Record) -> None:
        attachments_root = self.attachments_dir.resolve()
        for attachment in record.attachments:
            target = self._resolved_library_attachment(attachment.path, attachments_root)
            if target is None:
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                continue

    def planned_main_attachment_path(self, record: Record) -> str:
        """Relative path the Mac should write into iCloud for the main PDF.

        An existing main attachment path wins, including when the file itself
        lives only in iCloud and is absent from this library root. Otherwise
        the next unused filename under the attachment naming policy is used.
        """
        for attachment in record.attachments:
            if attachment.kind != "main":
                continue
            relative = self.validated_attachment_relative(attachment.path)
            if relative is not None:
                return relative
        for collision_index in (None, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            name = attachment_filename(
                record, kind="main", collision_index=collision_index
            )
            relative = f"attachments/{name}"
            if not (self.root / relative).exists():
                return relative
        raise RuntimeError(
            f"too many attachment-name collisions for {record.citekey}"
        )

    def set_main_attachment_path(self, record: Record, relative_path: str) -> str:
        """Record the main PDF path without copying bytes onto this machine."""
        planned = self.planned_main_attachment_path(record)
        if relative_path != planned:
            raise ValueError(f"unexpected attachment path: {relative_path}")
        if any(
            attachment.kind == "main" and attachment.path == planned
            for attachment in record.attachments
        ):
            return planned
        record.attachments = [
            attachment for attachment in record.attachments if attachment.kind != "main"
        ]
        record.attachments.append(Attachment(path=planned, kind="main"))
        self.save(record)
        return planned

    def main_attachment_file(self, record: Record) -> Optional[Path]:
        """Resolved main PDF inside this library, whether or not it exists."""
        for attachment in record.attachments:
            if attachment.kind != "main":
                continue
            relative = self.validated_attachment_relative(attachment.path)
            if relative is None:
                return None
            return self.root / relative
        return None

    def release_main_attachment_file(self, record: Record) -> bool:
        """Delete this machine's copy of the main PDF after iCloud has it."""
        path = self.main_attachment_file(record)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def validated_attachment_relative(self, relative_path: str) -> Optional[str]:
        raw = (relative_path or "").strip()
        if not re.fullmatch(r"attachments/[^/]+\.pdf", raw):
            return None
        return raw

    def _resolved_library_attachment(
        self, relative_path: str, attachments_root: Path
    ) -> Optional[Path]:
        raw = (relative_path or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(attachments_root)
        except (OSError, ValueError):
            return None
        return resolved

    def iter_records(self) -> Iterator[Record]:
        if not self.records_dir.is_dir():
            return
        for p in sorted(self.records_dir.glob("*.yaml")):
            yield loads(_read_record_text(p))

    def scan_records(self) -> tuple[list[Record], set[str]]:
        """Read available records and report File Provider placeholders.

        A dataless YAML has a real directory entry but no local content yet.
        Index refreshes must retain its existing row instead of treating that
        placeholder as a deleted record. Other transient File Provider read
        failures receive the normal bounded retry, then receive the same
        preserve-until-readable treatment.
        """
        records: list[Record] = []
        unavailable: set[str] = set()
        if not self.records_dir.is_dir():
            return records, unavailable
        for path in sorted(self.records_dir.glob("*.yaml")):
            try:
                flags = getattr(path.stat(), "st_flags", 0)
                if flags & _SF_DATALESS:
                    unavailable.add(path.stem)
                    continue
                records.append(loads(_read_record_text(path)))
            except OSError as exc:
                if exc.errno not in _TRANSIENT_READ_ERRNOS:
                    raise
                unavailable.add(path.stem)
        return records, unavailable
