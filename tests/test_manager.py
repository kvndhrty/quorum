"""Manager agent tests: the harness-driven supervision loop.

The manager's harness is the fake in tests/bin/fake_harness.py running in a
manager mode; the tasks it acts on use the same fake in echo mode. Each
[harness.<name>] table pins its own FAKE_HARNESS_MODE via `env`, which is
exactly how the mechanism separates concerns in production too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from quorum import fsio, notes, runner, tasks
from quorum.actor import notes_path, run_snapshot_path, runs_dir, usage_path
from quorum.agent import AgentContext
from quorum.agents import manager
from quorum.agents.manager import (
    LOOP_WINDOW_CALLS,
    STALL_QUIET_MINUTES,
    Manager,
    build_digest,
    journal_path,
    loop_signal,
    transcript_path,
)
from quorum.config import TasksConfig, load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore
from test_tasks import make_repo, repo_git

FAKE = str(Path(__file__).parent / "bin" / "fake_harness.py")


def write_config(
    home: Path,
    manager_mode: str,
    extra_settings: str = "",
    run_timeout_seconds: int = 60,
    mgr_inject: bool = False,
    extra_harness: str = "",
    manager_usage: str = "",
    manager_note: str = "",
) -> None:
    (home / "config.toml").write_text(
        "[tasks]\n"
        'default_harness = "tasktool"\n'
        "[harness.tasktool]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        'env = { FAKE_HARNESS_MODE = "echo" }\n'
        f"{extra_harness}"
        "[harness.mgr]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        f'env = {{ FAKE_HARNESS_MODE = "{manager_mode}"'
        + (f', FAKE_HARNESS_USAGE = "{manager_usage}"' if manager_usage else "")
        + (f', FAKE_HARNESS_NOTE = "{manager_note}"' if manager_note else "")
        + " }\n"
        + ('inject = "stream-json"\n' if mgr_inject else "")
        + "[agents.manager]\n"
        'type = "manager"\n'
        'schedule = "every 5m"\n'
        "auto_pause = false\n"
        "[agents.manager.settings]\n"
        'harness = "mgr"\n'
        f"run_timeout_seconds = {run_timeout_seconds}\n"
        f"{extra_settings}"
    )


def make_manager(home: Path, clock) -> Manager:
    config = load_config(home)
    ctx = AgentContext(
        home=home, name="manager",
        settings=config.agents["manager"].settings, config=config, now=clock,
    )
    return Manager(ctx)


def manager_transcript_text(home: Path) -> str:
    return "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(transcript_path(home))
    )


@pytest.fixture
def project(home: Path, tmp_path: Path) -> str:
    repo = make_repo(tmp_path, "mgrproj")
    ProjectRegistry(home).add(repo, name="mgrproj")
    return "mgrproj"


def test_idle_home_skips_the_harness_run(home: Path, clock):
    write_config(home, "manager_act")
    make_manager(home, clock).tick()
    assert not transcript_path(home).exists()  # no tasks, no directives: no run


def test_full_loop_launch_nudge_journal_and_directives(home: Path, clock, project: str):
    write_config(home, "manager_act")
    task = TaskStore(home).add(project, "tidy up the docs", "tasktool")
    bus = MessageBus(home)
    bus.send("user", "manager", type="directive", text="prioritize the docs work today")

    make_manager(home, clock).tick()

    # the manager's harness actually ran the queued task (foreground in the fake)
    fresh = TaskStore(home).get(task.id)
    assert len(fresh.runs) == 1 and fresh.runs[0].exit_code == 0

    # every action was auto-journaled under one run id, actor=manager
    entries = fsio.read_jsonl(journal_path(home))
    actions = [e["action"] for e in entries]
    assert actions == ["task.run", "task.nudge", "note"]
    run_ids = {e["run"] for e in entries}
    assert len(run_ids) == 1 and run_ids != {""}
    assert {e["actor"] for e in entries} == {"manager"}
    assert entries[0]["target"] == task.short_id
    assert entries[0]["target_status"] == "queued"

    # the digest reached the harness: queued line + the user's directive
    text = manager_transcript_text(home)
    assert f"- [queued] {task.short_id}" in text
    assert "prioritize the docs work today" in text

    # directive consumed after a successful run; nudge waits in the task inbox
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "new") == []
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "cur") == []
    nudges = fsio.sorted_entries(bus.inbox_dir / tasks.inbox_name(task.id) / "new")
    assert len(nudges) == 1
    assert fsio.read_json(nudges[0])["from"] == "manager"


def test_mid_run_directive_reaches_a_live_manager_run(
    home: Path, clock, project: str, monkeypatch
):
    """`quorum manager tell` while a tick's harness is in flight: the pump
    forwards the directive as a user turn instead of holding it for the next
    tick (the fake posts the tell itself mid-run, for determinism)."""
    monkeypatch.setattr(runner, "GUIDANCE_POLL_SECONDS", 0.05)
    monkeypatch.setenv("FAKE_HARNESS_INJECT_POST", "tell")
    write_config(home, "inject", mgr_inject=True)
    TaskStore(home).add(project, "x", "tasktool")  # something active, so the tick runs

    make_manager(home, clock).tick()

    raw = transcript_path(home).read_text()
    assert "pause new launches until tests pass" in raw  # delivered into the live run
    assert '"role": "user"' in raw  # ...as a stream-json user turn
    bus = MessageBus(home)
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "new") == []
    assert fsio.sorted_entries(bus.inbox_dir / "manager" / "cur") == []


def test_action_cap_refuses_and_bounds_the_journal(home: Path, clock, project: str):
    write_config(home, "manager_flood", extra_settings="max_actions_per_run = 2\n")
    TaskStore(home).add(project, "x", "tasktool")

    make_manager(home, clock).tick()

    entries = fsio.read_jsonl(journal_path(home))
    actions = [e["action"] for e in entries]
    assert actions.count("task.nudge") == 2  # the cap, exactly
    assert "REFUSED|" in manager_transcript_text(home)

    # ...and the refusal left a mark the *next* run can read (#59): one
    # cap.hit for the run, however many further actions it tried.
    hits = [e for e in entries if e["action"] == "cap.hit"]
    assert len(hits) == 1
    assert hits[0]["actor"] == "manager" and hits[0]["run"] == entries[0]["run"]
    assert "task.nudge" in hits[0]["args"] and "cap (2)" in hits[0]["args"]

    # the journal section of the next digest is where the manager meets it
    digest = build_digest(home, TaskStore(home).list(), clock(), [])
    assert "cap.hit" in digest


def test_failed_harness_raises_and_returns_directives(home: Path, clock, project: str):
    write_config(home, "fail")
    TaskStore(home).add(project, "x", "tasktool")
    bus = MessageBus(home)
    bus.send("user", "manager", type="directive", text="do the thing")

    with pytest.raises(RuntimeError, match="exited 3"):
        make_manager(home, clock).tick()

    # the directive went straight back to new/, ready for the next tick
    assert len(fsio.sorted_entries(bus.inbox_dir / "manager" / "new")) == 1


def test_hung_harness_is_killed_at_the_run_timeout(home: Path, clock, project: str):
    write_config(home, "hang", run_timeout_seconds=1)
    TaskStore(home).add(project, "x", "tasktool")
    with pytest.raises(RuntimeError, match="timed out"):
        make_manager(home, clock).tick()


def test_missing_harness_config_raises(home: Path, clock, project: str):
    (home / "config.toml").write_text('[agents.manager]\ntype = "manager"\n')
    TaskStore(home).add(project, "x", "tasktool")
    with pytest.raises(RuntimeError, match="no usable harness"):
        make_manager(home, clock).tick()


def test_digest_liveness_quiet_time_and_journal_outcomes(home: Path, clock, project: str):
    write_config(home, "manager_act")
    store = TaskStore(home)
    queued = store.add(project, "queued work", "tasktool")
    stuck = store.add(project, "stuck work", "tasktool")
    store.update(stuck.id, status="executing")
    fsio.append_jsonl(tasks.transcript_path(home, stuck.id), {"at": "t", "line": "output"})
    path = tasks.transcript_path(home, stuck.id)
    stamp = path.stat().st_mtime - 3600
    os.utime(path, (stamp, stamp))
    fsio.append_jsonl(
        journal_path(home),
        {"at": "2026-08-24T00:00:00Z", "run": "R1", "actor": "manager",
         "action": "task.nudge", "target": stuck.short_id, "target_status": "executing"},
    )

    digest = build_digest(home, store.list(), clock(), directives=["focus on stuck work"])

    assert f"- [queued] {queued.short_id}" in digest
    assert "runner=dead" in digest
    stuck_line = next(line for line in digest.splitlines() if f"[executing] {stuck.short_id}" in line)
    quiet = int(stuck_line.split("quiet=")[1].rstrip("m"))
    assert quiet >= 59  # backdated an hour: the model sees real silence
    assert "status_then=executing status_now=executing (UNCHANGED since)" in digest
    assert "- focus on stuck work" in digest


def test_digest_surfaces_stranded_work(home: Path, clock, tmp_path: Path):
    repo = make_repo(tmp_path, "strandproj")
    store = TaskStore(home)
    finished = store.add(project="p", prompt="reported done, left dirt", harness="t")
    store.update(finished.id, workdir=str(repo), status="done")
    active = store.add(project="p", prompt="still working, dirty tree", harness="t")
    store.update(active.id, workdir=str(repo), status="executing")
    (repo / "wip.txt").write_text("uncommitted")

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "STRANDED-WORK dirty=1 unpushed=no-remote" in digest
    assert "git: branch=" in digest

    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "delivered")
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "STRANDED-WORK" not in digest
    assert "git: branch=" not in digest


def test_digest_surfaces_spend_and_flags_a_run_over_budget(home: Path, clock, project: str):
    """Surfacing: cost/tokens show up per task when the harness reported
    them, and a configured budget turns an expensive run into a digest
    line that also says what the runner's budget gate will do about it."""
    write_config(home, "manager_act")
    store = TaskStore(home)
    task = store.add(project, "expensive work", "tasktool")
    store.update(
        task.id,
        status="executing",
        runs=[
            {"started_at": "t0", "ended_at": "t1", "exit_code": 0,
             "usage": {"cost_usd": 0.5, "total_tokens": 5000, "events": 1}},
            {"started_at": "t2", "ended_at": "t3", "exit_code": 0,
             "usage": {"cost_usd": 2.0, "total_tokens": 40000, "events": 1}},
        ],
    )

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "usage: $2.50 · 45.0k tok over 2 reporting run(s)" in digest
    assert "BUDGET-EXCEEDED" not in digest  # no budget configured: nothing to exceed

    digest = build_digest(
        home, store.list(), clock(), directives=[], tasks_config=TasksConfig(max_cost_per_run=1.0)
    )
    assert (
        "BUDGET-EXCEEDED: run 2: cost $2.00 > max_cost_per_run $1.00 "
        "(next run gated; --force to override)" in digest
    )
    assert "run 1:" not in digest  # the cheap run is not indicted


