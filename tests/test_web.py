from glean.web import _refresh_index


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
