"""The task substrate: durable records for harness-driven development tasks.

A task is a prompt pointed at a registered project, executed by a
user-configured coding harness (claude, codex, opencode, ...) as a sequence
of *runs*. Everything durable lives under `tasks/<id>/`:

    task.json          spec + reported status + session id + run history
    runner.lock        pid of the active run (mtime doubles as a heartbeat)
    transcript.jsonl   the harness's stdout, one JSON line per line seen
    reports.jsonl      what the task said via `quorum task report`
    runner.log         stderr/bootstrap output of detached runs

Status is a *reported* string, not an enforced state machine: the harness
calls `quorum task report --status <word>` and quorum records whatever word
it chose. Only the TERMINAL_STATUSES set carries meaning inside quorum — the
manager stops attending to a task once it reaches one of them.

`pr_state` sits beside it and is never merged into it: `done` is the
harness's word about itself, `merged` is the forge's word about the pull
request, and `record_pr_state` — the single writer, called only from the
manager tick's CI probe — keeps them separate on purpose.

Guidance flows the other way through the ordinary message bus: each task
owns the inbox `task-<id>`, and the runner injects claimed messages into the
next run's prompt.

A task may declare `depends_on` (`task add --after <id>`): tasks it must not
start before. That is *not* a scheduler — the manager still decides every
launch. `dependency_state` reads the list back for the digest, the views and
the runner's one narrow refusal.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import fsio
from .messages import MessageBus

BOARD_TOPIC = "tasks"

# The statuses quorum itself reacts to. "done" and "cancelled" end a task;
# "blocked" parks it for a human. Every other status string is free-form and
# merely displayed.
TERMINAL_STATUSES = {"done", "blocked", "cancelled"}

# The vocabulary of `Task.pr_state`: what the *forge* says about the pull
# request a task's branch belongs to, as opposed to what the harness said
# about itself. Deliberately forge-neutral (`ci.PR_STATE_WORDS` maps GitHub's
# OPEN/MERGED/CLOSED into it, and #51's GitLab backend will map its own) and
# deliberately closed — a state we do not recognize is never written, because
# nothing re-probes a task once its worktree is gone.
PR_STATES = frozenset({"open", "merged", "closed"})


class TaskRun(BaseModel):
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None
    # The runner's auto-commit note for this run ("auto-committed N path(s)
    # as <sha>" / "auto-commit failed: ..."), None when the net didn't fire —
    # the durable record that quorum, not the harness, committed that work.
    auto_commit: str | None = None
    # What the harness said this run spent (usage.py): canonical token
    # counts and cost, or None when it reported nothing — most harnesses
    # say nothing, and every reader treats absence as "unknown", never zero.
    usage: dict[str, Any] | None = None


class Task(BaseModel):
    v: int = 1
    id: str
    project: str
    prompt: str
    harness: str
    status: str = "queued"
    session: str | None = None
    pr_url: str | None = None
    use_worktree: bool = True
    workdir: str | None = None  # resolved on first run
    # True: this task *is* a live interactive session the user adopted into
    # quorum (workdir = their checkout, session = the harness's own session).
    # Quorum never spawns runs for it; guidance reaches it through hooks.
    attached: bool = False
    # The herdr pane hosting the session, when it runs inside herdr: enables
    # pane-status observation and the nudge doorbell (herdr.py).
    herdr_pane: str | None = None
    # Full ids of the tasks this one must not start before (`task add
    # --after`). Not a DAG engine: the manager still decides every launch,
    # and these only say when a launch would be premature. See
    # `dependency_state` for how they are read.
    depends_on: list[str] = Field(default_factory=list)
    # True: this task is not expected to finish. It works in cycles — watch,
    # tidy, poll, groom — and only the user ends it. Quorum enforces nothing
    # (status stays free-form and TERMINAL_STATUSES still means what it
    # means); the flag is *observation context*, and it changes three
    # readings of the same substrate: the run preamble softens the
    # deliver-then-report-done conventions into deliver-every-cycle, the
    # digest renders `perpetual=true` and suppresses the `possible-loop`
    # observation (repetition is the job, not a symptom), and the manager
    # prompt is told to relaunch it forever and never call a long run count
    # stuck. See docs/architecture.md ("Perpetual tasks").
    perpetual: bool = False
    # What the forge last said about this task's pull request: one of
    # `PR_STATES`, and when it was observed. Quorum's **one** materialized
    # probe result — written only from the manager tick's digest build
    # (`record_pr_state`), never by a view, so `quorum status` / `task list`
    # / the TUI / the web dashboard can badge a merged task while staying
    # pure file readers. It is an observation and never a status: `done` is
    # the harness's word, merged is the forge's, and quorum never turns one
    # into the other. None means nothing was ever observed — no gh, no PR,
    # `[ci]` off — and reads as "no information", never as "not merged".
    pr_state: str | None = None
    pr_state_at: str | None = None
    runs: list[TaskRun] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @property
    def short_id(self) -> str:
        """The tail of the ULID: its random half. The head is a timestamp,
        shared by every task created in the same instant, so a usable short
        handle must come from the end."""
        return self.id[-6:].lower()


def tasks_dir(home: Path) -> Path:
    return Path(home) / "tasks"


def task_dir(home: Path, task_id: str) -> Path:
    return tasks_dir(home) / task_id


def task_json_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "task.json"


def transcript_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "transcript.jsonl"


def reports_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "reports.jsonl"


def runner_lock_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "runner.lock"


def runner_log_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "runner.log"


def worktree_path(home: Path, task_id: str) -> Path:
    return Path(home) / "worktrees" / task_id


def attached_path(home: Path, task_id: str) -> Path:
    return task_dir(home, task_id) / "attached.json"


def write_attached_state(
    home: Path, task_id: str, event: str, session: str | None = None, now: Any = None
) -> None:
    """Record the adopted session's latest lifecycle event ("adopt",
    "session-start", "stop", "session-end") — the liveness signal for a run
    quorum didn't spawn."""
    fsio.atomic_write_json(
        attached_path(home, task_id),
        {"at": fsio.iso(now or fsio.utc_now()), "event": event, "session": session},
    )


