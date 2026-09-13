"""`views.task_history` and `quorum task history`: one chronological list of
a task's life, read back out of the files that already record it — task.json,
reports.jsonl, the inbox and message archive, the agents' journals, the
archive directory. Nothing is recorded for the list's sake, so every test
here builds the files the way the substrate does and reads them back."""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from conftest import harness_config, make_repo
from quorum import fsio, prune, tasks, views
from quorum.actor import journal_path
from quorum.cli import app
from quorum.config import load_config
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.runner import run_task
from quorum.tasks import TaskRun, TaskStore, inbox_name

runner = CliRunner()


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def kinds(rows: list[dict]) -> list[str]:
    return [r["kind"] for r in rows]


def build_life(home: Path) -> tasks.Task:
    """A task that has had everything happen to it, each event stamped with
    its own hour so the expected order is unambiguous."""
    store = TaskStore(home)
    task = store.add(
        "proj", "do the thing", "fake",
        issue_url="https://github.com/o/r/issues/95", now=at(1),
    )
    # 02:00 the manager nudged it (journaled, target = short id)
    fsio.append_jsonl(
        journal_path(home),
        {"at": fsio.iso(at(2)), "run": "R1", "actor": "manager", "action": "task.nudge",
         "target": task.short_id, "target_status": "queued", "args": "start with the tests"},
    )
    # ...and the nudge itself was sent at 02:00, claimed and acked by a run
    bus = MessageBus(home, now=lambda: at(2))
    bus.send("manager", inbox_name(task.id), type="guidance", text="start with the tests")
    for claimed in bus.claim(inbox_name(task.id)):
        claimed.ack()
    # 03:00 the run started, reported at 04:00, ended at 05:00 with usage
    tasks.report(home, task.id, status="executing", text="reading the code", now=at(4))
    run1 = TaskRun(
        started_at=fsio.iso(at(3)), ended_at=fsio.iso(at(5)), exit_code=0,
        usage={"cost_usd": 0.42, "total_tokens": 11000},
    )
    # 06:00 a second run, stopped by `task stop` at 07:00 (SIGTERM = 15)
    run2 = TaskRun(started_at=fsio.iso(at(6)), ended_at=fsio.iso(at(7)), exit_code=-15,
                   stopped=True)
    # 08:00 a fresh-session relaunch that stalled and was auto-committed at 09:00
    run3 = TaskRun(
        started_at=fsio.iso(at(8)), ended_at=fsio.iso(at(9)), exit_code=143,
        stalled=True, fresh_session=True, auto_commit="auto-committed 2 path(s) as abc123",
    )
    store.update(task.id, runs=[r.model_dump() for r in (run1, run2, run3)])
    # 10:00 the user sent guidance that is still waiting; 10:30 one a run
    # claimed but has not acked yet
    MessageBus(home, now=lambda: at(10)).send("user", inbox_name(task.id), text="try harder")
    later = MessageBus(home, now=lambda: at(10, 30))
    later.send("user@tui", inbox_name(task.id), text="mid-flight")
    claimed = list(later.claim(inbox_name(task.id)))
    assert len(claimed) == 2
    claimed[0].reject()  # "try harder" back to new/; "mid-flight" stays in cur/
    # 11:00 reported done with a PR; 12:00 the manager's probe saw it merged
    tasks.report(home, task.id, status="done", text="shipped",
                 pr_url="https://github.com/o/r/pull/7", now=at(11))
    tasks.record_pr_state(home, store.get(task.id), "merged", now=at(12))
    return store.get(task.id)


