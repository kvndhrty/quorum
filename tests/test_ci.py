"""The fail-soft `gh` CI/PR probe and its digest line.

A fake `gh` (tests/bin/fake_gh.py, installed onto a stripped PATH) plays
GitHub. The probe's contract is the herdr contract, not the sandbox one: no
gh, no auth, no PR, garbage output or a hung call must all degrade to None
and leave the digest byte-identical to one built with the probe off.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from quorum import ci
from quorum.agents.manager import build_digest
from quorum.tasks import TaskStore
from test_tasks import make_repo

FAKE_GH = Path(__file__).parent / "bin" / "fake_gh.py"

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
    mode: str = "pr",
    pr: dict | None = None,
    log: Path | None = None,
) -> None:
    # The shebang is the absolute interpreter: PATH holds no python3.
    body = FAKE_GH.read_text().split("\n", 1)[1]
    shim = bindir / "gh"
    shim.write_text(f"#!{sys.executable}\n{body}")
    shim.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_MODE", mode)
    monkeypatch.setenv("FAKE_GH_PR_JSON", json.dumps(pr if pr is not None else FAILING_PR))
    if log is not None:
        monkeypatch.setenv("FAKE_GH_LOG", str(log))


def make_task(home: Path, workdir: Path, status: str = "executing"):
    store = TaskStore(home)
    task = store.add(project="p", prompt="ship the thing", harness="t")
    return store.update(task.id, workdir=str(workdir), status=status)


# -- the probe itself --------------------------------------------------------


def test_probe_reads_checks_and_names_the_failures(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch)
    task = make_task(home, make_repo(tmp_path))

    state = ci.pr_state(home, task)
    assert state is not None
    assert state["pr"] == 42
    assert state["summary"] == "failing"
    assert state["counts"] == {"pass": 1, "fail": 1, "pending": 1}
    assert state["failing"] == ["tests"]
    assert state["conflict"] is False
    assert "pr=#42" in ci.describe(state)
    assert "failing=tests" in ci.describe(state)


def test_status_contexts_and_green_and_pending_rollups(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    task = make_task(home, make_repo(tmp_path))

    green = {"number": 1, "state": "OPEN", "mergeable": "MERGEABLE", "statusCheckRollup": [
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS"},
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ]}
    install_gh(path_without_gh, monkeypatch, pr=green)
    assert ci.pr_state(home, task)["summary"] == "passing"

    pending = {"number": 1, "state": "OPEN", "statusCheckRollup": [
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"},
    ]}
    install_gh(path_without_gh, monkeypatch, pr=pending)
    assert ci.pr_state(home, task)["summary"] == "pending"

    classic_red = {"number": 1, "state": "OPEN", "statusCheckRollup": [
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE"},
    ]}
    install_gh(path_without_gh, monkeypatch, pr=classic_red)
    state = ci.pr_state(home, task)
    assert state["summary"] == "failing" and state["failing"] == ["ci/legacy"]

    no_checks = {"number": 1, "state": "MERGED", "statusCheckRollup": []}
    install_gh(path_without_gh, monkeypatch, pr=no_checks)
    assert ci.pr_state(home, task)["summary"] == "none"


def test_merge_conflict_is_surfaced(home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch):
    conflicted = {"number": 7, "state": "OPEN", "mergeable": "CONFLICTING", "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]}
    install_gh(path_without_gh, monkeypatch, pr=conflicted)
    task = make_task(home, make_repo(tmp_path))

    state = ci.pr_state(home, task)
    assert state["conflict"] is True
    assert "MERGE-CONFLICT" in ci.describe(state)


@pytest.mark.parametrize("mode", ["nopr", "unauth", "garbage"])
def test_every_gh_disappointment_degrades_to_none(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch, mode: str
):
    install_gh(path_without_gh, monkeypatch, mode=mode)
    assert ci.pr_state(home, make_task(home, make_repo(tmp_path))) is None


def test_a_hung_gh_is_bounded_by_the_timeout(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    (home / "config.toml").write_text("[ci]\ntimeout_seconds = 0.5\n")
    install_gh(path_without_gh, monkeypatch, mode="hang")
    assert ci.pr_state(home, make_task(home, make_repo(tmp_path))) is None


def test_no_gh_no_workdir_and_disabled_config_all_stay_quiet(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    task = make_task(home, make_repo(tmp_path))
    assert ci.available(home) is False  # nothing installed a gh
    assert ci.pr_state(home, task) is None

    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)
    (home / "config.toml").write_text("[ci]\nenabled = false\n")
    assert ci.available(home) is False
    assert ci.pr_state(home, task) is None
    assert not log.exists()  # disabled means the subprocess never runs

    (home / "config.toml").write_text("")
    gone = TaskStore(home).update(task.id, workdir=str(tmp_path / "deleted"))
    assert ci.pr_state(home, gone) is None
    unstarted = TaskStore(home).add(project="p", prompt="not started", harness="t")
    assert ci.pr_state(home, unstarted) is None
    assert not log.exists()  # neither does a task with nowhere to probe


def test_an_unreadable_config_disables_the_probe(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """The fail-open bug (#33): `[ci].enabled = false` in a config.toml that
    does not parse used to fall back to enabled, so the probe kept spawning
    `gh` against the user's explicit switch. An unreadable config means off."""
    task = make_task(home, make_repo(tmp_path))
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)

    (home / "config.toml").write_text("[ci]\nenabled = false\n[harness.broken\noops")
    assert ci.available(home) is False
    assert ci.pr_state(home, task) is None
    assert not log.exists()  # no subprocess, no network call

    # a home with no config at all is the *other* case — the user said
    # nothing, so the probe auto-detects exactly as its docstring promises
    bare = tmp_path / "not-a-home"
    bare.mkdir()
    assert ci.available(bare) is True
    assert not log.exists()  # available() only looks for gh; no probe yet

    # bad bytes are not a ConfigError (tomllib raises UnicodeDecodeError) and
    # must be just as silent: a probe that raised here would take the
    # manager tick down with it
    (home / "config.toml").write_bytes(b"[ci]\nenabled = false\n# caf\xe9\n")
    assert ci.available(home) is False
    assert ci.pr_state(home, task) is None
    assert not log.exists()

    # a config that parses is still trusted, defaults included
    (home / "config.toml").write_text("")
    assert ci.available(home) is True
    assert ci.pr_state(home, task) is not None


