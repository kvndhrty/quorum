from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quorum import home as home_mod

TESTS_BIN = Path(__file__).parent / "bin"
FAKE_GH = TESTS_BIN / "fake_gh.py"

# The PR payload most forge tests want: one red check, one green, one still
# running (tests/bin/fake_gh.py's default `pr view` body).
FAILING_PR = {
    "number": 42,
    "url": "https://github.com/o/r/pull/42",
    "state": "OPEN",
    "isDraft": False,
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": None},
    ],
}


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


@pytest.fixture
def path_without_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding only real git — so `gh` is provably absent until a test
    installs one (the dev machine running these tests very likely has a real
    gh, which would otherwise reach the network)."""
    d = tmp_path / "shimbin"
    d.mkdir()
    git = shutil.which("git")
    assert git, "these tests need git"
    (d / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(d))
    return d


def install_gh(
    bindir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str = "ok",
    pr: dict | None = None,
    issue: dict | None = None,
    log: Path | None = None,
) -> None:
    """Put tests/bin/fake_gh.py on the stripped PATH as `gh`, in `mode`."""
    # The shebang is the absolute interpreter: PATH holds no python3.
    body = FAKE_GH.read_text().split("\n", 1)[1]
    shim = bindir / "gh"
    shim.write_text(f"#!{sys.executable}\n{body}")
    shim.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_MODE", mode)
    monkeypatch.setenv("FAKE_GH_PR_JSON", json.dumps(pr if pr is not None else FAILING_PR))
    if issue is not None:
        monkeypatch.setenv("FAKE_GH_ISSUE_JSON", json.dumps(issue))
    if log is not None:
        monkeypatch.setenv("FAKE_GH_LOG", str(log))
