"""Task substrate and runner tests: the store, one full harness run, guidance
injection, session capture/resume, and the cooperative report channel."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quorum import fsio, runner, tasks
from quorum.config import HarnessConfig, load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.runner import RunnerError, run_task
from quorum.tasks import TaskStore

TESTS_BIN = Path(__file__).parent / "bin"
FAKE = str(TESTS_BIN / "fake_harness.py")


def make_repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
            check=True, capture_output=True,
        )
    git("init", "-q")
    # Committing inside a worktree (the auto-commit safety net does) has no
    # -c flags of its own, so the identity has to live in the repo config.
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (repo / "README.md").write_text("hello")
    git("add", ".")
    git("commit", "-qm", "init")
    return repo


def repo_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    ).stdout.strip()


def harness_config(home: Path, extra: str = "", tasks_extra: str = "") -> None:
    body = (
        "[tasks]\n"
        'default_harness = "fake"\n'
        f"{tasks_extra}"
        "[harness.fake]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        f'resume = ["{sys.executable}", "{FAKE}", "--resumed", "{{session}}"]\n'
        f"{extra}"
    )
    (home / "config.toml").write_text(body)


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    return "proj"


def transcript_text(home: Path, task_id: str) -> str:
    lines = []
    for e in fsio.read_jsonl(tasks.transcript_path(home, task_id)):
        lines.append(e.get("line") or json.dumps(e.get("event")))
    return "\n".join(lines)


# -- store ----------------------------------------------------------------


def test_store_add_resolve_and_prefix(home: Path):
    store = TaskStore(home)
    t1 = store.add("proj", "do a thing", "fake")
    t2 = store.add("proj", "another", "fake")
    assert store.resolve(t1.id).id == t1.id
    assert store.resolve(t1.short_id).id == t1.id  # case-insensitive suffix handle
    assert store.resolve(t2.short_id).id == t2.id  # same-instant tasks stay distinct
    with pytest.raises(KeyError):
        store.resolve("zzzzzz")
    shared = t1.id[:2]  # ULIDs minted the same second share their prefix
    assert t2.id.startswith(shared)
    with pytest.raises(ValueError):
        store.resolve(shared)


def test_report_updates_status_and_board(home: Path):
    store = TaskStore(home)
    t = store.add("proj", "x", "fake")
    tasks.report(home, t.short_id, status="executing", text="working on it")
    tasks.report(home, t.id, status="pr", text="opened", pr_url="https://example.com/pr/1")
    fresh = store.get(t.id)
    assert fresh.status == "pr" and fresh.pr_url == "https://example.com/pr/1"
    assert [r["status"] for r in tasks.read_reports(home, t.id)] == ["executing", "pr"]
    board = MessageBus(home).read_topic(tasks.BOARD_TOPIC)
    assert [m.type for m in board] == ["task.executing", "task.pr"]


# -- runner ---------------------------------------------------------------


def test_run_creates_worktree_and_streams_transcript(home: Path, project: str, tmp_path: Path):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "improve the README", "fake")

    assert run_task(home, config, task.id) == 0

    fresh = TaskStore(home).get(task.id)
    workdir = Path(fresh.workdir)
    assert workdir == tasks.worktree_path(home, task.id) and workdir.is_dir()
    branches = subprocess.run(
        ["git", "-C", str(tmp_path / "proj"), "branch", "--list", f"quorum/{task.short_id}"],
        capture_output=True, text=True,
    ).stdout
    assert f"quorum/{task.short_id}" in branches

    text = transcript_text(home, task.id)
    assert f"Task ID: {task.short_id}" in text  # preamble reached the harness
    assert "improve the README" in text  # so did the task prompt
    assert f"CWD| {workdir}" in text  # and it ran in the worktree
    assert fresh.session == "sess-fake-123"  # captured from the JSON stream
    assert len(fresh.runs) == 1 and fresh.runs[0].exit_code == 0


def test_guidance_is_claimed_and_injected(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    bus = MessageBus(home)
    bus.send("monitor", tasks.inbox_name(task.id), type="nudge", text="try the other approach")

    run_task(home, config, task.id)

    assert "try the other approach" in transcript_text(home, task.id)
    assert fsio.sorted_entries(bus.inbox_dir / tasks.inbox_name(task.id) / "new") == []


def test_resume_template_used_once_session_known(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    run_task(home, config, task.id)  # captures the session id
    run_task(home, config, task.id)

    entries = fsio.read_jsonl(tasks.transcript_path(home, task.id))
    argvs = [e["event"]["argv"] for e in entries if "event" in e and "argv" in e.get("event", {})]
    assert "--resumed" not in argvs[0]
    assert argvs[1][0] == "--resumed" and argvs[1][1] == "sess-fake-123"


def test_session_capture_accepts_codex_thread_ids():
    assert runner._find_session_id({"type": "thread.started", "thread_id": "th-1"}) == "th-1"
    assert runner._find_session_id({"threadId": "th-2"}) == "th-2"
    assert runner._find_session_id({"type": "system", "session_id": "s-1"}) == "s-1"
    assert runner._find_session_id({"type": "turn.started"}) is None


def test_inject_pump_delivers_mid_run_guidance(home: Path, project: str, monkeypatch):
    """A nudge that arrives while the harness is running reaches it as a
    stream-json user turn (the fake posts the nudge itself mid-run, before its
    first turn boundary, so delivery provably happens inside one run)."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    monkeypatch.setenv("FAKE_HARNESS_INJECT_POST", "nudge")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    text = transcript_text(home, task.id)
    assert "switch to the fallback plan" in text  # the nudge reached the live harness
    assert '"role": "user"' in text  # ...framed as a stream-json user turn
    inbox = MessageBus(home).inbox_dir / tasks.inbox_name(task.id)
    assert fsio.sorted_entries(inbox / "new") == []  # consumed, not re-delivered next run
    assert fsio.sorted_entries(inbox / "cur") == []


