from pathlib import Path

from glean.store import Library
from glean.web import _record_detail, _refresh_index


class RefreshingIndex:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> tuple[int, int, int]:
        self.calls += 1
        return (1, 2, 3)


class FailingIndex:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> tuple[int, int, int]:
        self.calls += 1
        raise OSError(11, "Resource deadlock avoided")


def test_refresh_index_returns_true_when_refresh_succeeds(capsys):
    idx = RefreshingIndex()

    assert _refresh_index(idx, phase="startup") is True

    assert idx.calls == 1
    assert capsys.readouterr().err == ""


def test_refresh_index_logs_and_returns_false_when_refresh_fails(capsys):
    idx = FailingIndex()

    assert _refresh_index(idx, phase="startup") is False

    captured = capsys.readouterr()
    assert idx.calls == 1
    assert "glean web startup refresh failed" in captured.err
    assert "Resource deadlock avoided" in captured.err


def test_record_detail_retries_transient_deadlock(lib: Library, monkeypatch):
    original_read_text = Path.read_text
    calls = 0

    def flaky_read_text(path: Path, *args, **kwargs):
        nonlocal calls
        if path.name == "Miyawaki2024Attention.yaml" and calls == 0:
            calls += 1
            raise OSError(11, "Resource deadlock avoided")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    detail = _record_detail(lib, "Miyawaki2024Attention")

    assert detail is not None
    assert detail["citekey"] == "Miyawaki2024Attention"
    assert calls == 1
