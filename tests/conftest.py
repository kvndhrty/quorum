from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
    """Injectable clock: agents receive `now` as a callable by design.

    Anchored to real wall-clock time rather than a fixed date. Agents compare
    their injected clock against real filesystem mtimes — the tracker's
    staleness scan and the bus's stale-claim recovery both do — and that only
    behaves the way it does in production when the two start out agreeing. A
    hardcoded anchor bleeds a day of headroom for every day that passes since
    it was written, so such a test passes on the day it is written and fails
    silently later. Pass `start` when a test needs one specific instant.
    """

    def __init__(self, start: datetime | None = None):
        # Whole seconds: quorum stores timestamps at second resolution
        # (fsio.iso), so a sub-second anchor would not survive a round-trip.
        self.current = start or datetime.now(UTC).replace(microsecond=0)

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