def test_build_harness_argv_strips_prompt_for_inject_harnesses():
    """A stream-json CLI reads user turns only from stdin and ignores an argv
    prompt (this is how real claude behaves), so inject templates lose their
    "{prompt}" element and never get the prompt appended."""
    inject = HarnessConfig(start=["h", "-p", "{prompt}", "--flag"], inject="stream-json")
    assert runner.build_harness_argv(inject, "the prompt") == ["h", "-p", "--flag"]
    bare = HarnessConfig(start=["h"], inject="stream-json")
    assert runner.build_harness_argv(bare, "the prompt") == ["h"]
    plain = HarnessConfig(start=["h"])
    assert runner.build_harness_argv(plain, "the prompt") == ["h", "the prompt"]


def test_inject_prompt_arrives_over_stdin_not_argv(home: Path, project: str, monkeypatch):
    """The composed prompt reaches an inject harness as the pump's opening
    stream-json user turn — the regression that hung every real claude run:
    the prompt sat on argv, which the stream-json protocol ignores, and the
    harness waited on stdin until the run timeout killed it."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "improve the README", "fake")

    assert run_task(home, config, task.id) == 0

    entries = fsio.read_jsonl(tasks.transcript_path(home, task.id))
    argvs = [e["event"]["argv"] for e in entries if "argv" in e.get("event", {})]
    assert argvs and all("improve the README" not in arg for arg in argvs[0])
    text = transcript_text(home, task.id)
    assert "improve the README" in text  # the prompt reached the harness via stdin…
    assert f"Task ID: {task.short_id}" in text  # …preamble included


def test_inject_pump_closes_an_idle_run(home: Path, project: str, monkeypatch):
    """With nothing in the inbox the pump closes stdin at the first turn
    boundary — an inject-mode run still ends on its own."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "inject")
    harness_config(home, extra='inject = "stream-json"\n')
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0
    fresh = TaskStore(home).get(task.id)
    assert fresh.runs[0].exit_code == 0


def test_harness_reports_back_through_the_cli(home: Path, project: str, monkeypatch):
    """The cooperative return channel end to end: the harness subprocess calls
    `python -m quorum task report` against QUORUM_HOME and the task's status,
    reports file, and board all reflect it."""
    monkeypatch.setenv("FAKE_HARNESS_MODE", "report")
    monkeypatch.setenv("FAKE_HARNESS_PR_URL", "https://example.com/pr/9")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "done"
    assert fresh.pr_url == "https://example.com/pr/9"
    assert any(m.type == "task.done" for m in MessageBus(home).read_topic(tasks.BOARD_TOPIC))


def test_failing_harness_records_exit_code_and_no_status_change(home: Path, project: str, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "fail")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    assert run_task(home, config, task.id) == 3
    fresh = TaskStore(home).get(task.id)
    assert fresh.status == "queued"  # the runner never sets status itself
    assert fresh.runs[0].exit_code == 3


def test_no_worktree_runs_in_project_dir(home: Path, project: str, tmp_path: Path):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake", use_worktree=False)
    run_task(home, config, task.id)
    assert f"CWD| {(tmp_path / 'proj').resolve()}" in transcript_text(home, task.id)


def test_auto_commit_captures_work_the_harness_left_behind(home: Path, project: str, monkeypatch):
    """The safety net's hard guarantee: with `[tasks].auto_commit` on, work a
    harness left uncommitted lands on the task branch, which outlives the
    worktree — no policy, no status change, no push."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    assert run_task(home, config, task.id) == 0

    workdir = tasks.worktree_path(home, task.id)
    assert git_out(workdir, "status", "--porcelain") == ""  # nothing stranded
    assert git_out(workdir, "log", "-1", "--pretty=%s") == runner.AUTO_COMMIT_MESSAGE
    assert "scratch.txt" in git_out(workdir, "show", "--name-only", "--pretty=", "HEAD")
    assert git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD") == f"quorum/{task.short_id}"
    assert "auto-committed 1 path(s)" in transcript_text(home, task.id)
    fresh = TaskStore(home).get(task.id)
    assert fresh.runs[0].exit_code == 0 and fresh.status == "queued"


def test_auto_commit_is_off_by_default(home: Path, project: str, monkeypatch):
    """Default off: the tree stays dirty and stranded-work detection sees it."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")

    run_task(home, config, task.id)

    workdir = tasks.worktree_path(home, task.id)
    assert "scratch.txt" in git_out(workdir, "status", "--porcelain")
    assert git_out(workdir, "log", "-1", "--pretty=%s") == "init"
    assert tasks.workdir_git_state(TaskStore(home).get(task.id))["dirty"] == 1


