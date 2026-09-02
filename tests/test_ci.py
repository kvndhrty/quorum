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

from quorum import ci, fsio
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


def test_auth_status_answers_yes_no_or_nothing(
    home: Path, path_without_gh: Path, monkeypatch
):
    """The doctor entry point. Three answers, and the third is not a failure:
    a gh that never replied says nothing about whether it is logged in."""
    assert ci.auth_status(home) is None  # no gh on PATH at all

    install_gh(path_without_gh, monkeypatch, mode="pr")  # exits 0
    assert ci.auth_status(home) is True

    install_gh(path_without_gh, monkeypatch, mode="unauth")
    assert ci.auth_status(home) is False

    (home / "config.toml").write_text("[ci]\ntimeout_seconds = 0.5\n")
    install_gh(path_without_gh, monkeypatch, mode="hang")
    assert ci.auth_status(home) is None  # offline is not unauthenticated


def test_auth_status_honours_the_same_ci_switches_as_the_probe(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, log=log)

    (home / "config.toml").write_text("[ci]\nenabled = false\n")
    assert ci.auth_status(home) is None
    assert not log.exists()  # disabled means no subprocess, exactly like pr_state

    (home / "config.toml").write_text("[ci]\nenabled = false\n[harness.broken\noops")
    assert ci.auth_status(home) is None
    assert not log.exists()  # and an unreadable config means off, never fail-open


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
    assert "pr=#42 state=open checks=failing pass=1 fail=1 pending=1 failing=tests" in line
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
    assert "ci: pr=#42 state=merged checks=passing" in digest


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


# -- the merged observation (#57) --------------------------------------------
#
# A task's lifecycle ends at the harness's word (`done`), but its work is
# delivered when the PR merges. The forge's word for that reaches disk in
# exactly one place — the digest's probe path — so views can badge it while
# staying pure file readers.


def merged_pr(state: str = "MERGED") -> dict:
    """A green PR in a given forge state."""
    return {
        "number": 42,
        "url": "https://github.com/o/r/pull/42",
        "state": state,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "t", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("OPEN", "open"),
        ("open", "open"),
        ("OPENED", "open"),  # what #51's glab backend will say
        ("MERGED", "merged"),
        ("CLOSED", "closed"),
        ("LOCKED", "closed"),
        ("", "unknown"),
        (None, "unknown"),
        ("SOMETHING_NEW", "unknown"),
        (17, "unknown"),
    ],
)
def test_pr_states_normalize_to_a_forge_neutral_vocabulary(raw, expected):
    assert ci.normalize_state(raw) == expected


@pytest.mark.parametrize(
    "forge,state,merged",
    [("OPEN", "open", False), ("MERGED", "merged", True), ("CLOSED", "closed", False)],
)
def test_the_probe_reports_the_normalized_state_and_a_merged_flag(
    home: Path, tmp_path: Path, path_without_gh: Path, monkeypatch, forge, state, merged
):
    install_gh(path_without_gh, monkeypatch, pr=merged_pr(forge))
    task = make_task(home, make_repo(tmp_path), status="done")

    probed = ci.pr_state(home, task)
    assert probed["state"] == state and probed["merged"] is merged
    assert f"state={state}" in ci.describe(probed)


