from datetime import date

import pytest

from glean.schema import Attachment, Author, Record, ValidationError, validate


def make_record(**overrides) -> Record:
    base = dict(
        citekey="Miyawaki2024Attention",
        type="journal-article",
        title="Attention and convection",
        authors=[Author(family="Miyawaki", given="Osamu")],
        year=2024,
        added_at=date(2026, 4, 18),
        modified_at=date(2026, 4, 20),
    )
    base.update(overrides)
    return Record(**base)


def test_validate_ok():
    validate(make_record())


def test_validate_rejects_non_date_publication_date():
    with pytest.raises(ValidationError, match="published_at must be a date"):
        validate(make_record(published_at="2024-03-14"))


def test_validate_rejects_bad_citekey():
    with pytest.raises(ValidationError):
        validate(make_record(citekey="has space"))


def test_validate_rejects_unknown_type():
    with pytest.raises(ValidationError):
        validate(make_record(type="blog-post"))


def test_validate_requires_authors():
    with pytest.raises(ValidationError):
        validate(make_record(authors=[]))


def test_validate_rejects_author_without_family():
    with pytest.raises(ValidationError):
        validate(make_record(authors=[Author(family="", given="Osamu")]))


def test_validate_rejects_unknown_attachment_kind():
    r = make_record(attachments=[Attachment(path="a.pdf", kind="foo")])
    with pytest.raises(ValidationError):
        validate(r)


def test_validate_rejects_two_main_attachments():
    r = make_record(attachments=[
        Attachment(path="a.pdf", kind="main"),
        Attachment(path="b.pdf", kind="main"),
    ])
    with pytest.raises(ValidationError):
        validate(r)


def test_main_attachment_getter():
    r = make_record(attachments=[
        Attachment(path="a.pdf", kind="supplement"),
        Attachment(path="b.pdf", kind="main"),
    ])
    assert r.main_attachment().path == "b.pdf"
    r2 = make_record()
    assert r2.main_attachment() is None