def test_digest_marks_an_earlier_overage_as_cleared(home: Path, clock, project: str):
    """Only the last run gates: an over-budget run followed by a cheaper one
    is still reported (the spend happened) but the digest says the gate is
    clear, so the manager does not reach for --force it does not need."""
    write_config(home, "manager_act")
    store = TaskStore(home)
    task = store.add(project, "calmed down", "tasktool")
    store.update(
        task.id,
        status="executing",
        runs=[
            {"started_at": "t0", "ended_at": "t1", "exit_code": 0,
             "usage": {"cost_usd": 2.0, "total_tokens": 40000, "events": 1}},
            {"started_at": "t2", "ended_at": "t3", "exit_code": 0,
             "usage": {"cost_usd": 0.5, "total_tokens": 5000, "events": 1}},
        ],
    )
    digest = build_digest(
        home, store.list(), clock(), directives=[], tasks_config=TasksConfig(max_cost_per_run=1.0)
    )
    assert (
        "BUDGET-EXCEEDED: run 1: cost $2.00 > max_cost_per_run $1.00 "
        "(an earlier run; a later one cleared the gate)" in digest
    )
    assert "next run gated" not in digest


def test_digest_says_nothing_about_spend_a_harness_never_reported(
    home: Path, clock, project: str
):
    write_config(home, "manager_act")
    store = TaskStore(home)
    task = store.add(project, "quiet harness", "tasktool")
    store.update(task.id, status="executing",
                 runs=[{"started_at": "t0", "ended_at": "t1", "exit_code": 0}])

    digest = build_digest(
        home, store.list(), clock(), directives=[],
        tasks_config=TasksConfig(max_cost_per_run=0.01, max_tokens_per_run=1),
    )
    assert "usage:" not in digest and "BUDGET-EXCEEDED" not in digest


# --- possible-loop observation ----------------------------------------------


def tool_use(name: str, cmd: str, call_id: str = "c1") -> dict:
    """One claude-shaped transcript entry carrying a tool call."""
    return {
        "at": "2026-08-30T00:00:00Z",
        "event": {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": call_id, "name": name, "input": {"command": cmd}}
                ]
            },
        },
    }


def write_transcript(home: Path, task_id: str, entries: list[dict]) -> None:
    for e in entries:
        fsio.append_jsonl(tasks.transcript_path(home, task_id), e)


def digest_line(digest: str, short_id: str, prefix: str) -> str | None:
    return next(
        (line for line in digest.splitlines() if line.strip().startswith(prefix)), None
    )


def test_loop_signal_flags_a_repeated_call_and_ignores_varied_work():
    stuck = [tool_use("Bash", "pytest -x", f"c{i}") for i in range(8)]
    assert loop_signal(stuck) == {"tool": "Bash", "repeats": 8, "window": 8, "distinct": 1}

    varied = [tool_use("Bash", f"step {i}", f"c{i}") for i in range(8)]
    assert loop_signal(varied) is None


def mark_runner_alive(home: Path, task_id: str) -> None:
    tasks.runner_lock_path(home, task_id).write_text('{"pid": 1}\n')  # alive, never ours