def attached_state(home: Path, task_id: str) -> dict[str, Any] | None:
    try:
        return fsio.read_json(attached_path(home, task_id))
    except (OSError, ValueError):
        return None


def inbox_name(task_id: str) -> str:
    """The bus inbox a task's guidance goes to."""
    return f"task-{task_id}"


class TaskStore:
    """CRUD over tasks/<id>/task.json. All writes are atomic; there is no
    index — the directory listing (ULID names) is the chronological index."""

    def __init__(self, home: Path):
        self.home = Path(home)

    def add(
        self,
        project: str,
        prompt: str,
        harness: str,
        use_worktree: bool = True,
        workdir: str | None = None,
        session: str | None = None,
        attached: bool = False,
        status: str = "queued",
        depends_on: list[str] | None = None,
        perpetual: bool = False,
        now: Any = None,
    ) -> Task:
        created = fsio.iso(now or fsio.utc_now())
        task = Task(
            id=fsio.ulid(),
            project=project,
            prompt=prompt,
            harness=harness,
            use_worktree=use_worktree,
            workdir=workdir,
            session=session,
            attached=attached,
            status=status,
            depends_on=list(depends_on or []),
            perpetual=perpetual,
            created_at=created,
            updated_at=created,
        )
        fsio.atomic_write_json(task_json_path(self.home, task.id), task.model_dump())
        return task

    def get(self, task_id: str) -> Task | None:
        try:
            return Task.model_validate(fsio.read_json(task_json_path(self.home, task_id)))
        except (OSError, ValueError):
            return None

    def list(self) -> list[Task]:
        root = tasks_dir(self.home)
        if not root.is_dir():
            return []
        out = []
        for entry in sorted(p for p in root.iterdir() if p.is_dir() and not fsio.is_tmp(p.name)):
            task = self.get(entry.name)
            if task is not None:
                out.append(task)
        return out

    def resolve(self, handle: str) -> Task:
        """Find a task by full id, unique id prefix, or unique suffix — the
        suffix form is what `short_id` hands out (case-insensitive).

        Raises KeyError when nothing matches, ValueError when the handle is
        ambiguous — callers turn both into friendly CLI errors.
        """
        handle = handle.strip().upper()
        exact = self.get(handle)
        if exact is not None:
            return exact
        matches = [
            t for t in self.list() if t.id.startswith(handle) or t.id.endswith(handle)
        ]
        if not matches:
            raise KeyError(handle)
        if len(matches) > 1:
            raise ValueError(
                f"task handle {handle!r} is ambiguous: {', '.join(t.short_id for t in matches)}"
            )
        return matches[0]

    def update(self, task_id: str, now: Any = None, **fields: Any) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        data = task.model_dump()
        data.update(fields)
        data["updated_at"] = fsio.iso(now or fsio.utc_now())
        task = Task.model_validate(data)
        fsio.atomic_write_json(task_json_path(self.home, task.id), task.model_dump())
        return task