def test_every_kind_of_entry_appears_once_and_in_order(home: Path):
    task = build_life(home)
    rows = views.task_history(home, task)

    assert kinds(rows) == [
        "queued",
        "action",        # 02:00 manager: task.nudge
        "guidance",      # 02:00 delivered
        "run.started",   # 03:00 run 1
        "report",        # 04:00 executing
        "run.ended",     # 05:00 run 1
        "run.started",   # 06:00 run 2
        "run.ended",     # 07:00 stopped
        "run.started",   # 08:00 run 3, fresh
        "run.ended",     # 09:00 stalled + auto-commit
        "guidance",      # 10:00 waiting
        "guidance",      # 10:30 claimed
        "report",        # 11:00 done
        "pr_state",      # 12:00 merged
    ]
    assert [r["at"] for r in rows] == sorted(r["at"] for r in rows)
    text = [r["text"] for r in rows]
    assert text[0] == "queued on proj · harness fake · from #95"
    assert text[1] == "manager: task.nudge — start with the tests (status then queued)"
    assert text[2] == "guidance from manager: start with the tests"
    assert text[3] == "run 1 started"
    assert text[4] == "reported executing: reading the code"
    assert text[5] == "run 1 ended · exit 0 · $0.42 · 11.0k tok"
    assert text[7] == "run 2 ended · stopped by `task stop` (SIGTERM)"
    assert text[8] == "run 3 started · fresh session"
    assert text[9] == (
        "run 3 ended · exit 143 · stalled (no harness output) · "
        "auto-committed 2 path(s) as abc123"
    )
    assert text[10] == "guidance from user (waiting): try harder"
    assert text[11] == "guidance from user@tui (claimed): mid-flight"
    assert text[12] == "reported done: shipped · https://github.com/o/r/pull/7"
    assert text[13] == "pr state observed: merged · https://github.com/o/r/pull/7"
    # the raw fields ride along for scripts
    assert rows[5]["usage"] == {"cost_usd": 0.42, "total_tokens": 11000}
    assert rows[7]["stopped"] is True and rows[7]["exit_code"] == -15
    assert rows[9]["stalled"] is True and rows[9]["fresh_session"] is True
    assert {r["state"] for r in rows if r["kind"] == "guidance"} == {
        "delivered", "waiting", "claimed"
    }
    assert rows[13]["state"] == "merged"


def test_history_over_a_real_fake_harness_run(home: Path, tmp_path: Path, monkeypatch):
    """The fake harness home end to end: a nudge sent before the run is
    delivered by it, the harness reports through the CLI, and the run record
    closes the list."""
    repo = make_repo(tmp_path)
    ProjectRegistry(home).add(repo, name="proj")
    harness_config(home, resume=False)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "report")
    monkeypatch.setenv("FAKE_HARNESS_PR_URL", "https://example.com/pr/9")
    task = TaskStore(home).add("proj", "x", "fake")
    tasks.nudge(home, task, "mind the tests", sender="user")

    assert run_task(home, load_config(home), task.id) == 0

    rows = views.task_history(home, TaskStore(home).get(task.id))
    assert kinds(rows) == ["queued", "guidance", "run.started", "report", "run.ended"]
    assert rows[1]["state"] == "delivered" and rows[1]["from"] == "user"
    assert rows[3]["status"] == "done" and rows[3]["pr_url"] == "https://example.com/pr/9"
    assert rows[4]["exit_code"] == 0 and rows[4]["run"] == 1
    # delivered guidance left the inbox: only the archive still has it
    assert MessageBus(home).inbox_messages(inbox_name(task.id), "cur") == []
    assert MessageBus(home).inbox_messages(inbox_name(task.id), "new") == []


def test_a_live_run_shows_as_started_off_its_lock(home: Path):
    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    # a runner "alive" at pid 1, the way every live-runner test says so
    fsio.atomic_write_json(
        tasks.runner_lock_path(home, task.id),
        {"pid": 1, "started_at": fsio.iso(at(2)), "role": "task-runner"},
    )
    rows = views.task_history(home, task)
    assert kinds(rows) == ["queued", "run.started"]
    assert rows[1]["text"] == "run 1 started · still running" and rows[1]["live"] is True

    # a lock whose process is gone is no run at all
    fsio.atomic_write_json(
        tasks.runner_lock_path(home, task.id),
        {"pid": 2**22 - 1, "started_at": fsio.iso(at(2))},
    )
    assert kinds(views.task_history(home, task)) == ["queued"]


