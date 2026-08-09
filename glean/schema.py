"""Dataclasses and validation for the record schema.

See DESIGN.md "Data model" for the YAML schema and field constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Optional
import unicodedata


TYPE_ENUM = frozenset({
    "journal-article",
    "preprint",
    "book",
    "chapter",
    "thesis",
    "report",
    "conference",
    "other",
})

ATTACHMENT_KIND_ENUM = frozenset({
    "main",
    "supplement",
    "erratum",
    "corrigendum",
    "slides",
    "response",
    "dataset",
    "other",
})


class ValidationError(ValueError):
    pass


REFERENCE_KIND_ENUM = frozenset({"doi", "openalex", "citekey"})
REFERENCE_GRAPH_STATUS_ENUM = frozenset({"complete", "empty"})


@dataclass(frozen=True)
class ReferenceID:
    """Stable, explicitly typed identity for a cited work."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        kind = self.kind.strip().lower()
        value = self.value.strip()
        if kind not in REFERENCE_KIND_ENUM:
            raise ValidationError(f"unknown reference kind: {self.kind!r}")
        if not value:
            raise ValidationError("reference value is required")
        if kind == "doi":
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if value.lower().startswith(prefix):
                    value = value[len(prefix):].strip()
                    break
        elif kind == "openalex":
            for prefix in ("https://openalex.org/", "http://openalex.org/", "openalex:"):
                if value.lower().startswith(prefix):
                    value = value[len(prefix):].strip()
                    break
            value = value.upper()
        elif kind == "citekey" and value.lower().startswith("citekey:"):
            value = value[len("citekey:"):].strip()
        if not value:
            raise ValidationError("reference value is required")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)

    @classmethod
    def parse(cls, value: str | ReferenceID) -> ReferenceID:
        if isinstance(value, ReferenceID):
            return value
        if not isinstance(value, str):
            raise ValidationError(
                f"reference must be a string, got {type(value).__name__}"
            )
        raw = value.strip()
        lowered = raw.lower()
        for kind in REFERENCE_KIND_ENUM:
            prefix = f"{kind}:"
            if lowered.startswith(prefix):
                return cls(kind, raw[len(prefix):])
        if lowered.startswith(("https://openalex.org/", "http://openalex.org/")):
            return cls("openalex", raw)
        if lowered.startswith(("https://doi.org/", "http://doi.org/")) or "/" in raw:
            return cls("doi", raw)
        return cls("citekey", raw)

    @property
    def serialized(self) -> str:
        return f"{self.kind}:{self.value}"

    @property
    def legacy_value(self) -> str:
        return self.serialized if self.kind == "openalex" else self.value

    @property
    def normalized_value(self) -> str:
        return self.value.lower() if self.kind == "doi" else self.value


@dataclass
class ReferenceGraphState:
    status: str
    providers: list[str]
    fetched_at: date


_TAG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_tag(tag: str) -> str:
    """Normalize a user-facing tag to a lowercase ASCII slug."""
    if not isinstance(tag, str):
        raise ValidationError(f"tag must be a string, got {type(tag).__name__}")
    ascii_tag = (
        unicodedata.normalize("NFKD", tag)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = _TAG_NON_ALNUM.sub("-", ascii_tag).strip("-")
    if not slug:
        raise ValidationError("tag must contain at least one ASCII letter or digit")
    return slug


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize and de-duplicate tags while preserving first-seen order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        slug = normalize_tag(tag)
        if slug not in seen:
            normalized.append(slug)
            seen.add(slug)
    return normalized


@dataclass
class Author:
    family: str
    given: Optional[str] = None


@dataclass
class Attachment:
    path: str
    kind: str


@dataclass
class Record:
    citekey: str
    type: str
    title: str
    authors: list[Author]
    year: int
    added_at: Optional[date] = None
    modified_at: Optional[date] = None
    doi: Optional[str] = None
    arxiv: Optional[str] = None
    isbn: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    legacy_citekey: Optional[str] = None
    journal: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    publisher: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    read: bool = False
    notes: Optional[str] = None
    references: list[ReferenceID] = field(default_factory=list)
    reference_graph: Optional[ReferenceGraphState] = None
    source: Optional[str] = None
    source_fetched_at: Optional[date] = None

    def main_attachment(self) -> Optional[Attachment]:
        for a in self.attachments:
            if a.kind == "main":
                return a
        return None


def validate(record: Record) -> None:
    from glean.citekey import is_valid as citekey_is_valid

    if not citekey_is_valid(record.citekey):
        raise ValidationError(f"invalid citekey: {record.citekey!r}")
    if record.type not in TYPE_ENUM:
        raise ValidationError(f"invalid type: {record.type!r}")
    if not record.title:
        raise ValidationError("title is required")
    if not record.authors:
        raise ValidationError("at least one author is required")
    for a in record.authors:
        if not a.family:
            raise ValidationError("author family is required")
    if not isinstance(record.year, int):
        raise ValidationError(f"year must be an int, got {type(record.year).__name__}")
    seen_tags: set[str] = set()
    for tag in record.tags:
        normalized = normalize_tag(tag)
        if tag != normalized:
            raise ValidationError(f"tag is not normalized: {tag!r}")
        if tag in seen_tags:
            raise ValidationError(f"duplicate tag: {tag!r}")
        seen_tags.add(tag)

    seen_references: set[tuple[str, str]] = set()
    for reference in record.references:
        if not isinstance(reference, ReferenceID):
            raise ValidationError("record references must be ReferenceID values")
        key = (reference.kind, reference.normalized_value)
        if key in seen_references:
            raise ValidationError(f"duplicate reference: {reference.serialized!r}")
        seen_references.add(key)
    if record.reference_graph is not None:
        graph = record.reference_graph
        if graph.status not in REFERENCE_GRAPH_STATUS_ENUM:
            raise ValidationError(f"invalid reference graph status: {graph.status!r}")
        if not isinstance(graph.fetched_at, date):
            raise ValidationError("reference graph fetched_at must be a date")
        if graph.status == "empty" and record.references:
            raise ValidationError("empty reference graph cannot contain references")
        if not graph.providers or any(not provider.strip() for provider in graph.providers):
            raise ValidationError("reference graph providers are required")

    main_count = 0
    for a in record.attachments:
        if a.kind not in ATTACHMENT_KIND_ENUM:
            raise ValidationError(f"invalid attachment kind: {a.kind!r}")
        if not a.path:
            raise ValidationError("attachment path is required")
        if a.kind == "main":
            main_count += 1
    if main_count > 1:
        raise ValidationError("at most one main attachment allowed")