def test_digest_flags_a_looping_task_but_not_a_varied_one(home: Path, clock, project: str):
    store = TaskStore(home)
    looping = store.add(project, "spinning", "tasktool")
    healthy = store.add(project, "progressing", "tasktool")
    store.update(looping.id, status="executing")
    store.update(healthy.id, status="executing")
    mark_runner_alive(home, looping.id)
    mark_runner_alive(home, healthy.id)
    write_transcript(home, looping.id, [tool_use("Bash", "pytest -x", f"c{i}") for i in range(6)])
    write_transcript(home, healthy.id, [tool_use("Bash", f"step {i}", f"c{i}") for i in range(6)])

    digest = build_digest(home, store.list(), clock(), directives=[])

    flagged = digest.split(f"[executing] {looping.short_id}")[1]
    assert "possible-loop: tool=Bash repeated=6x in last 6 tool calls (distinct=1)" in flagged
    assert "possible-loop" not in digest.split(f"[executing] {healthy.short_id}")[1]
    # the flag line itself carries a name and counts, not the argument payload
    # (adjacent out| lines may still quote raw events, truncated)
    assert "pytest -x" not in digest_line(digest, looping.short_id, "possible-loop")  # type: ignore[arg-type]


def test_loop_flag_needs_a_live_runner_and_current_run_evidence(
    home: Path, clock, project: str
):
    """The transcript is append-only: without these gates a dead task stays
    flagged forever, and a relaunched task is indicted by the previous run's
    spinning."""
    store = TaskStore(home)
    dead = store.add(project, "died mid-loop", "tasktool")
    store.update(dead.id, status="executing")
    write_transcript(home, dead.id, [tool_use("Bash", "pytest -x", f"c{i}") for i in range(6)])
    # no runner.lock: runner dead → no flag, however loopy the tail
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "possible-loop" not in digest

    relaunched = store.add(project, "recovering", "tasktool")
    store.update(relaunched.id, status="executing")
    mark_runner_alive(home, relaunched.id)
    old = [tool_use("Bash", "pytest -x", f"c{i}") for i in range(6)]
    for e in old:
        e["at"] = "2026-08-30T00:00:00Z"  # the previous run's spinning
    write_transcript(home, relaunched.id, old)
    prior = tasks.TaskRun(
        started_at="2026-08-30T00:00:00Z", ended_at="2026-08-30T01:00:00Z", exit_code=1
    )
    store.update(relaunched.id, runs=[prior.model_dump()])

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "possible-loop" not in digest  # stale evidence filtered out

    fresh = [tool_use("Bash", "pytest -x", f"n{i}") for i in range(6)]
    for e in fresh:
        e["at"] = "2026-08-30T02:00:00Z"  # the *current* run spinning again
    write_transcript(home, relaunched.id, fresh)
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "possible-loop" in digest


def test_loop_signal_stays_quiet_on_benign_transcripts():
    # a short tail: fewer calls than the threshold says nothing
    assert loop_signal([tool_use("Bash", "ls", f"c{i}") for i in range(3)]) is None
    # polling that interleaves real work: repeated, but not dominant
    poll = []
    for i in range(6):
        poll.append(tool_use("Bash", "git status", f"p{i}"))
        poll.append(tool_use("Edit", f"file{i}.py", f"e{i}"))
    assert loop_signal(poll) is None
    # retries with backoff: same tool, arguments differ each time
    assert loop_signal([tool_use("Fetch", f"url?attempt={i}", f"r{i}") for i in range(8)]) is None
    # entries carrying no structured events at all (raw stdout lines)
    assert loop_signal([{"at": "t", "line": "same log line"} for _ in range(20)]) is None
    assert loop_signal([]) is None


def codex_events(i: int, command: str) -> list[dict]:
    """One codex call as the CLI actually emits it: item.started AND
    item.completed, both carrying the full item with the same id."""
    item = {"id": f"i{i}", "item_type": "command_execution", "command": command}
    return [
        {"at": "t", "event": {"type": "item.started", "item": dict(item)}},
        {"at": "t", "event": {"type": "item.completed", "item": dict(item)}},
    ]


def test_loop_signal_reads_other_harness_shapes():
    # codex-shaped items: a different schema, the same loose normalization —
    # and the started/completed pair counts as ONE call, not two
    codex = [e for i in range(5) for e in codex_events(i, "make test")]
    signal = loop_signal(codex)
    assert signal is not None
    assert signal["tool"] == "command_execution" and signal["repeats"] == 5

    # healthy codex work with one legitimate re-run (a normal edit/test
    # cycle) must NOT flag — double-counting the event pairs used to
    # halve the threshold and fire exactly here
    healthy = [e for i, cmd in enumerate(
        ["ls", "make test", "vi a.py", "make test", "git diff", "git commit"]
    ) for e in codex_events(i, cmd)]
    assert loop_signal(healthy) is None


def test_loop_signal_ignores_pseudo_calls_and_nested_payloads():
    # a null tool_name on non-call events (hook/permission traffic) is not a
    # call; a constant stream of them must not fingerprint
    pseudo = [
        {"at": "t", "event": {"tool_name": None, "type": "status", "phase": "idle"}}
        for _ in range(20)
    ]
    assert loop_signal(pseudo) is None
    # tool-call-shaped dicts inside another call's arguments are data, not
    # calls this run made — four varied Writes must not flag
    nested = [
        tool_use("Write", f"file{i}.py", f"c{i}") for i in range(4)
    ]
    for i, e in enumerate(nested):
        e["event"]["message"]["content"][0]["input"] = {
            "path": f"file{i}.py",
            "content": [{"type": "tool_call", "name": "X", "args": {"n": 1}}] * 2,
        }
    assert loop_signal(nested) is None


def test_loop_signal_fingerprints_custom_shapes_predictably():
    # argv-style calls (a common BYO-harness shape): varied work stays quiet,
    # a genuine repeat fires — per-call counters must not make it unique
    varied = [
        {"at": "t", "event": {"type": "tool_call", "argv": ["step", str(i)], "seq": i}}
        for i in range(8)
    ]
    assert loop_signal(varied) is None
    stuck = [
        {"at": "t", "event": {"type": "tool_call", "argv": ["make", "test"], "seq": i}}
        for i in range(8)
    ]
    signal = loop_signal(stuck)
    assert signal is not None and signal["repeats"] == 8
    # no recognized arg key at all: the name alone is the fingerprint — a
    # coarser signal, but it fires predictably instead of never
    unknown = [
        {"at": "t", "event": {"type": "tool_call", "tool": "poll", "weird_args": {"n": i}}}
        for i in range(8)
    ]
    signal = loop_signal(unknown)
    assert signal is not None and signal["tool"] == "poll"


def test_loop_signal_only_scores_the_most_recent_calls():
    # an old loop that the harness has since escaped is not flagged: the
    # window is the *last* LOOP_WINDOW_CALLS calls, not the whole tail
    escaped = [tool_use("Bash", "pytest -x", f"o{i}") for i in range(20)]
    escaped += [tool_use("Edit", f"fix{i}.py", f"n{i}") for i in range(LOOP_WINDOW_CALLS)]
    assert loop_signal(escaped) is None


# -- task dependencies (#31) ----------------------------------------------


