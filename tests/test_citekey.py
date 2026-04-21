from glean.citekey import (
    disambiguate,
    first_content_word,
    generate,
    is_valid,
    looks_like_doi,
)


def test_is_valid_accepts_typical():
    assert is_valid("Miyawaki2024Attention")
    assert is_valid("Vallis2019AOFD")
    assert is_valid("Held_2019-a")


def test_is_valid_rejects_empty():
    assert not is_valid("")


def test_is_valid_rejects_whitespace():
    assert not is_valid("Miyawaki 2024")


def test_is_valid_rejects_slash():
    assert not is_valid("10.1175/JCLI")


def test_looks_like_doi():
    assert looks_like_doi("10.1175/JCLI-D-23-0456.1")
    assert not looks_like_doi("Miyawaki2024Attention")


def test_first_content_word_skips_stopwords():
    assert first_content_word("The attention mechanism") == "attention"
    assert first_content_word("On a Thermodynamic Model") == "Thermodynamic"
    assert first_content_word("A Short Note") == "Short"


def test_first_content_word_falls_back_empty():
    assert first_content_word("The of on in") == ""
    assert first_content_word("") == ""


def test_generate_basic():
    assert generate("Miyawaki", 2024, "Attention and convection") == "Miyawaki2024Attention"
    assert generate("Thuburn", 2017, "Use of the Gibbs thermodynamic potential") == "Thuburn2017Use"


def test_generate_ascii_folds_diacritics():
    assert generate("Vélez-Pardo", 2024, "Étude physique") == "VelezPardo2024Etude"


def test_generate_handles_empty_title():
    # No content word available — citekey is family+year only.
    assert generate("Held", 2019, "") == "Held2019"


def test_disambiguate_returns_base_if_free():
    assert disambiguate("Held2019Radiative", set()) == "Held2019Radiative"


def test_disambiguate_appends_suffix():
    taken = {"Held2019Radiative", "Held2019Radiativea"}
    assert disambiguate("Held2019Radiative", taken) == "Held2019Radiativeb"
