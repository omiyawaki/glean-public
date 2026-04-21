from glean.citekey import is_valid, looks_like_doi


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
