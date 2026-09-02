"""On-demand cleanup: `task prune`, `board clear`, `task inbox --clear`.

The three of them share one rule — archive, never delete — so every test
here checks both halves: the thing left the live view, *and* it is still on
disk somewhere a human can get it back from.
"""

from __future__ import annotations

import gzip
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, prune, views
from quorum.cli import app
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore, inbox_name, runner_lock_path, worktree_path

runner = CliRunner()


def make_repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
            check=True, capture_output=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (repo / "README.md").write_text("hello")
    git("add", ".")
    git("commit", "-qm", "init")
    return repo


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(home: Path, tmp_path: Path) -> Path:
    path = make_repo(tmp_path)
    ProjectRegistry(home).add(path, name="proj")
    return path


def finished(home: Path, prompt: str = "a thing", status: str = "done", **kwargs):
    task = TaskStore(home).add("proj", prompt, "fake", **kwargs)
    return TaskStore(home).update(task.id, status=status)


def short_ids(home: Path) -> set[str]:
    return {row["id_short"] for row in views.task_rows(home)}


def archive_lines(home: Path) -> list[dict]:
    out = []
    for path in sorted((home / "messages" / "archive").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            out.extend(json.loads(line) for line in f if line.strip())
    return out


# -- selection -------------------------------------------------------------


def test_select_takes_terminal_statuses_and_skips_perpetual(home: Path):
    done = finished(home, "done one")
    running = finished(home, "still going", status="executing")
    forever = finished(home, "watch CI", status="done", perpetual=True)

    chosen = {t.id for t in prune.select(TaskStore(home).list())}
    assert done.id in chosen
    assert running.id not in chosen  # free-form statuses are never swept up
    assert forever.id not in chosen  # a perpetual task's "done" is an accident


def test_select_honours_older_than_over_updated_at(home: Path):
    old = finished(home, "ancient")
    fresh = finished(home, "just now")
    TaskStore(home).update(old.id, now=fsio.utc_now() - timedelta(days=30))

    chosen = {t.id for t in prune.select(TaskStore(home).list(), older_than=timedelta(days=7))}
    assert chosen == {old.id}
    assert fresh.id not in chosen


# -- task prune ------------------------------------------------------------


def test_prune_dry_run_changes_nothing(home: Path):
    task = finished(home)
    result = runner.invoke(app, ["task", "prune", "--dry-run", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert task.short_id in result.output
    assert "dry run" in result.output
    assert short_ids(home) == {task.short_id}
    assert not prune.archive_root(home).exists()


def test_prune_archives_the_task_and_views_forget_it(home: Path):
    task = finished(home)
    keep = finished(home, "still working", status="executing")

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output

    assert short_ids(home) == {keep.short_id}  # the dot-dir is skipped by every reader
    assert TaskStore(home).get(task.id) is None
    archived = prune.archived_task_dir(home, task.id)
    assert json.loads((archived / "task.json").read_text())["id"] == task.id
    assert prune.archived_ids(home) == [task.id]


def test_prune_is_reversible_by_moving_the_directory_back(home: Path):
    task = finished(home)
    runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])

    prune.archived_task_dir(home, task.id).rename(home / "tasks" / task.id)
    assert short_ids(home) == {task.short_id}
    assert TaskStore(home).get(task.id).status == "done"


def test_prune_refuses_a_task_with_a_live_runner(home: Path):
    task = finished(home)
    # pid 1 is always alive and is never this process (same-process locks are
    # treated as stale takeovers, which would defeat the test)
    fsio.atomic_write_json(runner_lock_path(home, task.id), {"pid": 1})

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "holds its lock" in result.output
    assert short_ids(home) == {task.short_id}


def test_prune_refuses_an_attached_task(home: Path):
    task = finished(home, attached=True)

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "attached" in result.output
    assert short_ids(home) == {task.short_id}


def test_prune_refuses_a_task_another_one_still_depends_on(home: Path):
    upstream = finished(home, "the base")
    downstream = TaskStore(home).add("proj", "builds on it", "fake", depends_on=[upstream.id])

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert f"{downstream.short_id} still depends on it" in result.output
    assert upstream.short_id in short_ids(home)


def test_prune_archives_a_dependency_when_its_dependent_goes_too(home: Path):
    upstream = finished(home, "the base")
    downstream = TaskStore(home).add("proj", "builds on it", "fake", depends_on=[upstream.id])
    TaskStore(home).update(downstream.id, status="done")

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert short_ids(home) == set()
    assert sorted(prune.archived_ids(home)) == sorted([upstream.id, downstream.id])


def test_prune_refuses_stranded_work_unless_forced(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    (workdir / "scratch.txt").write_text("uncommitted")

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "stranded work" in result.output
    assert short_ids(home) == {task.short_id}

    forced = runner.invoke(app, ["task", "prune", "--force", "--yes", "--home", str(home)])
    assert forced.exit_code == 0, forced.output
    assert short_ids(home) == set()


def test_prune_worktrees_removes_the_worktree_and_the_merged_branch(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    branch = f"quorum/{task.short_id}"
    assert workdir.is_dir()
    assert branch in git_out(repo, "branch", "--list", branch)

    result = runner.invoke(
        app, ["task", "prune", "--worktrees", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert not worktree_path(home, task.id).exists()
    # the branch never moved off the base commit, so git agrees it is merged
    assert git_out(repo, "branch", "--list", branch) == ""
    assert prune.archived_ids(home) == [task.id]


def test_prune_worktrees_force_deletes_an_unmerged_branch(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    (workdir / "work.txt").write_text("real work")
    subprocess.run(
        ["git", "-C", str(workdir), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-qm", "work"], check=True, capture_output=True
    )
    branch = f"quorum/{task.short_id}"

    result = runner.invoke(
        app, ["task", "prune", "--worktrees", "--force", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    # --force is needed at all only because the commit is unpushed; it also
    # upgrades `git branch -d` to -D, which is the point of asking for it
    assert git_out(repo, "branch", "--list", branch) == ""
    assert not worktree_path(home, task.id).exists()


def test_prune_worktrees_force_never_destroys_an_uncommitted_file(home: Path, repo: Path):
    """--force waives the stranded-work refusal and forces the *branch* delete;
    it is never passed to `git worktree remove`, so files nobody committed
    survive a prune and their task stays unarchived to say so."""
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    (workdir / "work.txt").write_text("real work")
    subprocess.run(["git", "-C", str(workdir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-qm", "work"], check=True, capture_output=True
    )
    (workdir / "scratch.txt").write_text("never committed")

    result = runner.invoke(
        app, ["task", "prune", "--worktrees", "--force", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "worktree kept, task not archived" in result.output
    assert (workdir / "scratch.txt").read_text() == "never committed"
    assert "work" in git_out(repo, "log", "--all", "--oneline")
    assert short_ids(home) == {task.short_id}
    assert not prune.archive_root(home).exists()


def test_prune_worktrees_dry_run_names_the_worktree_and_the_branch(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)

    result = runner.invoke(
        app, ["task", "prune", "--worktrees", "--dry-run", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert f"would remove worktree {workdir}" in result.output
    assert f"would delete branch quorum/{task.short_id}" in result.output
    assert "git branch -d" in result.output  # unforced: git still gets the veto
    assert workdir.is_dir()
    assert short_ids(home) == {task.short_id}

    forced = runner.invoke(
        app, ["task", "prune", "--worktrees", "--force", "--dry-run", "--home", str(home)]
    )
    assert "git branch -D" in forced.output


def test_prune_worktrees_dry_run_says_a_dirty_worktree_would_stay(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    (workdir / "scratch.txt").write_text("uncommitted")

    result = runner.invoke(
        app,
        ["task", "prune", "--worktrees", "--force", "--dry-run", "--home", str(home)],
    )
    assert result.exit_code == 0, result.output
    assert f"would keep worktree {workdir}" in result.output
    assert "unarchived" in result.output


def test_prune_rechecks_the_runner_lock_after_planning(home: Path, monkeypatch: pytest.MonkeyPatch):
    """plan() runs before an interactive confirm — a runner can take the lock
    in between, and archiving would move the directory out from under it."""
    task = finished(home)
    real_plan = prune.plan

    def a_runner_appears(*args, **kwargs):
        candidates = real_plan(*args, **kwargs)
        # pid 1 is always alive and is never this process (see above)
        fsio.atomic_write_json(runner_lock_path(home, task.id), {"pid": 1})
        return candidates

    monkeypatch.setattr("quorum.cli.prune_mod.plan", a_runner_appears)

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "holds its lock" in result.output
    assert short_ids(home) == {task.short_id}
    assert not prune.archive_root(home).exists()


def test_prune_keeps_an_upstream_whose_dependent_was_skipped(home: Path, repo: Path):
    """The same-batch dependency exemption only holds while the dependent is
    actually going: if its worktree will not go, the upstream stays too
    rather than leaving a dangling `depends_on`."""
    from quorum import runner as runner_mod

    store = TaskStore(home)
    upstream = finished(home, "the base")
    downstream = store.add("proj", "builds on it", "fake", depends_on=[upstream.id])
    store.update(downstream.id, status="done")
    workdir = runner_mod.prepare_workdir(home, store.get(downstream.id), store)
    (workdir / "scratch.txt").write_text("uncommitted")  # git will refuse to remove it

    result = runner.invoke(
        app, ["task", "prune", "--worktrees", "--force", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "worktree kept, task not archived" in result.output
    assert f"{downstream.short_id} still depends on it" in result.output
    assert short_ids(home) == {upstream.short_id, downstream.short_id}
    assert not prune.archive_root(home).exists()


def test_dependents_first_orders_a_dependent_before_its_upstream(home: Path):
    store = TaskStore(home)
    upstream = finished(home, "the base")
    downstream = store.add("proj", "builds on it", "fake", depends_on=[upstream.id])

    ordered = prune.dependents_first([store.get(upstream.id), store.get(downstream.id)])
    assert [t.id for t in ordered] == [downstream.id, upstream.id]


def test_remove_task_worktree_keeps_an_unmerged_branch_without_force(home: Path, repo: Path):
    from quorum import runner as runner_mod

    task = finished(home)
    store = TaskStore(home)
    workdir = runner_mod.prepare_workdir(home, store.get(task.id), store)
    (workdir / "work.txt").write_text("real work")
    subprocess.run(["git", "-C", str(workdir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-qm", "work"], check=True, capture_output=True
    )

    removed, notes = prune.remove_task_worktree(home, store.get(task.id))
    assert removed
    assert not worktree_path(home, task.id).exists()
    assert any("kept branch" in n for n in notes)
    assert git_out(repo, "branch", "--list", f"quorum/{task.short_id}") != ""


def test_prune_older_than_leaves_a_fresh_task_alone(home: Path):
    old = finished(home, "ancient")
    fresh = finished(home, "just finished")
    TaskStore(home).update(old.id, now=fsio.utc_now() - timedelta(days=30))

    result = runner.invoke(
        app, ["task", "prune", "--older-than", "7d", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert short_ids(home) == {fresh.short_id}


def test_prune_with_nothing_to_do_says_so(home: Path):
    finished(home, "in flight", status="executing")
    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "nothing to prune" in result.output


def test_prune_journals_through_the_actor_guard(home: Path, monkeypatch: pytest.MonkeyPatch):
    task = finished(home)
    monkeypatch.setenv("QUORUM_ACTOR", "manager")
    monkeypatch.setenv("QUORUM_ACTOR_RUN", "run-1")

    result = runner.invoke(app, ["task", "prune", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output

    entries = fsio.read_jsonl(home / "state" / "manager" / "journal.jsonl")
    assert [e["action"] for e in entries] == ["task.prune"]
    assert task.short_id in entries[0]["args"]


# -- board clear -----------------------------------------------------------


def test_board_clear_empties_the_attention_banner_and_keeps_the_history(home: Path):
    bus = MessageBus(home)
    bus.post("manager", "attention", "escalation", text="a human is needed")
    assert views.attention_summary(home)["count"] == 1

    result = runner.invoke(app, ["board", "clear", "attention", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert views.attention_summary(home)["count"] == 0
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["a human is needed"]


def test_board_clear_dry_run_changes_nothing(home: Path):
    MessageBus(home).post("manager", "attention", "escalation", text="still here")
    result = runner.invoke(
        app, ["board", "clear", "attention", "--dry-run", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "would archive 1" in result.output
    assert views.attention_summary(home)["count"] == 1
    assert archive_lines(home) == []


def test_board_clear_before_keeps_newer_messages(home: Path):
    old = MessageBus(home, now=lambda: fsio.utc_now() - timedelta(days=30))
    old.post("manager", "attention", "escalation", text="ancient")
    MessageBus(home).post("manager", "attention", "escalation", text="recent")

    result = runner.invoke(
        app, ["board", "clear", "attention", "--before", "7d", "--yes", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    live = [m.payload["text"] for m in MessageBus(home).read_topic("attention")]
    assert live == ["recent"]
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["ancient"]


def test_board_clear_on_an_empty_topic_says_so(home: Path):
    result = runner.invoke(app, ["board", "clear", "nothing", "--yes", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "nothing to clear" in result.output


def test_board_clear_rejects_an_unparseable_cutoff(home: Path):
    MessageBus(home).post("manager", "attention", "escalation", text="here")
    result = runner.invoke(
        app, ["board", "clear", "attention", "--before", "soon", "--yes", "--home", str(home)]
    )
    assert result.exit_code != 0
    assert views.attention_summary(home)["count"] == 1


# -- task inbox --clear ----------------------------------------------------


def test_task_inbox_clear_archives_pending_guidance(home: Path):
    task = finished(home, status="executing")
    MessageBus(home).send("user", inbox_name(task.id), text="never mind")

    result = runner.invoke(
        app, ["task", "inbox", task.short_id, "--clear", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "archived 1" in result.output

    peek = runner.invoke(app, ["task", "inbox", task.short_id, "--home", str(home)])
    assert "no guidance waiting" in peek.output
    assert [m["payload"]["text"] for m in archive_lines(home)] == ["never mind"]


def test_task_inbox_clear_leaves_a_claimed_message_alone(home: Path):
    task = finished(home, status="executing")
    bus = MessageBus(home)
    bus.send("user", inbox_name(task.id), text="being worked on")
    claimed = next(bus.claim(inbox_name(task.id)))  # sits in cur/, someone owns it

    result = runner.invoke(
        app, ["task", "inbox", task.short_id, "--clear", "--home", str(home)]
    )
    assert result.exit_code == 0, result.output
    assert "no guidance waiting" in result.output
    assert claimed.path.exists()


def test_task_inbox_clear_and_claim_are_mutually_exclusive(home: Path):
    task = finished(home, status="executing")
    MessageBus(home).send("user", inbox_name(task.id), text="keep me")

    result = runner.invoke(
        app, ["task", "inbox", task.short_id, "--claim", "--clear", "--home", str(home)]
    )
    assert result.exit_code == 1
    assert MessageBus(home).pending(inbox_name(task.id))