def test_digest_marks_waiting_failed_and_cycled_dependencies(home: Path, clock, project: str):
    write_config(home, "manager_act")
    store = TaskStore(home)
    upstream = store.add(project, "build it", "tasktool")
    dependent = store.add(project, "review it", "tasktool", depends_on=[upstream.id])

    digest = build_digest(home, store.list(), clock(), directives=[])
    line = next(line for line in digest.splitlines() if f"[queued] {dependent.short_id}" in line)
    assert f"waiting-on={upstream.short_id}" in line
    assert "DEP-FAILED" not in digest
    assert f"deps: waiting on {upstream.short_id}" in digest

    # a dependency that ends blocked never satisfies: an observation, and the
    # dependent is no longer "waiting" on anything
    tasks.report(home, upstream.id, "blocked", "needs a human")
    digest = build_digest(home, store.list(), clock(), directives=[])
    line = next(line for line in digest.splitlines() if f"[queued] {dependent.short_id}" in line)
    assert "waiting-on=" not in line and "DEP-FAILED" in line
    assert f"DEP-FAILED: dependency {upstream.short_id} ended blocked" in digest

    # a hand-edited cycle is flagged, not crashed on
    store.update(upstream.id, status="queued", depends_on=[dependent.id])
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "DEP-CYCLE" in digest


def test_digest_says_nothing_about_a_task_without_dependencies(
    home: Path, clock, project: str
):
    write_config(home, "manager_act")
    store = TaskStore(home)
    store.add(project, "ordinary work", "tasktool")
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "waiting-on" not in digest and "deps:" not in digest


def test_manager_drives_a_two_task_chain_in_order(home: Path, clock, project: str):
    """End to end with the fake harness: the manager launches the upstream
    task, and only launches the dependent one after the upstream reports
    `done` — the prompt rule doing the deciding, with the runner's refusal
    behind it."""
    write_config(
        home,
        "manager_chain",
        extra_harness=(
            "[harness.reporter]\n"
            f'start = ["{sys.executable}", "{FAKE}"]\n'
            'env = { FAKE_HARNESS_MODE = "report" }\n'
        ),
    )
    store = TaskStore(home)
    upstream = store.add(project, "build it", "reporter")
    dependent = store.add(project, "review it", "reporter", depends_on=[upstream.id])

    make_manager(home, clock).tick()

    text = manager_transcript_text(home)
    assert f"ACT| task run {upstream.short_id} -> exit 0" in text
    assert f"SKIP| {dependent.short_id} waiting on its dependencies" in text
    assert store.get(upstream.id).status == "done"  # the fake reported it
    assert store.get(dependent.id).runs == []  # not a single run spent early

    make_manager(home, clock).tick()

    text = manager_transcript_text(home)
    assert f"ACT| task run {dependent.short_id} -> exit 0" in text
    assert len(store.get(dependent.id).runs) == 1
    assert store.get(dependent.id).status == "done"
    # the dependent's prompt carried the upstream's outcome
    dep_text = "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(tasks.transcript_path(home, dependent.id))
    )
    assert f"- {upstream.short_id}: status=done" in dep_text
# -- what supervision itself costs (#32) -------------------------------------


def test_the_manager_records_and_reads_back_its_own_spend(
    home: Path, clock, project: str
):
    """The manager's runs are the steadiest recurring cost in a live home, so
    they land in a ledger of their own and come back in the next digest."""
    from quorum import views

    write_config(home, "manager_act", manager_usage="0.30")
    TaskStore(home).add(project, "something to manage", "tasktool")

    make_manager(home, clock).tick()

    entries = fsio.read_jsonl(usage_path(home, "manager"))
    assert len(entries) == 1 and entries[0]["usage"]["cost_usd"] == 0.30

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "Your own runs have cost: last $0.30 · 11.0k tok" in digest

    row = next(r for r in views.agent_rows(home) if r["name"] == "manager")
    assert row["usage"]["total"]["cost_usd"] == 0.30
    assert row["usage_text"] == "last $0.30 · 11.0k tok"


def test_a_manager_harness_that_reports_no_spend_says_nothing(
    home: Path, clock, project: str
):
    from quorum import views

    write_config(home, "manager_act")
    TaskStore(home).add(project, "something to manage", "tasktool")

    make_manager(home, clock).tick()

    # the run is still counted (an unreported spend is unknown, not zero)
    entries = fsio.read_jsonl(usage_path(home, "manager"))
    assert len(entries) == 1 and entries[0]["usage"] is None
    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "Your own runs have cost" not in digest
    row = next(r for r in views.agent_rows(home) if r["name"] == "manager")
    assert row["usage"] is None and row["usage_text"] == ""


# -- self-observations: recent outcomes and the action budget (#59) ----------


def test_a_run_records_how_it_went_and_the_next_digest_shows_it(
    home: Path, clock, project: str
):
    """The ledger line is the record of a run, not only of its cost: a
    manager that has been timing out must be able to read that off its own
    digest — which is the one thing no amount of task detail tells it."""
    write_config(home, "manager_act")
    TaskStore(home).add(project, "something to manage", "tasktool")

    make_manager(home, clock).tick()

    entry = fsio.read_jsonl(usage_path(home, "manager"))[-1]
    assert entry["outcome"] == "ok"
    assert isinstance(entry["duration_seconds"], (int, float))
    assert entry["duration_seconds"] >= 0

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[], cap=7)
    assert "Your last run: ok " in digest
    assert "Actions this run: 0 of 7 (cap)" in digest


def test_a_timed_out_run_is_the_one_the_ledger_must_still_show(
    home: Path, clock, project: str
):
    """A timeout reports no usage at all, so its outcome is exactly the thing
    a spend-only ledger would lose — and the run before it is still `ok`."""
    write_config(home, "manager_act")
    TaskStore(home).add(project, "something to manage", "tasktool")
    make_manager(home, clock).tick()

    write_config(home, "hang", run_timeout_seconds=1)
    with pytest.raises(RuntimeError, match="timed out"):
        make_manager(home, clock).tick()

    outcomes = [e.get("outcome") for e in fsio.read_jsonl(usage_path(home, "manager"))]
    assert outcomes == ["ok", "timeout"]

    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    line = next(ln for ln in digest.splitlines() if ln.startswith("Your last 2 runs:"))
    assert "TIMEOUT" in line and line.index("ok") < line.index("TIMEOUT")
    assert "Your own runs have cost" not in digest  # neither run reported any


def test_a_crashed_run_is_recorded_as_raised(home: Path, clock, project: str):
    write_config(home, "fail")
    TaskStore(home).add(project, "something to manage", "tasktool")
    with pytest.raises(RuntimeError, match="exited 3"):
        make_manager(home, clock).tick()

    entry = fsio.read_jsonl(usage_path(home, "manager"))[-1]
    assert entry["outcome"] == "raised"
    digest = build_digest(home, TaskStore(home).list(), clock(), directives=[])
    assert "RAISED" in digest


def test_the_action_budget_is_in_every_digest_even_an_empty_home(home: Path, clock):
    """The budget line does not depend on a ledger existing: a manager's very
    first run still needs to know what it may spend."""
    write_config(home, "manager_act")
    digest = build_digest(home, [], clock(), directives=[])
    assert "Actions this run: 0 of 20 (cap)" in digest  # the default cap
    assert "Your last" not in digest


