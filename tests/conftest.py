from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
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


# -- git repositories ---------------------------------------------------------

FAKE_HARNESS = TESTS_BIN / "fake_harness.py"


def repo_git(repo: Path, *args: str) -> None:
    """Run git in `repo` with a committer identity supplied on the command
    line, so a repo with no user config still commits."""
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def git_out(repo: Path, *args: str) -> str:
    """Stdout of a git command in `repo`, stripped. Never raises: callers
    assert on the text, and an empty string is a legitimate answer."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    ).stdout.strip()


def make_repo(base: Path, name: str = "proj", *, commit: bool = True) -> Path:
    """A git repository at `base / name` with one commit on the default branch.

    The committer identity is written into the repo config as well as passed
    on the command line, because committing inside a worktree (the auto-commit
    safety net does) has no -c flags of its own.

    Pass `commit=False` for an empty repository — a checkout with no commits
    at all, which some checks have to survive.
    """
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    repo_git(repo, "init", "-q")
    repo_git(repo, "config", "user.email", "t@t")
    repo_git(repo, "config", "user.name", "T")
    if commit:
        (repo / "README.md").write_text("hello")
        repo_git(repo, "add", ".")
        repo_git(repo, "commit", "-qm", "init")
    return repo


# -- the fake harness ---------------------------------------------------------


def harness_table(name: str = "fake", *, resume: bool = True, mode: str = "") -> str:
    """The TOML for one `[harness.<name>]` table pointing at the fake harness.

    `mode` pins FAKE_HARNESS_MODE through the table's own `env`, which is how
    a fake task harness and a fake manager harness coexist in one config.
    """
    lines = [f"[harness.{name}]", f'start = ["{sys.executable}", "{FAKE_HARNESS}"]']
    if resume:
        lines.append(f'resume = ["{sys.executable}", "{FAKE_HARNESS}", "--resumed", "{{session}}"]')
    if mode:
        lines.append(f'env = {{ FAKE_HARNESS_MODE = "{mode}" }}')
    return "\n".join(lines) + "\n"


def harness_config(
    home: Path, extra: str = "", tasks_extra: str = "", *, resume: bool = True
) -> None:
    """Write a config.toml whose default harness is the fake one.

    `tasks_extra` adds lines to `[tasks]`, `extra` is appended after the
    harness table (a second table, a `[sandbox]` block, and so on).
    """
    body = (
        "[tasks]\n"
        'default_harness = "fake"\n'
        f"{tasks_extra}"
        f"{harness_table(resume=resume)}"
        f"{extra}"
    )
    (home / "config.toml").write_text(body)


# -- the TUI ------------------------------------------------------------------


@pytest.fixture
def tui():
    """Drive the Textual dashboard: `tui(home, script, ...)` mounts one app and
    awaits each `script(app, pilot)` against it in turn.

    Passing several scripts is how a test that would otherwise mount the app
    twice pays the startup cost once; they share one app, so state a script
    leaves behind (the cursor row, the open task, the screen stack) is what the
    next one starts from. Tests stay isolated from one another because each
    gets its own `home` and its own app.
    """
    from quorum.tui.app import QuorumTUI

    def drive(home: Path, *scripts) -> None:
        async def main() -> None:
            app = QuorumTUI(home)
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause()
                for script in scripts:
                    await script(app, pilot)

        asyncio.run(main())

    return drive


# -- documented CLI commands --------------------------------------------------

# The packaged prompts, and the example home's README and overlays, teach the
# CLI by naming commands in prose. A command renamed out from under one of
# them fails silently at 3am, in a transcript nobody reads — so both suites
# extract what a file tells a reader to run and check it against the real app.


def quorum_invocations(text: str) -> list[str]:
    """Every `quorum ...` command a document tells someone to run: inline code
    spans, list-item tool lines, and indented example blocks."""
    import re

    found = [span for span in re.findall(r"`([^`\n]+)`", text) if span.startswith("quorum ")]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if stripped.startswith("quorum "):
            found.append(stripped)
    return found


def cli_command_names(app) -> set[str]:
    """"task run", "up", ... — every command name the typer app registers."""

    def cmd_name(info) -> str:
        # An unnamed @app.command() takes its name from the callback.
        return info.name or info.callback.__name__.rstrip("_").replace("_", "-")

    known = {cmd_name(c) for c in app.registered_commands}
    for group in app.registered_groups:
        known |= {f"{group.name} {cmd_name(c)}" for c in group.typer_instance.registered_commands}
    return known


def names_a_real_command(invocation: str, known: set[str]) -> bool:
    """Does `invocation` start with a command in `known`? Its leading words up
    to the first non-word token are the command; the rest are arguments."""
    import re

    words: list[str] = []
    for token in invocation.split()[1:]:
        if len(words) == 2 or not re.fullmatch(r"[a-z][a-z-]*", token):
            break
        words.append(token)
    assert words, f"bare `quorum` in {invocation!r}"
    return " ".join(words) in known or words[0] in known