def test_other_agents_journals_count_and_unrelated_entries_do_not(home: Path):
    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    other = TaskStore(home).add("proj", "y", "fake", now=at(1))
    fsio.append_jsonl(
        journal_path(home, "babysitter"),
        {"at": fsio.iso(at(2)), "run": "B1", "actor": "babysitter", "action": "task.nudge",
         "target": task.short_id, "target_status": "queued", "args": "CI is red"},
    )
    fsio.append_jsonl(
        journal_path(home),
        {"at": fsio.iso(at(3)), "run": "R1", "actor": "manager", "action": "task.nudge",
         "target": other.short_id, "target_status": "queued"},
    )
    fsio.append_jsonl(
        journal_path(home),
        {"at": fsio.iso(at(4)), "run": "R1", "actor": "manager", "action": "note",
         "args": "thinking"},
    )
    # a prune is journaled once per command, naming its tasks in args
    fsio.append_jsonl(
        journal_path(home),
        {"at": fsio.iso(at(5)), "run": "R2", "actor": "manager", "action": "task.prune",
         "args": f"2 task(s): {task.short_id}, {other.short_id} +worktrees"},
    )
    rows = views.task_history(home, task)
    assert [(r["kind"], r.get("actor"), r.get("action")) for r in rows] == [
        ("queued", None, None),
        ("action", "babysitter", "task.nudge"),
        ("action", "manager", "task.prune"),
    ]
    assert rows[1]["text"] == "babysitter: task.nudge — CI is red (status then queued)"


def test_history_is_fail_soft_over_bad_files(home: Path):
    """A torn report line, an unreadable archive file and a journal line that
    is not an object cost the rows they held — never the list."""
    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    with open(tasks.reports_path(home, task.id), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": fsio.iso(at(2)), "status": "executing", "text": "ok"}) + "\n")
        f.write('{"at": "2026-01-01T03:00:00Z", "status": "torn')
    with open(journal_path(home), "a", encoding="utf-8") as f:
        f.write('"just a string"\n')
        f.write("[1, 2]\n")
    archive = home / "messages" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "2026-01.jsonl.gz").write_bytes(b"this is not gzip")
    rows = views.task_history(home, task)
    assert kinds(rows) == ["queued", "report"]
    assert MessageBus(home).archived_direct(inbox_name(task.id)) == []


def test_history_survives_a_corrupt_deflate_stream_in_the_archive(home: Path):
    """gzip reports damage three ways and only two of them are OSErrors:
    corruption inside the compressed data raises zlib.error, which used to
    escape the archive scan and take `task history` (and the TUI tab and the
    web task detail) down with it."""
    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    archive = home / "messages" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    good = gzip.compress(json.dumps({"to": inbox_name(task.id)}).encode() + b"\n")
    # A valid gzip header (the first ten bytes) over random deflate data.
    (archive / "2026-01.jsonl.gz").write_bytes(good[:10] + os.urandom(200))

    assert MessageBus(home).archived_records(inbox_name(task.id)) == []
    assert kinds(views.task_history(home, task)) == ["queued"]
    r = runner.invoke(app, ["task", "history", task.short_id])
    assert r.exit_code == 0, r.output


def test_history_survives_an_archive_that_inflates_to_non_utf8(home: Path):
    """The fourth way archive damage shows up: bytes that decompress fine and
    are not text. That comes back off the text wrapper as a
    UnicodeDecodeError rather than a gzip or zlib failure, and it is the
    shape the random-deflate test above hits a fraction of the time."""
    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    archive = home / "messages" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "2026-01.jsonl.gz").write_bytes(gzip.compress(b"\xff\xfe not utf-8\n"))

    assert MessageBus(home).archived_records(inbox_name(task.id)) == []
    assert kinds(views.task_history(home, task)) == ["queued"]
    r = runner.invoke(app, ["task", "history", task.short_id])
    assert r.exit_code == 0, r.output


def test_cli_prints_the_life_and_emits_json(home: Path):
    task = build_life(home)
    r = runner.invoke(app, ["task", "history", task.short_id])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith(f"task {task.short_id}  ({task.id})  14 event(s)")
    assert lines[1] == "[2026-01-01 01:00:00] queued on proj · harness fake · from #95"
    assert "[2026-01-01 07:00:00] run 2 ended · stopped by `task stop` (SIGTERM)" in lines
    assert lines[-1] == (
        "[2026-01-01 12:00:00] pr state observed: merged · https://github.com/o/r/pull/7"
    )

    r = runner.invoke(app, ["task", "history", task.short_id, "--json"])
    rows = json.loads(r.output)
    assert kinds(rows) == kinds(views.task_history(home, task))
    assert rows[0]["issue_url"] == "https://github.com/o/r/issues/95"

    r = runner.invoke(app, ["task", "history", "zzzzzz"])
    assert r.exit_code == 1 and "no task matching" in r.output