def test_auto_commit_never_touches_a_no_worktree_checkout(
    home: Path, project: str, tmp_path: Path, monkeypatch
):
    """A `--no-worktree` task runs in the user's own checkout on whatever
    branch they had out; committing there is not quorum's to do."""
    monkeypatch.setenv("FAKE_HARNESS_WRITE", "scratch.txt")
    harness_config(home, tasks_extra="auto_commit = true\n")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake", use_worktree=False)

    run_task(home, config, task.id)

    repo = tmp_path / "proj"
    assert "scratch.txt" in git_out(repo, "status", "--porcelain")
    assert git_out(repo, "log", "-1", "--pretty=%s") == "init"


def test_auto_commit_is_a_no_op_on_a_clean_tree(tmp_path: Path):
    """A harness that committed its own work gets no empty extra commit."""
    repo = make_repo(tmp_path)
    assert runner.auto_commit_workdir(repo) == ""
    assert git_out(repo, "log", "-1", "--pretty=%s") == "init"


def test_auto_commit_failure_is_recorded_not_raised(home: Path, project: str, tmp_path: Path):
    """A net that cannot fire leaves a note and the dirty tree behind — never
    an exception that would cost the run its record."""
    task = TaskStore(home).add(project, "x", "fake")
    loose = tmp_path / "loose"
    loose.mkdir()
    with pytest.raises(RunnerError, match="git status failed"):
        runner.auto_commit_workdir(loose)

    runner._auto_commit_after_run(home, task, loose)  # same failure, absorbed

    assert "auto-commit failed" in transcript_text(home, task.id)


def test_missing_harness_and_unknown_task_fail_loud(home: Path, project: str):
    (home / "config.toml").write_text("")
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "ghost")
    with pytest.raises(RunnerError, match="no \\[harness.ghost\\]"):
        run_task(home, config, task.id)
    with pytest.raises(RunnerError, match="no task matching"):
        run_task(home, config, "zzzz")


def test_second_concurrent_run_is_refused(home: Path, project: str):
    harness_config(home)
    config = load_config(home)
    task = TaskStore(home).add(project, "x", "fake")
    lock = tasks.runner_lock_path(home, task.id)
    # a live *foreign* pid: our own would read as a stale same-process lock
    lock.write_text('{"pid": 1}\n')
    try:
        with pytest.raises(RunnerError, match="already has a live run"):
            run_task(home, config, task.id)
    finally:
        lock.unlink()


def test_workdir_git_state_tracks_dirty_and_unpushed(home: Path, tmp_path: Path):
    repo = make_repo(tmp_path)
    store = TaskStore(home)
    task = store.add(project="proj", prompt="p", harness="fake")
    assert tasks.workdir_git_state(task) is None  # no workdir resolved yet

    task = store.update(task.id, workdir=str(repo))
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 0
    assert state["unpushed"] is None  # no remote: pushing does not apply

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo_git(repo, "remote", "add", "origin", str(bare))
    repo_git(repo, "push", "-q", "-u", "origin", "HEAD")
    assert tasks.workdir_git_state(task)["unpushed"] == 0

    (repo / "work.txt").write_text("wip")
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 1
    assert state["unpushed"] == 0

    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "wip")
    state = tasks.workdir_git_state(task)
    assert state["dirty"] == 0
    assert state["unpushed"] == 1
    assert state["branch"]

    repo_git(repo, "push", "-q", "origin", "HEAD")
    assert tasks.workdir_git_state(task)["unpushed"] == 0


def test_task_rows_surface_git_state_but_skip_settled_tasks(home: Path, tmp_path: Path):
    from datetime import timedelta

    from quorum import views

    repo = make_repo(tmp_path)
    store = TaskStore(home)
    task = store.add(project="proj", prompt="p", harness="fake")
    store.update(task.id, workdir=str(repo), status="executing")
    (repo / "work.txt").write_text("wip")

    row = views.task_rows(home)[0]
    assert row["git"]["dirty"] == 1

    # long-terminal tasks stop being probed (views refresh constantly)
    old = fsio.utc_now() - timedelta(hours=views.GIT_PROBE_TERMINAL_HOURS + 1)
    store.update(task.id, now=old, status="done")
    assert views.task_rows(home)[0]["git"] is None