def record_pr_state(home: Path, task: Task, state: str | None, now: Any = None) -> bool:
    """Materialize an observed PR state onto `tasks/<id>/task.json`.

    The single writer of `pr_state` / `pr_state_at`, called from exactly one
    place: the CI probe inside `manager.build_digest`. See
    docs/architecture.md ("The merged observation") for why this one probe
    result is allowed to reach disk when nothing else is.

    Returns True when the file changed. Fail-soft in the shape of everything
    on the probe path — an unreadable or unwritable task.json is a lost
    observation, re-made next tick, never a failed digest:

    - an unknown state (including `ci`'s "unknown") writes nothing, so a
      shape we did not understand can never badge a task as delivered;
    - an unchanged state writes nothing, so a quiet home is not rewritten
      every tick;
    - `updated_at` is deliberately left alone. It means "someone acted on
      this task", and the digest's own recently-finished window is measured
      from it — a probe that touched it would pin every merged task in that
      section forever.

    The record is re-read from disk rather than dumped from `task` for the
    same reason: a live run may have reported a new status since this Task
    was loaded, and an observation must never roll that back.
    """
    if state not in PR_STATES:
        return False
    if task.pr_state == state:
        return False
    path = task_json_path(home, task.id)
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    data["pr_state"] = state
    data["pr_state_at"] = fsio.iso(now or fsio.utc_now())
    try:
        fsio.atomic_write_json(path, data)
    except OSError:
        return False
    # Keep the caller's in-memory record true, so the digest being built
    # right now agrees with the file it just wrote.
    task.pr_state = state
    task.pr_state_at = data["pr_state_at"]
    return True


def short_handle(task_id: str) -> str:
    """The short id of a task id, without needing the record — dependencies
    may name a task whose file is gone."""
    return task_id[-6:].lower()


def resolve_dependencies(
    store: TaskStore, handles: Iterable[str], self_id: str | None = None
) -> list[str]:
    """Turn `--after` handles into full task ids, in order, deduplicated.

    Raises ValueError (message ready for the CLI) for anything that could
    never be a usable dependency: an unknown or ambiguous handle, and the
    task itself. This is the *only* validated entry point for dependencies —
    a hand-edited task.json can still say anything, which is why the readers
    below are total.
    """
    resolved: list[str] = []
    for handle in handles:
        try:
            dep = store.resolve(handle)
        except KeyError:
            raise ValueError(f"no task matching {handle!r} — `quorum task list`") from None
        except ValueError as e:
            raise ValueError(str(e)) from None
        if self_id is not None and dep.id == self_id:
            raise ValueError(f"task {dep.short_id} cannot depend on itself")
        # A perpetual task never reaches a terminal status, so a dependent
        # would wait on it forever — refuse the chain at the one validated
        # entry point rather than queue work that can never start.
        if dep.perpetual:
            raise ValueError(
                f"task {dep.short_id} is perpetual — it never finishes, so nothing "
                "may depend on it"
            )
        if dep.id not in resolved:
            resolved.append(dep.id)
    return resolved


