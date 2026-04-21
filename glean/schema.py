"""Dataclasses and validation for the record schema.

See DESIGN.md "Data model" for the YAML schema and field constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


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
    attachments: list[Attachment] = field(default_factory=list)
    read: bool = False
    notes: Optional[str] = None
    references: list[str] = field(default_factory=list)
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