# -- issue intake (#62) ------------------------------------------------------


def test_the_digest_names_the_issue_a_task_came_from(home: Path, clock, project: str):
    """So the manager can tell a human "the task for #62 is done" — the
    short form on the line, the url one `task show` away."""
    url = "https://github.com/kvndhrty/quorum/issues/62"
    store = TaskStore(home)
    live = store.add(project, "issue work", "tasktool", issue_url=url)
    store.update(live.id, status="executing")
    finished = store.add(project, "earlier issue work", "tasktool", issue_url=url)
    store.update(finished.id, status="done")
    plain = store.add(project, "prompt work", "tasktool")
    store.update(plain.id, status="executing")

    digest = build_digest(home, store.list(), clock(), directives=[])

    live_line = next(ln for ln in digest.splitlines() if f"[executing] {live.short_id}" in ln)
    assert "issue=#62" in live_line
    done_line = next(ln for ln in digest.splitlines() if f"[done] {finished.short_id}" in ln)
    assert "issue=#62" in done_line
    plain_line = next(ln for ln in digest.splitlines() if f"[executing] {plain.short_id}" in ln)
    assert "issue=" not in plain_line  # an ordinary task's line is unchanged


# -- perpetual tasks (#12) ---------------------------------------------------


def test_a_perpetual_task_is_marked_and_never_flagged_as_looping(
    home: Path, clock, project: str
):
    """Repetition is the job for a perpetual task, so the digest marks it and
    withholds the one observation that would read it as stuck."""
    store = TaskStore(home)
    forever = store.add(project, "watch CI and fix what breaks", "tasktool", perpetual=True)
    ordinary = store.add(project, "one-off work", "tasktool")
    store.update(forever.id, status="cycle-9")
    store.update(ordinary.id, status="executing")
    for task in (forever, ordinary):
        mark_runner_alive(home, task.id)
        write_transcript(home, task.id, [tool_use("Bash", "gh pr checks", f"c{i}") for i in range(6)])

    digest = build_digest(home, store.list(), clock(), directives=[])

    marked = digest.split(f"[cycle-9] {forever.short_id}")[1]
    assert marked.splitlines()[0].endswith("perpetual=true")
    assert "possible-loop" not in marked.split("- [")[0]
    # the identical transcript on an ordinary task is still flagged
    assert "possible-loop" in digest.split(f"[executing] {ordinary.short_id}")[1]


def test_a_perpetual_task_that_reported_done_is_observed_not_forgotten(
    home: Path, clock, project: str
):
    """Terminal tasks drop out of the active list, so the one thing a
    perpetual task must never do would otherwise be invisible."""
    store = TaskStore(home)
    ended = store.add(project, "watch CI", "tasktool", perpetual=True)
    stopped = store.add(project, "watch builds", "tasktool", perpetual=True)
    ordinary = store.add(project, "one-off", "tasktool")
    store.update(ended.id, status="done")
    store.update(stopped.id, status="cancelled")
    store.update(ordinary.id, status="done")

    digest = build_digest(home, store.list(), clock(), directives=[])

    assert f"PERPETUAL-ENDED {ended.short_id}: reported 'done'" in digest
    assert stopped.short_id not in digest.split("## Active tasks")[0]
    assert ordinary.short_id not in digest.split("## Active tasks")[0]


# -- the notebook (a separate memory, #35) ----------------------------------


# -- hung sessions: STALLED, stop, fresh sessions -------------------------


def hold_the_runner_lock(home: Path, task_id: str) -> subprocess.Popen:
    """A real process in its own session holding a task's runner.lock.

    `task stop` kills process *groups* for real, so a stub pid (the pid-1
    trick the passive-observation tests use) would be both a lie and a
    disaster. Nothing reaps it: the killed process lingers as a zombie in its
    group, which the stop has to read as dead all the same.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    lock = tasks.runner_lock_path(home, task_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_json(
        lock,
        {"role": "task-runner", "task": task_id, "pid": proc.pid,
         "started_at": fsio.iso(fsio.utc_now())},
    )
    return proc


def age_the_transcript(home: Path, task_id: str, minutes: int) -> None:
    """One line of output, last written `minutes` ago — a silent harness."""
    write_transcript(home, task_id, [{"at": fsio.iso(fsio.utc_now()), "line": "PROMPT| working"}])
    old = time.time() - minutes * 60
    os.utime(tasks.transcript_path(home, task_id), (old, old))


def test_digest_flags_a_silent_live_runner_as_stalled(home: Path, clock, project: str):
    store = TaskStore(home)
    quiet = store.add(project, "hung task", "tasktool")
    busy = store.add(project, "healthy task", "tasktool")
    for t in (quiet, busy):
        mark_runner_alive(home, t.id)
    age_the_transcript(home, quiet.id, minutes=STALL_QUIET_MINUTES + 10)
    age_the_transcript(home, busy.id, minutes=STALL_QUIET_MINUTES - 10)

    digest = build_digest(home, store.list(), clock(), [])

    assert "STALLED" in digest_line(digest, quiet.short_id, f"- [queued] {quiet.short_id}")
    assert "STALLED" not in digest_line(digest, busy.short_id, f"- [queued] {busy.short_id}")
    assert "An observation, not a verdict" in digest


def test_a_dead_runner_is_never_stalled_only_relaunchable(home: Path, clock, project: str):
    """A silent transcript with no live runner is just a task to relaunch —
    flagging it STALLED would send the manager stopping a run that is over."""
    store = TaskStore(home)
    task = store.add(project, "x", "tasktool")
    age_the_transcript(home, task.id, minutes=STALL_QUIET_MINUTES * 3)

    digest = build_digest(home, store.list(), clock(), [])

    assert "STALLED" not in digest
    assert manager.stall_minutes(home, task, clock()) >= STALL_QUIET_MINUTES


def test_a_first_run_that_never_printed_can_still_be_stalled(
    home: Path, clock, project: str
):
    """The hang that motivated all this (#24: a stream-json harness blocked on
    stdin) produces no output at all, so there is no transcript to age. The
    live run's own start answers instead — otherwise the loudest hang there is
    would be the one stall the digest cannot see."""
    store = TaskStore(home)
    task = store.add(project, "hung on its first turn", "tasktool")
    started = clock() - timedelta(minutes=STALL_QUIET_MINUTES + 5)
    lock = tasks.runner_lock_path(home, task.id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_json(
        lock,
        {"role": "task-runner", "task": task.id, "pid": 1, "started_at": fsio.iso(started)},
    )
    assert not tasks.transcript_path(home, task.id).exists()

    assert manager.stall_minutes(home, task, clock()) == STALL_QUIET_MINUTES + 5
    digest = build_digest(home, store.list(), clock(), [])

    assert "STALLED" in digest_line(digest, task.short_id, f"- [queued] {task.short_id}")


def test_a_young_run_with_no_transcript_is_not_stalled(home: Path, clock, project: str):
    store = TaskStore(home)
    task = store.add(project, "just launched", "tasktool")
    mark_runner_alive(home, task.id)  # no started_at: the lock's own mtime, now

    assert manager.stall_minutes(home, task, clock()) < STALL_QUIET_MINUTES
    assert "STALLED" not in build_digest(home, store.list(), clock(), [])


def test_digest_counts_stops_fresh_sessions_and_a_stalled_run(home: Path, clock, project: str):
    store = TaskStore(home)
    task = store.add(project, "x", "tasktool")
    store.update(task.id, runs=[
        tasks.TaskRun(started_at="2026-01-01T00:00:00Z", stopped=True).model_dump(),
        tasks.TaskRun(started_at="2026-01-01T01:00:00Z", fresh_session=True).model_dump(),
        tasks.TaskRun(started_at="2026-01-01T02:00:00Z", fresh_session=True, stalled=True).model_dump(),
    ])

    digest = build_digest(home, store.list(), clock(), [])

    line = digest_line(digest, task.short_id, f"- [queued] {task.short_id}")
    assert "stopped=1" in line and "fresh_sessions=2" in line and "last-run=stalled" in line
    assert "the runner's own stall watchdog" in digest


def test_a_task_nobody_restarted_carries_no_restart_marks(home: Path, clock, project: str):
    store = TaskStore(home)
    task = store.add(project, "x", "tasktool")
    digest = build_digest(home, store.list(), clock(), [])
    line = digest_line(digest, task.short_id, f"- [queued] {task.short_id}")
    assert "stopped=" not in line and "fresh_sessions=" not in line and "STALLED" not in line


def test_manager_stops_resumes_then_restarts_fresh_and_finally_escalates(
    home: Path, clock, project: str
):
    """The whole hung-session policy, driven by a manager harness that reads
    each step off the digest's own marks (prompts/manager.md item 7)."""
    write_config(home, "manager_restart")
    store = TaskStore(home)
    task = store.add(project, "parse the logs", "tasktool")
    hung = hold_the_runner_lock(home, task.id)
    age_the_transcript(home, task.id, minutes=STALL_QUIET_MINUTES + 15)

    make_manager(home, clock).tick()  # 1: STALLED -> stop, then resume

    assert not fsio.pid_alive(hung.pid)  # the hung run is really gone
    fresh = store.get(task.id)
    assert fresh.status == "queued"  # stop is not cancel
    assert fresh.runs[0].stopped and fresh.runs[1].stopped is False

    make_manager(home, clock).tick()  # 2: stopped once already -> fresh session
    make_manager(home, clock).tick()  # 3: still failing -> a second fresh session

    fresh = store.get(task.id)
    assert [r.fresh_session for r in fresh.runs] == [False, False, True, True]
    assert "continue there" in "\n".join(
        e.get("line", "") for e in fsio.read_jsonl(tasks.transcript_path(home, task.id))
    )  # the new session was told what the old one had done

    make_manager(home, clock).tick()  # 4: two fresh restarts -> escalate

    attention = MessageBus(home).read_topic("attention")
    assert len(attention) == 1 and task.short_id in attention[0].payload["text"]
    actions = [e["action"] for e in fsio.read_jsonl(journal_path(home)) if e["action"] != "note"]
    assert actions == [
        "task.stop", "task.run",
        "task.nudge", "task.run",
        "task.nudge", "task.run",
        "board.post",
    ]