def test_cli_still_answers_for_a_pruned_task(home: Path):
    """Archival is the last thing that happens to a task, so the history must
    survive the move into tasks/.archive — resolved there when the live
    listing has nothing, and closed by an `archived` row."""
    task = build_life(home)
    prune.archive_task(home, task.id)
    assert TaskStore(home).get(task.id) is None

    r = runner.invoke(app, ["task", "history", task.short_id, "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert kinds(rows)[-1] == "archived" and kinds(rows)[0] == "queued"
    assert len(rows) == 15  # everything the live task had, plus the move
    assert rows[-1]["at"] > rows[-2]["at"]  # stamped now, off the directory
    assert "task prune" in rows[-1]["text"]

    # the same grammar as the live resolver: a prefix works, an ambiguous one is refused
    twin = TaskStore(home).add("proj", "twin", "fake", now=at(1))
    prune.archive_task(home, twin.id)
    shared = task.id[:2]
    r = runner.invoke(app, ["task", "history", shared])
    assert r.exit_code == 1 and "ambiguous" in r.output
    r = runner.invoke(app, ["task", "history", twin.id])
    assert r.exit_code == 0 and "queued on proj" in r.output


def test_an_unparseable_stamp_sorts_last_and_says_so(home: Path):
    """A row quorum cannot place in time is still shown, but it goes after
    every real row and its line is marked `?`. Ordering by raw string
    comparison would file an empty stamp as the first thing that ever
    happened to the task and `not-a-date` as the last, both silently."""
    task = TaskStore(home).add("proj", "x", "fake", now=at(2))
    with open(tasks.reports_path(home, task.id), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": fsio.iso(at(3)), "status": "executing", "text": "real"}) + "\n")
        f.write(json.dumps({"status": "planning", "text": "no at key"}) + "\n")
        f.write(json.dumps({"at": "", "status": "planning", "text": "empty at"}) + "\n")
        f.write(json.dumps({"at": "not-a-date", "status": "planning", "text": "junk at"}) + "\n")
    rows = views.task_history(home, task)
    assert [r["text"] for r in rows][:2] == [
        f"queued on {task.project} · harness fake",
        "reported executing: real",
    ]
    assert {r["text"] for r in rows[2:]} == {
        "reported planning: no at key",
        "reported planning: empty at",
        "reported planning: junk at",
    }
    marked = [views.history_line(r) for r in rows[2:]]
    assert all(line.startswith("[?") for line in marked)
    assert "[? not-a-date]" in " ".join(marked)
    assert views.history_line(rows[0]).startswith("[2026-01-01 02:00:00]")
    # the raw stamp survives for --json; only the rendered half is marked
    assert [r["at"] for r in rows[2:]].count("") == 2
    assert json.dumps(rows)


def test_guidance_in_two_places_at_once_is_one_row(home: Path):
    """`ack()` appends to the archive before it unlinks the `cur/` copy, so a
    consumer that dies between the two leaves the message in both — and the
    janitor then returns the orphaned claim to `new/`, where nothing ever
    re-archives it. The history must list that nudge once, in the furthest
    state it reached, not once per place a copy of it is lying in."""
    from quorum.messages import _archive_one

    task = TaskStore(home).add("proj", "x", "fake", now=at(1))
    bus = MessageBus(home)
    tasks.nudge(home, task, "steer left", sender="user")
    claimed = next(bus.claim(inbox_name(task.id)))
    assert claimed.message.payload["text"] == "steer left"
    # ack() halfway through: archived, but the cur/ copy still on disk, which
    # is what a crash between its two lines leaves behind
    _archive_one(bus.archive_dir, claimed.message.dump())
    assert claimed.path.exists()
    rows = [r for r in views.task_history(home, task) if r["kind"] == "guidance"]
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"
    assert rows[0]["text"] == "guidance from user: steer left"
    # and still one row once the janitor has put the orphaned claim back
    claimed.reject()
    rows = [r for r in views.task_history(home, task) if r["kind"] == "guidance"]
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"