def test_the_digest_records_the_observed_pr_state_on_task_json(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """The one deliberate materialization: written from the probe path so
    `quorum status` can badge merged without making a network call."""
    install_gh(path_without_gh, monkeypatch, pr=merged_pr())
    store = TaskStore(home)
    task = make_task(home, make_repo(tmp_path), status="done")
    assert store.get(task.id).pr_state is None

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "ci: pr=#42 state=merged" in digest

    recorded = store.get(task.id)
    assert recorded.pr_state == "merged"
    # Stamped with the tick's clock, not wall time: the observation belongs
    # to the digest that made it.
    assert recorded.pr_state_at == fsio.iso(clock())
    # An observation is not an edit: the recently-finished window is measured
    # from updated_at, so a probe that bumped it would pin the task there.
    assert recorded.updated_at == task.updated_at


def test_a_state_that_is_not_recognized_is_never_written(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    install_gh(path_without_gh, monkeypatch, pr=merged_pr("DRAFTING"))
    store = TaskStore(home)
    task = make_task(home, make_repo(tmp_path), status="done")

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "state=unknown" in digest
    assert store.get(task.id).pr_state is None


@pytest.mark.parametrize("mode", ["missing", "nopr", "unauth", "garbage"])
def test_nothing_is_written_when_the_probe_says_nothing(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch, mode: str
):
    """Fail-soft all the way to disk: no gh, no PR → no pr_state, no badge."""
    if mode != "missing":
        install_gh(path_without_gh, monkeypatch, mode=mode)
    store = TaskStore(home)
    task = make_task(home, make_repo(tmp_path), status="done")

    build_digest(home, store.list(), clock(), directives=[])
    after = store.get(task.id)
    assert after.pr_state is None and after.pr_state_at is None


def test_a_merged_task_never_carries_a_red_flag(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """Merged is delivered, whatever a stale rollup still says."""
    stale = dict(FAILING_PR, state="MERGED", mergeable="CONFLICTING")
    install_gh(path_without_gh, monkeypatch, pr=stale)
    make_task(home, make_repo(tmp_path), status="done")

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "state=merged" in digest and "checks=failing" in digest
    assert "CI-FAILING" not in digest

    # ... while the very same rollup on an open PR still is one.
    install_gh(path_without_gh, monkeypatch, pr=dict(FAILING_PR, mergeable="CONFLICTING"))
    assert "CI-FAILING" in build_digest(home, TaskStore(home).list(), clock(), directives=[])


def test_a_view_never_probes_and_reads_the_recorded_state_back(
    home: Path, clock, tmp_path: Path, path_without_gh: Path, monkeypatch
):
    """The point of materializing it: views stay pure file readers."""
    from quorum import views

    log = tmp_path / "gh.log"
    install_gh(path_without_gh, monkeypatch, pr=merged_pr(), log=log)
    task = make_task(home, make_repo(tmp_path), status="done")
    build_digest(home, TaskStore(home).list(), clock(), directives=[])
    probes = len(log.read_text().splitlines())

    row = next(r for r in views.task_rows(home) if r["id"] == task.id)
    assert row["pr_state"] == "merged" and row["pr_state_at"]
    assert len(log.read_text().splitlines()) == probes  # the view spent no gh call


# -- record_pr_state, the single writer --------------------------------------


def test_record_pr_state_is_a_no_op_for_unknown_and_unchanged_states(home: Path, tmp_path: Path):
    from quorum import tasks as tasks_mod

    store = TaskStore(home)
    task = make_task(home, make_repo(tmp_path), status="done")
    assert tasks_mod.record_pr_state(home, task, "unknown") is False
    assert tasks_mod.record_pr_state(home, task, None) is False
    assert store.get(task.id).pr_state is None

    assert tasks_mod.record_pr_state(home, task, "open") is True
    assert tasks_mod.record_pr_state(home, task, "open") is False  # nothing changed
    assert tasks_mod.record_pr_state(home, task, "merged") is True
    assert store.get(task.id).pr_state == "merged"


def test_record_pr_state_never_rolls_back_a_status_reported_meanwhile(
    home: Path, tmp_path: Path
):
    """The Task in hand may be stale — a live run reports through the same
    file — so the record is re-read from disk, never dumped over."""
    from quorum import tasks as tasks_mod

    store = TaskStore(home)
    task = make_task(home, make_repo(tmp_path), status="executing")
    store.update(task.id, status="done")  # the harness reported, we hold a stale copy

    assert tasks_mod.record_pr_state(home, task, "merged") is True
    after = store.get(task.id)
    assert after.status == "done" and after.pr_state == "merged"


def test_record_pr_state_fails_soft_on_an_unreadable_record(home: Path, tmp_path: Path):
    from quorum import tasks as tasks_mod

    task = make_task(home, make_repo(tmp_path), status="done")
    tasks_mod.task_json_path(home, task.id).write_text("{not json")
    assert tasks_mod.record_pr_state(home, task, "merged") is False

    fsio.atomic_write_json(tasks_mod.task_json_path(home, task.id), ["a", "list"])
    assert tasks_mod.record_pr_state(home, task, "merged") is False

    missing = task.model_copy(update={"id": "01MISSINGMISSINGMISSINGMI"})
    assert tasks_mod.record_pr_state(home, missing, "merged") is False