def notebook_section(digest: str) -> list[str]:
    """The digest's notebook block: the lines after its header, up to the
    blank line that ends the section."""
    body = digest.split(notes.SECTION_HEADER)[1]
    return body.split("\n\n")[0].strip().splitlines()


def test_a_note_written_this_tick_is_in_the_next_ticks_prompt(
    home: Path, clock, project: str
):
    """The whole point: the manager has no memory between runs except what
    the digest hands it, and a standing note has to survive that gap."""
    standing = "the api PR is waiting on the human - do not relaunch it"
    write_config(home, "manager_remember", manager_note=standing)
    TaskStore(home).add(project, "something to manage", "tasktool")

    make_manager(home, clock).tick()
    first = manager_transcript_text(home)
    assert "ACT| remember -> exit 0" in first
    # tick one saw an empty notebook — the note did not exist when it started
    assert notes.EMPTY_LINE in first
    assert standing not in first

    clock.advance(minutes=5)
    make_manager(home, clock).tick()
    prompt = "\n".join(
        line for line in manager_transcript_text(home).splitlines()
        if line.startswith("PROMPT|")
    )
    assert notes.SECTION_HEADER in prompt
    assert standing in prompt
    # and it is attributed to the manager itself, tagged with its run
    written = notes.active(home)
    assert written[0]["sender"] == "manager" and written[0]["run_id"]


def test_a_retired_or_expired_note_is_not_in_the_digest(home: Path, clock):
    now = clock()
    keep = notes.remember(home, "the user wants at most two tasks running", now=now)
    retired = notes.remember(home, "wait for the 0.2.0 release before merging", now=now)
    notes.remember(home, "codex is rate-limited today", ttl_days=1, now=now)
    notes.forget(home, retired["id"], now=now)

    later = clock.advance(days=2)
    section = notebook_section(build_digest(home, [], later, []))
    assert any(keep["text"] in line for line in section)
    assert not any("0.2.0" in line for line in section)   # retired
    assert not any("rate-limited" in line for line in section)  # expired
    assert len(section) == 1  # the one live note, and nothing else


def test_noisy_tasks_cannot_shrink_the_notebook(home: Path, clock, project: str):
    """Read-side crowding is the failure the reserved slot exists to prevent:
    the task section grows with the number of live tasks, the notebook must
    not pay for it."""
    for i in range(6):
        notes.remember(home, f"standing fact {i}")
    quiet = notebook_section(build_digest(home, [], clock(), []))

    store = TaskStore(home)
    noisy = []
    for i in range(12):
        task = store.add(project, f"noisy task {i} " + "y" * 400, "tasktool")
        for r in range(5):
            tasks.report(home, task.id, "executing", "z" * 400 + f" report {r}")
        noisy.append(store.get(task.id))

    digest = build_digest(home, noisy, clock(), [])
    assert len(digest) > 8_000  # the task section really did get big
    assert notebook_section(digest) == quiet
    # and the notebook comes before the first task line
    assert digest.index(notes.SECTION_HEADER) < digest.index("## Active tasks")


