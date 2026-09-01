"""On-demand cleanup: archive finished tasks (and, optionally, their worktrees).

Nothing here deletes a task. Pruning *moves* `tasks/<id>/` to
`tasks/.archive/<id>/` — the same "archive, never delete" pattern the message
bus uses, and reversible with one `mv`. The archive directory is dot-prefixed
on purpose: `TaskStore.list` (and therefore `quorum status`, `task list`, the
TUI, the web dashboard, the manager digest — every reader in the codebase)
already skips dot-entries, so an archived task leaves every view with no code
change anywhere else.

The work splits into three total, side-effect-free readers and two doers, so
that later features can reuse the selection without re-deriving it (#57 wants
`prune` to default to *merged* tasks):

    select()      which tasks match the status/age filters       (pure)
    refusal()     why one selected task must not be archived     (reads files)
    plan()        the two above over a home, as one ordered list (reads files)
    remove_task_worktree()   git worktree remove + branch delete (mutates git)
    archive_task()           the move itself                     (mutates disk)

The refusals are substrate rails of the same class as the runner's: a live
runner would keep writing into a directory that moved out from under it, an
attached task's workdir is the user's own checkout, a task something else
still depends on would leave a dangling `depends_on`, and uncommitted or
unpushed work in a worktree is exactly the stranded work the rest of quorum
works to keep visible. `--force` overrides only the last one — the first
three are conditions the user can resolve directly (wait, detach, prune the
dependent too).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import fsio
from .projects import ProjectRegistry
from .tasks import (
    TERMINAL_STATUSES,
    Task,
    TaskStore,
    runner_alive,
    task_dir,
    tasks_dir,
    workdir_git_state,
    worktree_path,
)

# Dot-prefixed so every existing directory scan skips it (fsio.is_tmp).
ARCHIVE_DIRNAME = ".archive"

# What `task prune` considers by default: the statuses that end quorum's
# attention. Free-form statuses are never swept up by accident.
DEFAULT_STATUSES = tuple(sorted(TERMINAL_STATUSES))


def archive_root(home: Path) -> Path:
    return tasks_dir(home) / ARCHIVE_DIRNAME


def archived_task_dir(home: Path, task_id: str) -> Path:
    return archive_root(home) / task_id


def archived_ids(home: Path) -> list[str]:
    """Ids sitting in the archive, chronological (ULID names sort that way)."""
    root = archive_root(home)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not fsio.is_tmp(p.name))


@dataclass(frozen=True)
class PruneCandidate:
    """A selected task plus the reason it cannot be archived, if any."""

    task: Task
    refusal: str | None = None

    @property
    def prunable(self) -> bool:
        return self.refusal is None


def select(
    tasks: Iterable[Task],
    statuses: Iterable[str] = DEFAULT_STATUSES,
    older_than: timedelta | None = None,
    now: datetime | None = None,
) -> list[Task]:
    """The tasks matching the prune filters — pure over an already-loaded list.

    A perpetual task is never selected: it is not meant to finish, so a
    terminal status on one is an accident of reporting, not a life ended.
    Age is measured from `updated_at` (the last report or run), which is what
    "finished a week ago" means to a reader.
    """
    wanted = {s.strip().lower() for s in statuses if s.strip()}
    floor = (now or fsio.utc_now()) - older_than if older_than is not None else None
    out = []
    for task in tasks:
        if task.perpetual or task.status.lower() not in wanted:
            continue
        if floor is not None:
            try:
                updated = fsio.parse_iso(task.updated_at)
            except ValueError:
                continue  # unparseable timestamp: too old to judge, so not swept
            if updated > floor:
                continue
        out.append(task)
    return out


def refusal(
    home: Path,
    task: Task,
    by_id: Mapping[str, Task],
    selected: Iterable[str] = (),
    force: bool = False,
) -> str | None:
    """Why this task must not be archived, or None when it may be.

    `selected` is the rest of the batch: a dependent that is itself being
    pruned in the same pass is not a reason to keep this one.
    """
    if task.attached:
        return "attached to a live session — `quorum task detach` first"
    if runner_alive(home, task.id):
        return "a runner holds its lock — wait, or `quorum task cancel --kill`"
    batch = set(selected)
    dependents = sorted(
        other.short_id
        for other in by_id.values()
        if other.id != task.id and task.id in other.depends_on and other.id not in batch
    )
    if dependents:
        return f"{', '.join(dependents)} still depends on it"
    if not force:
        state = workdir_git_state(task)
        if state is not None:
            stranded = []
            if state["dirty"]:
                stranded.append(f"{state['dirty']} uncommitted")
            if state["unpushed"]:
                stranded.append(f"{state['unpushed']} unpushed")
            if stranded:
                return (
                    f"stranded work in its workdir ({', '.join(stranded)}) — "
                    "commit and push, or --force"
                )
    return None


def plan(
    home: Path,
    statuses: Iterable[str] = DEFAULT_STATUSES,
    older_than: timedelta | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> list[PruneCandidate]:
    """What a prune would do: every selected task, with its refusal or None."""
    all_tasks = TaskStore(home).list()
    by_id = {t.id: t for t in all_tasks}
    chosen = select(all_tasks, statuses=statuses, older_than=older_than, now=now)
    ids = {t.id for t in chosen}
    return [
        PruneCandidate(task=t, refusal=refusal(home, t, by_id, selected=ids, force=force))
        for t in chosen
    ]


def remove_task_worktree(home: Path, task: Task, force: bool = False) -> tuple[bool, list[str]]:
    """`git worktree remove` the task's worktree and drop its branch.

    Returns (removed, notes). The branch is only deleted when git agrees it
    is merged (`git branch -d`) unless `force` upgrades that to `-D`: a
    worktree can be recreated from a branch, but an unmerged branch that is
    gone is gone. A worktree git refuses to remove (uncommitted or untracked
    files) reports False, and the caller leaves the task alone rather than
    archiving a record whose stranded work nothing would surface again.
    """
    notes: list[str] = []
    path = worktree_path(home, task.id)
    branch = f"quorum/{task.short_id}"
    project = ProjectRegistry(home).get(task.project)
    if project is None or not project.dir.is_dir():
        if not path.exists():
            return True, ["worktree already gone"]
        return False, [f"project {task.project!r} is not registered — cannot run git"]
    if path.exists():
        args = ["worktree", "remove", str(path)] + (["--force"] if force else [])
        result = _git(project.dir, *args)
        if result.returncode != 0:
            return False, [f"git worktree remove failed: {_trim(result.stderr)}"]
        notes.append(f"removed worktree {path}")
    else:
        _git(project.dir, "worktree", "prune")
        notes.append("worktree already gone")
    if _branch_exists(project.dir, branch):
        drop = _git(project.dir, "branch", "-D" if force else "-d", branch)
        if drop.returncode == 0:
            notes.append(f"deleted branch {branch}")
        else:
            notes.append(f"kept branch {branch} (unmerged) — `git branch -D {branch}` to drop it")
    return True, notes


def archive_task(home: Path, task_id: str) -> Path:
    """Move `tasks/<id>/` into `tasks/.archive/<id>/`. Reversible by moving
    it back; nothing is deleted."""
    source = task_dir(home, task_id)
    if not source.is_dir():
        raise FileNotFoundError(f"no task directory at {source}")
    destination = archived_task_dir(home, task_id)
    if destination.exists():
        raise FileExistsError(f"already archived at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)
    return destination


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


def _trim(text: str) -> str:
    return " ".join(text.split())[:200]
