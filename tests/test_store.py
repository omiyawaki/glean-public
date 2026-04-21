from datetime import date
from pathlib import Path

import pytest

from glean.schema import Attachment, Author, Record
from glean.store import Library, attachment_filename, dumps, loads


def test_load_journal_article(lib: Library):
    r = lib.load("Miyawaki2024Attention")
    assert r.citekey == "Miyawaki2024Attention"
    assert r.type == "journal-article"
    assert r.title == "Attention and convection"
    assert r.authors[0].family == "Miyawaki"
    assert r.authors[0].given == "Osamu"
    assert r.year == 2024
    assert r.doi == "10.1175/JCLI-D-23-0456.1"
    assert r.keywords == ["attention", "convection", "transformers"]
    assert len(r.attachments) == 2
    assert r.main_attachment().kind == "main"
    assert r.references == [
        "10.1175/JAS-D-22-0011.1",
        "Sellers1965PhysicalClimatology",
    ]
    assert "paragraphs" in r.abstract
    assert "Figure 3" in r.notes


def test_load_preprint_keeps_arxiv_as_string(lib: Library):
    r = lib.load("Miyawaki2023Transformer")
    assert r.arxiv == "2301.04567"  # leading-zero preservation matters for e.g. 0801.*


def test_load_textbook_without_doi(lib: Library):
    r = lib.load("Vallis2019AOFD")
    assert r.doi is None
    assert r.read is True


def test_iter_records_sorted(lib: Library):
    citekeys = [r.citekey for r in lib.iter_records()]
    assert citekeys == sorted(citekeys)


def test_round_trip_is_idempotent_in_memory(lib: Library):
    r = lib.load("Miyawaki2024Attention")
    text1 = dumps(r)
    r2 = loads(text1)
    text2 = dumps(r2)
    assert text1 == text2


def test_save_bumps_modified_at(lib: Library, tmp_path: Path):
    r = lib.load("Vallis2019AOFD")
    assert r.modified_at == date(2026, 3, 2)
    lib.save(r, today=date(2026, 4, 20))
    r2 = lib.load("Vallis2019AOFD")
    assert r2.modified_at == date(2026, 4, 20)
    assert r2.added_at == date(2026, 3, 2)  # untouched


def test_save_new_record_sets_added_at(lib: Library):
    r = Record(
        citekey="Held2019Radiative",
        type="journal-article",
        title="Radiative-convective equilibrium",
        authors=[Author(family="Held", given="Isaac")],
        year=2019,
        added_at=None,        # new record, not set
        modified_at=None,
    )
    lib.save(r, today=date(2026, 4, 20))
    r2 = lib.load("Held2019Radiative")
    assert r2.added_at == date(2026, 4, 20)
    assert r2.modified_at == date(2026, 4, 20)


def test_delete(lib: Library):
    assert lib.exists("Miyawaki2023Transformer")
    lib.delete("Miyawaki2023Transformer")
    assert not lib.exists("Miyawaki2023Transformer")


def test_attachment_filename_single_author():
    r = Record(
        citekey="X", type="journal-article", title="A neat title",
        authors=[Author(family="Miyawaki", given="Osamu")],
        year=2024, added_at=date.today(), modified_at=date.today(),
    )
    assert attachment_filename(r) == "Miyawaki (2024) A neat title.pdf"


def test_attachment_filename_two_authors_and_kind():
    r = Record(
        citekey="X", type="journal-article", title="A neat title",
        authors=[Author(family="Miyawaki"), Author(family="Vallis")],
        year=2024, added_at=date.today(), modified_at=date.today(),
    )
    assert attachment_filename(r, kind="supplement") == \
        "Miyawaki and Vallis (2024) A neat title - SI.pdf"


def test_attachment_filename_three_plus_authors():
    r = Record(
        citekey="X", type="journal-article", title="Many cooks",
        authors=[Author(family="Arakawa"), Author(family="Schubert"), Author(family="Held")],
        year=1974, added_at=date.today(), modified_at=date.today(),
    )
    assert attachment_filename(r) == "Arakawa et al (1974) Many cooks.pdf"


def test_attachment_filename_title_truncated_at_colon():
    r = Record(
        citekey="X", type="book", title="Atmospheric and Oceanic Fluid Dynamics: Fundamentals",
        authors=[Author(family="Vallis")],
        year=2019, added_at=date.today(), modified_at=date.today(),
    )
    assert attachment_filename(r) == \
        "Vallis (2019) Atmospheric and Oceanic Fluid Dynamics.pdf"


def test_attachment_filename_truncated_at_80_chars():
    long_title = "A" * 120
    r = Record(
        citekey="X", type="journal-article", title=long_title,
        authors=[Author(family="Test")],
        year=2020, added_at=date.today(), modified_at=date.today(),
    )
    result = attachment_filename(r)
    # Expect exactly 80 As in the title portion.
    assert "A" * 80 in result
    assert "A" * 81 not in result


def test_attachment_filename_strips_slash():
    r = Record(
        citekey="X", type="journal-article", title="Dynamics of A/B forcing",
        authors=[Author(family="Test")],
        year=2020, added_at=date.today(), modified_at=date.today(),
    )
    assert "/" not in attachment_filename(r)


def test_attachment_filename_collision_index():
    r = Record(
        citekey="X", type="journal-article", title="T",
        authors=[Author(family="Miyawaki")],
        year=2024, added_at=date.today(), modified_at=date.today(),
    )
    assert attachment_filename(r, collision_index=2) == "Miyawaki (2024) T (2).pdf"
    assert attachment_filename(r, collision_index=3, kind="supplement") == \
        "Miyawaki (2024) T (3) - SI.pdf"