def _reaches_cycle(start: Task, by_id: Mapping[str, Task]) -> bool:
    """Whether following `depends_on` from this task ever revisits a task
    already on the path. Only reachable by hand-editing task.json (`task add`
    can only point at tasks that already exist), so this exists to make the
    readers total rather than to police anything."""
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[tuple[str, bool]] = [(start.id, False)]
    while stack:
        tid, closing = stack.pop()
        if closing:
            visiting.discard(tid)
            done.add(tid)
            continue
        if tid in done:
            continue
        if tid in visiting:
            return True
        visiting.add(tid)
        stack.append((tid, True))
        node = by_id.get(tid)
        for dep in node.depends_on if node else ():
            stack.append((dep, False))
    return False


def dependency_state(task: Task, by_id: Mapping[str, Task]) -> dict[str, Any]:
    """How a task's declared dependencies stand, over an already-loaded
    lookup of every task (so views, the digest and the runner all read
    dependencies the same way, from one listing).

    Total by design — a hand-edited `depends_on` never raises here.

    The policy in one line: **only a dependency that still might finish
    blocks.** Everything that never will — ended `blocked`/`cancelled`, or
    gone from disk — is reported as what it is and left for the manager to
    judge, because "waiting" on something unsatisfiable hides the decision
    instead of surfacing it.

      waiting_on  short ids of dependencies that have not reached a terminal
                  status. This is what the digest renders and what `task run`
                  refuses on.
      failed      short ids of dependencies that ended `blocked`/`cancelled`
                  — satisfied they never will be. An observation for the
                  manager to judge (nudge, cancel, escalate), never a rail:
                  they are *not* in `waiting_on`, so nothing is blocked by one.
      missing     handles named by `depends_on` with no task record — the
                  same class as `failed` (an upstream that can never reach
                  `done`), and treated identically: reported, never waited on.
      cycle       True when the dependency chain loops (it would wait forever).
    """
    waiting_on: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    for dep_id in task.depends_on:
        dep = by_id.get(dep_id)
        if dep is None:
            missing.append(short_handle(dep_id))
        elif dep.status not in TERMINAL_STATUSES:
            waiting_on.append(dep.short_id)
        elif dep.status != "done":
            failed.append(dep.short_id)
    return {
        "waiting_on": waiting_on,
        "failed": failed,
        "missing": missing,
        "cycle": _reaches_cycle(task, by_id) if task.depends_on else False,
    }


def dependency_states(all_tasks: list[Task]) -> dict[str, dict[str, Any]]:
    """`dependency_state` for every task that declares dependencies, keyed by
    full task id — one listing, one pass, for readers that render many rows."""
    by_id = {t.id: t for t in all_tasks}
    return {t.id: dependency_state(t, by_id) for t in all_tasks if t.depends_on}


def report(
    home: Path,
    task_id: str,
    status: str,
    text: str = "",
    pr_url: str | None = None,
    sender: str | None = None,
    now: Any = None,
) -> Task:
    """Record a progress report from the task itself (or a human on its behalf).

    Appends to reports.jsonl, updates the task's status (and pr_url when
    given), and mirrors the report onto the board so dashboards and the
    manager see it without polling every task directory's log.
    """
    home = Path(home)
    store = TaskStore(home)
    task = store.resolve(task_id)
    at = fsio.iso(now or fsio.utc_now())
    fsio.append_jsonl(
        reports_path(home, task.id),
        {"at": at, "status": status, "text": text, **({"pr_url": pr_url} if pr_url else {})},
    )
    updates: dict[str, Any] = {"status": status}
    if pr_url:
        updates["pr_url"] = pr_url
    task = store.update(task.id, **updates)
    MessageBus(home).post(
        sender or inbox_name(task.id),
        BOARD_TOPIC,
        f"task.{status}",
        text=text or f"task {task.short_id} reported {status}",
        payload={"task": task.id, "status": status, **({"pr_url": pr_url} if pr_url else {})},
    )
    return task