def test_herdr_is_off_under_an_unreadable_config_too(home: Path, tmp_path: Path):
    """The sibling audit: same shape, same policy (an optional adapter must
    never be switched *on* by a config quorum could not read)."""
    from quorum import herdr

    sock = tmp_path / "herdr.sock"
    sock.write_text("")  # merely existing is what `available` looks for
    (home / "config.toml").write_text(f'[herdr]\nsocket = "{sock}"\n')
    assert herdr.available(home) is True

    (home / "config.toml").write_text(f'[herdr]\nsocket = "{sock}"\nenabled = [[[')
    assert herdr.available(home) is False


# -- the digest line ---------------------------------------------------------


def test_digest_shows_a_ci_line_for_failing_checks(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch)
    task = make_task(home, make_repo(tmp_path))

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    line = next(x for x in digest.splitlines() if x.strip().startswith("ci:"))
    assert f"- [executing] {task.short_id}" in digest
    assert "pr=#42 state=OPEN checks=failing pass=1 fail=1 pending=1 failing=tests" in line
    assert "https://github.com/o/r/pull/42" in line
    assert "CI-FAILING" not in digest  # a live task's red checks are not a verdict


def test_a_finished_task_over_red_ci_is_marked(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch)
    task = make_task(home, make_repo(tmp_path), status="done")

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "## Recently finished" in digest
    assert f"- [done] {task.short_id}" in digest
    assert "ci: CI-FAILING pr=#42" in digest

    green = {"number": 42, "state": "MERGED", "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]}
    install_gh(path_without_gh, monkeypatch, pr=green)
    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "CI-FAILING" not in digest
    assert "ci: pr=#42 state=MERGED checks=passing" in digest


@pytest.mark.parametrize("mode", ["missing", "unauth"])
def test_digest_is_byte_identical_without_a_working_gh(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch, mode: str
):
    """The acceptance guarantee: a broken forge probe changes nothing."""
    make_task(home, make_repo(tmp_path))
    make_task(home, make_repo(tmp_path, "second"), status="done")
    tasks = TaskStore(home).list()

    (home / "config.toml").write_text("[ci]\nenabled = false\n")
    baseline = build_digest(home, tasks, clock(), directives=["hold the line"])

    (home / "config.toml").write_text("")
    if mode == "unauth":
        install_gh(path_without_gh, monkeypatch, mode="unauth")
    assert build_digest(home, tasks, clock(), directives=["hold the line"]) == baseline
    assert "ci:" not in baseline


def test_a_digest_spends_a_bounded_number_of_probes(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """Digest build blocks the tick, and every probe is a network call — a
    home with more tasks than budget must not stall supervision."""
    from quorum.agents.manager import CI_MAX_PROBES

    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)
    repo = make_repo(tmp_path)
    for _ in range(CI_MAX_PROBES + 3):
        make_task(home, repo)

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert len(log.read_text().splitlines()) == CI_MAX_PROBES
    # the budget is spent in digest order, so the oldest live work is covered
    assert digest.count("  ci: ") == CI_MAX_PROBES


def test_queued_tasks_do_not_spend_the_probe_budget(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """A workdir-less task costs no subprocess, so it must not cost budget:
    a queue deeper than the budget must not starve the one probeable task."""
    from quorum.agents.manager import CI_MAX_PROBES

    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)
    store = TaskStore(home)
    for _ in range(CI_MAX_PROBES + 3):
        store.add(project="p", prompt="queued, nowhere to probe", harness="t")
    task = make_task(home, make_repo(tmp_path))

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert len(log.read_text().splitlines()) == 1
    assert f"- [executing] {task.short_id}" in digest
    assert digest.count("  ci: ") == 1


def test_no_probe_runs_at_all_without_a_working_gh(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)
    make_task(home, make_repo(tmp_path))
    (home / "config.toml").write_text("[ci]\nenabled = false\n")

    build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert not log.exists()


def test_herdr_never_raises_on_undecodable_config(home: Path, tmp_path: Path):
    from quorum import herdr

    (home / "config.toml").write_bytes(b"[herdr]\nenabled = false\n# caf\xe9\n")
    assert herdr.available(home) is False