def test_a_malformed_note_line_does_not_fail_the_tick(home: Path, clock):
    """The digest build is the one thing that must never raise over a file
    anyone can hand-edit: a bad line there would fail every tick, forever."""
    good = notes.remember(home, "the user wants at most two tasks running", now=clock())
    with open(notes_path(home), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": 7, "ts": "2026-09-01T00:00:00Z", "text": "int id"}) + "\n")
        f.write("not json at all\n")

    section = notebook_section(build_digest(home, [], clock(), []))
    assert any(good["text"] in line for line in section)
    assert not any("int id" in line for line in section)


def test_house_rules_from_the_overlay_reach_the_manager_run(
    home: Path, clock, project: str
):
    """prompts/manager.local.md is how a home adds policy without forking the
    packaged constitution and stranding itself on an old default (#37)."""
    write_config(home, "echo")
    TaskStore(home).add(project, "tidy up the docs", "tasktool")
    (home / "prompts" / "manager.local.md").write_text(
        "House rules for this home: never run more than two tasks at once.\n"
    )

    make_manager(home, clock).tick()

    text = manager_transcript_text(home)
    assert "never run more than two tasks at once" in text
    assert "You are the manager of a quorum home" in text  # the packaged default
    # the header comment still *documents* the key, hence the line anchor
    assert "PROMPT| {local}" not in text


# --- overlap observation ------------------------------------------------------


def make_origin(tmp_path: Path, repo: Path, name: str = "origin.git") -> Path:
    """A bare remote with origin/HEAD set — what a `git clone` gives a project."""
    import subprocess

    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo_git(repo, "remote", "add", "origin", str(bare))
    repo_git(repo, "push", "-q", "-u", "origin", "HEAD")
    repo_git(repo, "remote", "set-head", "origin", "-a")
    return bare


def add_worktree(home: Path, repo: Path, store: TaskStore, project: str, prompt: str):
    """A task with a real worktree, forked from the project checkout the way
    the runner does it (branch quorum/<short-id>)."""
    task = store.add(project=project, prompt=prompt, harness="t")
    workdir = tasks.worktree_path(home, task.id)
    repo_git(repo, "worktree", "add", str(workdir), "-b", f"quorum/{task.short_id}")
    return store.update(task.id, workdir=str(workdir), status="executing")


def test_overlap_signal_sees_shared_paths_and_ignores_disjoint_work(home: Path, tmp_path: Path):
    from quorum.agents.manager import overlap_signal

    repo = make_repo(tmp_path, "ovproj")
    make_origin(tmp_path, repo)
    store = TaskStore(home)
    a = add_worktree(home, repo, store, "ovproj", "edit the readme and add a")
    b = add_worktree(home, repo, store, "ovproj", "edit the readme and add b")
    c = add_worktree(home, repo, store, "ovproj", "only touch c")

    # a commits its edits; b leaves them uncommitted (a live task mid-work);
    # c's change is untracked. All three shapes must count.
    (Path(a.workdir) / "README.md").write_text("a's version")
    (Path(a.workdir) / "a.txt").write_text("a")
    repo_git(Path(a.workdir), "add", ".")
    repo_git(Path(a.workdir), "commit", "-qm", "a's work")
    (Path(b.workdir) / "README.md").write_text("b's version")
    (Path(b.workdir) / "b.txt").write_text("b")
    (Path(c.workdir) / "c.txt").write_text("c")

    assert tasks.worktree_changed_paths(a) == {"README.md", "a.txt"}
    assert tasks.worktree_changed_paths(b) == {"README.md", "b.txt"}
    assert tasks.worktree_changed_paths(c) == {"c.txt"}

    result = overlap_signal(store.list())
    assert result == {
        a.id: [{"with": b.short_id, "paths": ["README.md"]}],
        b.id: [{"with": a.short_id, "paths": ["README.md"]}],
    }
    assert c.id not in result


def test_overlap_signal_only_compares_tasks_on_the_same_project(home: Path, tmp_path: Path):
    from quorum.agents.manager import overlap_signal

    store = TaskStore(home)
    repos = {}
    for name in ("one", "two"):
        repo = make_repo(tmp_path, name)
        make_origin(tmp_path, repo, f"{name}.git")
        repos[name] = repo
    a = add_worktree(home, repos["one"], store, "one", "readme")
    b = add_worktree(home, repos["two"], store, "two", "readme")
    for t in (a, b):
        (Path(t.workdir) / "README.md").write_text("changed")
    # Same path, different repositories: nothing to say.
    assert overlap_signal(store.list()) == {}


def test_digest_marks_both_overlapping_tasks_and_skips_attached_ones(
    home: Path, tmp_path: Path, clock
):
    repo = make_repo(tmp_path, "ovproj")
    make_origin(tmp_path, repo)
    store = TaskStore(home)
    a = add_worktree(home, repo, store, "ovproj", "edit the readme")
    b = add_worktree(home, repo, store, "ovproj", "also edit the readme")
    c = add_worktree(home, repo, store, "ovproj", "disjoint")
    for t in (a, b):
        (Path(t.workdir) / "README.md").write_text(f"{t.short_id}'s version")
    (Path(c.workdir) / "c.txt").write_text("c")

    digest = build_digest(home, store.list(), clock(), directives=[])
    a_line = digest_line(digest, a.short_id, f"- [executing] {a.short_id}")
    b_line = digest_line(digest, b.short_id, f"- [executing] {b.short_id}")
    c_line = digest_line(digest, c.short_id, f"- [executing] {c.short_id}")
    assert f"overlaps={b.short_id} paths=1" in a_line
    assert f"overlaps={a.short_id} paths=1" in b_line
    assert "overlaps=" not in c_line
    assert f"  overlap: with {b.short_id} on README.md" in digest
    assert f"  overlap: with {a.short_id} on README.md" in digest

    # An adopted session's checkout is the human's: it is never compared,
    # even though its diff really does collide with a's.
    store.update(b.id, attached=True)
    digest = build_digest(home, store.list(), clock(), directives=[])
    assert "overlaps=" not in digest
    assert "overlap:" not in digest


def test_overlap_detail_names_at_most_three_paths(home: Path, tmp_path: Path, clock):
    repo = make_repo(tmp_path, "ovproj")
    make_origin(tmp_path, repo)
    store = TaskStore(home)
    a = add_worktree(home, repo, store, "ovproj", "wide")
    b = add_worktree(home, repo, store, "ovproj", "also wide")
    for t in (a, b):
        for name in ("d.txt", "a.txt", "c.txt", "b.txt", "e.txt"):
            (Path(t.workdir) / name).write_text("x")

    digest = build_digest(home, store.list(), clock(), directives=[])
    assert f"overlaps={b.short_id} paths=5" in digest
    assert f"  overlap: with {b.short_id} on a.txt, b.txt, c.txt (+2 more)" in digest


def test_overlap_falls_back_to_the_checkout_branch_without_a_remote(
    home: Path, tmp_path: Path
):
    """A `git init`ed project has no origin/HEAD; the branch the runner forked
    from is the checkout's, and that is the base."""
    from quorum.agents.manager import overlap_signal

    repo = make_repo(tmp_path, "local")
    store = TaskStore(home)
    a = add_worktree(home, repo, store, "local", "readme")
    b = add_worktree(home, repo, store, "local", "readme too")
    for t in (a, b):
        (Path(t.workdir) / "README.md").write_text("changed")
    # The checkout itself moving on must not count against the tasks.
    (repo / "later.txt").write_text("landed after the fork")
    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "later")

    assert tasks.worktree_changed_paths(a) == {"README.md"}
    assert set(overlap_signal(store.list())) == {a.id, b.id}