def nudge(home: Path, task: Task, text: str, sender: str = "user"):
    """Queue guidance for a task — the single write path shared by the CLI,
    TUI, and web. When the task lives in a herdr pane, also ring the pane's
    doorbell; the payload stays in the inbox (exactly-once delivery), the
    doorbell only says something is waiting."""
    msg = MessageBus(home).send(sender, inbox_name(task.id), type="guidance", text=text)
    if task.herdr_pane:
        from . import herdr  # fail-soft adapter; a dead herdr never blocks a nudge

        herdr.ring_doorbell(
            home,
            task.herdr_pane,
            f"quorum guidance waiting — run: quorum task inbox {task.short_id} --claim",
        )
    return msg


def read_transcript_tail(
    home: Path, task_id: str, limit: int = 40, max_bytes: int = 256 * 1024
) -> list[dict]:
    # max_bytes binds before limit: callers that need depth in entries (the
    # manager's loop scan) must size the byte budget for their payloads.
    return fsio.read_jsonl_tail(transcript_path(home, task_id), limit=limit, max_bytes=max_bytes)


def read_reports(home: Path, task_id: str, limit: int | None = None) -> list[dict]:
    entries = fsio.read_jsonl(reports_path(home, task_id))
    return entries[-limit:] if limit else entries


def runner_alive(home: Path, task_id: str) -> bool:
    """Whether a runner process currently holds this task's lock."""
    try:
        meta = fsio.read_json(runner_lock_path(home, task_id))
        pid = int(meta.get("pid", -1))
    except (OSError, ValueError):
        return False
    return pid > 0 and fsio.pid_alive(pid)


def last_activity(home: Path, task_id: str) -> datetime | None:
    """The newest sign of life: transcript, reports, the runner lock, or an
    adopted session's hook-written attached.json."""
    newest = None
    for path in (
        transcript_path(home, task_id),
        reports_path(home, task_id),
        runner_lock_path(home, task_id),
        attached_path(home, task_id),
    ):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=UTC)


def workdir_git_state(task: Task) -> dict[str, Any] | None:
    """Git state of the task's working directory, or None when there is
    nothing to probe (no workdir resolved yet, directory gone, not git).

    Work only counts as delivered once it is committed *and* pushed;
    anything less is stranded in the worktree the moment attention moves
    on. This probe is how stranded work stays visible: views and the
    manager digest surface `dirty` (uncommitted paths) and `unpushed`
    (commits on HEAD that no remote ref has; None when the repository has
    no remote to push to). Read-only — a few git plumbing calls.
    """
    if not task.workdir:
        return None
    workdir = Path(task.workdir)
    if not workdir.is_dir():
        return None

    def git(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    # --untracked-files=all: a repo-level `status.showUntrackedFiles no`
    # must not hide an untracked-only dirty tree from the stranded-work probe.
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None or status.returncode != 0:
        return None
    dirty = sum(1 for line in status.stdout.splitlines() if line.strip())
    head = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = head.stdout.strip() if head is not None and head.returncode == 0 else ""
    unpushed: int | None = None
    remotes = git("remote")
    if remotes is not None and remotes.returncode == 0 and remotes.stdout.strip():
        count = git("rev-list", "--count", "HEAD", "--not", "--remotes")
        if count is not None and count.returncode == 0:
            try:
                unpushed = int(count.stdout.strip())
            except ValueError:
                unpushed = None
    return {"branch": branch, "dirty": dirty, "unpushed": unpushed}
