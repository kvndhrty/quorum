"""Manager agent tests: the harness-driven supervision loop.

The manager's harness is the fake in tests/bin/fake_harness.py running in a
manager mode; the tasks it acts on use the same fake in echo mode. Each
[harness.<name>] table pins its own FAKE_HARNESS_MODE via `env`, which is
exactly how the mechanism separates concerns in production too.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from quorum import fsio, notes, runner, tasks
from quorum.actor import notes_path, usage_path
from quorum.agent import AgentContext
from quorum.agents.manager import (
    LOOP_WINDOW_CALLS,
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
    manager_usage: str = "",
    manager_note: str = "",
) -> None:
    (home / "config.toml").write_text(
        "[tasks]\n"
        'default_harness = "tasktool"\n'
        "[harness.tasktool]\n"
        f'start = ["{sys.executable}", "{FAKE}"]\n'
        'env = { FAKE_HARNESS_MODE = "echo" }\n'
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
    assert len(entries) == 2  # the cap, exactly
    assert "REFUSED|" in manager_transcript_text(home)


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
    observation — quorum still never stops anything."""
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
    assert "BUDGET-EXCEEDED: run 2: cost $2.00 > max_cost_per_run $1.00" in digest
    assert "an observation, not a rail" in digest
    assert "run 1:" not in digest  # the cheap run is not indicted


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