def test_overlap_ignores_the_checkouts_own_unpushed_commits(home: Path, tmp_path: Path):
    """The runner forks a worktree from the checkout's HEAD, not from
    origin/HEAD. A checkout one unpushed commit ahead of the remote would,
    measured against origin/HEAD, put that commit's paths in *every* live
    task's changed set — and two tasks on unrelated files would report an
    overlap on a file neither of them wrote."""
    from quorum.agents.manager import overlap_signal

    repo = make_repo(tmp_path, "ahead")
    make_origin(tmp_path, repo)
    # One commit the remote has never seen, touching a file no task will.
    (repo / "X.txt").write_text("landed locally, not pushed")
    repo_git(repo, "add", ".")
    repo_git(repo, "commit", "-qm", "unpushed")

    store = TaskStore(home)
    a = add_worktree(home, repo, store, "ahead", "only touch a")
    b = add_worktree(home, repo, store, "ahead", "only touch b")
    (Path(a.workdir) / "a.txt").write_text("a")
    (Path(b.workdir) / "b.txt").write_text("b")

    assert tasks.worktree_changed_paths(a) == {"a.txt"}
    assert tasks.worktree_changed_paths(b) == {"b.txt"}
    assert overlap_signal(store.list()) == {}


def test_overlap_is_unobservable_without_a_base_or_a_worktree(home: Path, tmp_path: Path):
    from quorum.agents.manager import overlap_signal

    store = TaskStore(home)
    queued = store.add(project="p", prompt="not started", harness="t")
    assert tasks.worktree_changed_paths(queued) is None  # no workdir yet

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    notgit = store.add(project="p", prompt="plain dir", harness="t")
    notgit = store.update(notgit.id, workdir=str(plain), status="executing")
    assert tasks.worktree_changed_paths(notgit) is None

    # A repository that is its own main worktree with no remote: no base.
    lone = make_repo(tmp_path, "lone")
    solo = store.add(project="p", prompt="in the checkout", harness="t")
    solo = store.update(solo.id, workdir=str(lone), status="executing")
    assert tasks.worktree_changed_paths(solo) is None

    # None of these can be compared, and none of them raises.
    assert overlap_signal(store.list()) == {}


def test_overlap_pairs_are_bounded(home: Path, tmp_path: Path, monkeypatch):
    from quorum.agents import manager as manager_mod

    repo = make_repo(tmp_path, "ovproj")
    make_origin(tmp_path, repo)
    store = TaskStore(home)
    made = [add_worktree(home, repo, store, "ovproj", f"t{i}") for i in range(3)]
    for t in made:
        (Path(t.workdir) / "README.md").write_text("changed")

    assert len(manager_mod.overlap_signal(store.list())) == 3
    monkeypatch.setattr(manager_mod, "OVERLAP_MAX_PAIRS", 1)
    # One pair's worth of budget: exactly two tasks are marked, the third
    # goes unobserved rather than costing more git calls.
    assert len(manager_mod.overlap_signal(store.list())) == 2
    monkeypatch.setattr(manager_mod, "OVERLAP_MAX_PAIRS", 0)
    assert manager_mod.overlap_signal(store.list()) == {}


def test_the_preamble_tells_a_task_to_rebase_before_pushing(home: Path):
    from quorum import prompts

    text = prompts.render(home, "task-preamble", task_id="abc123", project_path="/w")
    assert "git fetch origin" in text
    assert "rebase" in text
    assert "report blocked, naming the conflicting files" in text
    # A task that pushed in an earlier run cannot fast-forward after a
    # rebase; the way out is spelled, and it is leased.
    assert "git push --force-with-lease origin HEAD" in text
    assert "never a bare `--force`" in text
    assert "overlaps=" in prompts.load(home, "manager")


# -- priority and hold (#61) ----------------------------------------------


def test_digest_renders_priority_only_when_non_zero(home: Path, clock, project: str):
    """`priority=N` is an exception mark: an ordinary task's line is unchanged
    so the mark reads as one (#61)."""
    write_config(home, "manager_act")
    store = TaskStore(home)
    plain = store.add(project, "whenever", "tasktool")
    urgent = store.add(project, "first", "tasktool", priority=3)
    backlog = store.add(project, "later", "tasktool", priority=-2)

    digest = build_digest(home, store.list(), clock(), directives=[])
    lines = {t.short_id: next(
        line for line in digest.splitlines() if f"[queued] {t.short_id}" in line
    ) for t in (plain, urgent, backlog)}
    assert "priority=" not in lines[plain.short_id]
    assert "priority=3" in lines[urgent.short_id]
    assert "priority=-2" in lines[backlog.short_id]


def test_digest_marks_a_held_task_and_says_not_to_launch_it(home: Path, clock, project: str):
    write_config(home, "manager_act")
    store = TaskStore(home)
    task = store.add(project, "parked work", "tasktool", held=True)

    digest = build_digest(home, store.list(), clock(), directives=[])
    line = next(line for line in digest.splitlines() if f"[queued] {task.short_id}" in line)
    assert "held=true" in line
    assert "do not release it; only the user does" in digest

    # hold is not a status: the task line still shows what the harness said
    assert "[queued]" in line
    store.update(task.id, held=False)
    assert "held=true" not in build_digest(home, store.list(), clock(), directives=[])


def test_the_manager_prompt_carries_the_priority_and_hold_rules(home: Path):
    from quorum import prompts

    text = prompts.load(home, "manager")
    assert "priority=N" in text and "held=true" in text
    assert "Never launch a held task" in text
    assert "never `quorum task release` one" in text


def test_the_hold_rule_covers_the_relaunch_rules_too(home: Path):
    """"Never launch a held task" sits under the *queued* step, but a held
    task also reads as an ordinary relaunch (rule 4) and a perpetual one is
    told to relaunch forever (rule 12). A refused run is not journaled, so a
    manager that only read those two rules would retry every tick with no
    memory of having tried (#61)."""
    from quorum import prompts

    text = prompts.load(home, "manager")
    assert "not as a relaunch under" in text
    # rule 4: the stopped-without-finishing relaunch
    stopped = text.split("without finishing")[1].split("\n5.")[0]
    assert "held=true" in stopped
    # rule 12: the perpetual loop
    perpetual = text.split("relaunch it with `task run --detach` whenever its runner is dead")[1]
    assert "unless it" in perpetual.split(";")[0] and "held=true" in perpetual.split(";")[0]


def test_a_tick_keeps_the_digest_it_reasoned_over(home: Path, clock, project: str):
    """The one file #82 adds: without it, "why did it launch that" is
    unanswerable an hour later — the digest was rendered and dropped."""
    from quorum import transcript

    write_config(home, "manager_act")
    task = TaskStore(home).add(project, "tidy up the docs", "tasktool")

    make_manager(home, clock).tick()

    run_id = {e["run"] for e in fsio.read_jsonl(journal_path(home))}.pop()
    snapshot = run_snapshot_path(home, "manager", run_id).read_text()
    assert snapshot.startswith("# Situation digest")
    assert f"- [queued] {task.short_id}" in snapshot
    # the digest, not the whole prompt: the constitution above it is static
    assert "You are the manager" not in snapshot
    assert [p.name for p in runs_dir(home, "manager").glob("*.md")] == [f"{run_id}.md"]

    # and the four files read back as one tick
    out = "\n".join(transcript.render_run(home, "manager", run_id))
    assert f"=== manager run {run_id}" in out
    assert f"- [queued] {task.short_id}" in out              # what it saw
    assert f"ACT| task run {task.short_id}" in out           # what it said
    assert f"task.run -> {task.short_id}" in out             # what it did
    assert "ok · " in out                                     # how it ended
