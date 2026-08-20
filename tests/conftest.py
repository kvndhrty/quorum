from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quorum import home as home_mod

TESTS_BIN = Path(__file__).parent / "bin"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scaffolded QUORUM_HOME in tmp_path, exported via env for CLI calls."""
    target = tmp_path / "qhome"
    home_mod.scaffold(target)
    monkeypatch.setenv("QUORUM_HOME", str(target))
    return target


class FakeClock:
    """Injectable clock: agents receive `now` as a callable by design."""

    def __init__(self, start: datetime | None = None):
        self.current = start or datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_llm() -> list[str]:
    """argv prefix invoking the canned-output fake LLM CLI."""
    return [sys.executable, str(TESTS_BIN / "fake_llm.py")]
