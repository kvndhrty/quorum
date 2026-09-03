"""Shared read-model for `quorum status`, the web dashboard, and the TUI.

Everything here is assembled purely from files under QUORUM_HOME, so every
view works whether or not the supervisor is running.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import fsio, prune, usage
from .config import Config, load_config_or_default, parse_schedule
from .messages import MessageBus
from .projects import ProjectRegistry
from .supervisor import LOCK_TOUCH_SECONDS
from .tasks import (
    TERMINAL_STATUSES,
    Task,
    TaskStore,
    attached_state,
    dependency_states,
    inbox_name,
    issue_ref,
    read_reports,
    runner_alive,
    short_handle,
    task_dir,
    workdir_git_state,
)

SUPERVISOR_STALE_AFTER = LOCK_TOUCH_SECONDS * 3

# How long after a task goes terminal its workdir keeps being probed for
# stranded work. Views refresh often (the TUI every 2s) and each probe is
# a few git subprocesses, so long-settled tasks stop being probed.
GIT_PROBE_TERMINAL_HOURS = 24


def supervisor_status(home: Path) -> dict[str, Any]:
    lock = home / "supervisor.lock"
    if not lock.exists():
        return {"alive": False}
    try:
        meta = fsio.read_json(lock)
        age = time.time() - lock.stat().st_mtime
    except (OSError, ValueError):
        return {"alive": False}
    pid = meta.get("pid")
    # The mtime heartbeat is only touched once a minute, so on its own it
    # reports a supervisor that crashed seconds ago (leaving its lock behind)
    # as running. Ask the OS whether the recorded pid is still there too.
    alive = age < SUPERVISOR_STALE_AFTER and isinstance(pid, int) and fsio.pid_alive(pid)
    return {
        "alive": alive,
        "pid": pid,
        "started_at": meta.get("started_at"),
        "lock_age_seconds": int(age),
    }


def _estimate_next_run(schedule: str, hb: dict[str, Any], now) -> str | None:
    """Best-effort next-fire estimate from the schedule alone, for when the
    live scheduler's answer (heartbeat `next_run`) is missing or stale — the
    heartbeat is only written by a running supervisor."""
    try:
        kwargs = parse_schedule(schedule)
    except Exception:
        return None
    if kwargs.pop("trigger") == "interval":
        try:
            base = fsio.parse_iso(hb["last_end"]) if hb.get("last_end") else now
        except (KeyError, ValueError):
            base = now
        nxt = base + timedelta(**kwargs)
        return fsio.iso(max(nxt, now))  # overdue → due as soon as the supervisor is back
    try:
        from apscheduler.triggers.cron import CronTrigger

        nxt = CronTrigger(**kwargs).get_next_fire_time(None, now)
        return fsio.iso(nxt) if nxt else None
    except Exception:
        return None


def agent_rows(home: Path, config: Config | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = load_config_or_default(home)
    now = fsio.utc_now()
    rows = []
    for name, acfg in sorted(config.agents.items()):
        hb_path = home / "state" / "agents" / name / "heartbeat.json"
        hb: dict[str, Any] = {}
        try:
            hb = fsio.read_json(hb_path)
        except (OSError, ValueError):
            pass
        status = hb.get("status", "never-ran")
        next_run = hb.get("next_run")
        estimated = False
        if not acfg.enabled or status in ("paused", "removed"):
            next_run = None
        else:
            try:
                stale = next_run is None or fsio.parse_iso(next_run) < now
            except ValueError:
                stale = True
            if stale:
                est = _estimate_next_run(acfg.schedule, hb, now)
                if est:
                    next_run, estimated = est, True
        # What this agent's own harness runs have cost (harness-driven agents
        # only; None whenever nothing was reported — never read as zero).
        spent = usage.agent_usage(home, name)
        rows.append(
            {
                "name": name,
                "type": acfg.type,
                "schedule": acfg.schedule,
                "enabled": acfg.enabled,
                "status": status,
                "last_start": hb.get("last_start"),
                "last_end": hb.get("last_end"),
                "duration_ms": hb.get("duration_ms"),
                "next_run": next_run,
                "next_run_estimated": estimated,
                "error": hb.get("error"),
                "usage": spent,
                # Rendered once, here, so the CLI, TUI and browser agree.
                "usage_text": usage.describe_agent(spent),
            }
        )
    return rows


def agent_detail(home: Path, name: str) -> dict[str, Any] | None:
    """One agent's row plus its recent activity: the auto-recorded action
    journal (harness-driven agents), the standing notes in its notebook, and
    its `logs/actions.jsonl` entries."""
    from . import notes
    from .actor import journal_path

    config = load_config_or_default(home)
    row = next((r for r in agent_rows(home, config) if r["name"] == name), None)
    if row is None:
        return None
    acfg = config.agents.get(name)
    row["settings"] = dict(acfg.settings) if acfg else {}
    row["journal"] = fsio.read_jsonl_tail(journal_path(home, name), limit=20)
    # The notebook, straight off its file — what the agent's next run reads,
    # rendered by the same code the digest uses so every reader agrees.
    row["notes"] = notes.active(home, name)
    row["notes_text"] = "\n".join(
        notes.render_section(row["notes"], unscanned=notes.unscanned_bytes(home, name))
    )
    row["actions"] = [
        a
        for a in fsio.read_jsonl_tail(home / "logs" / "actions.jsonl", max_bytes=512 * 1024)
        if a.get("agent") == name
    ][-20:]
    return row


def project_rows(home: Path) -> list[dict[str, Any]]:
    registry = ProjectRegistry(home)
    today = fsio.utc_now().date()
    return [
        {
            "slug": p.slug,
            "name": p.name,
            "path": p.path,
            "tags": p.tags,
            "deadline": p.deadline,
            "days_left": p.days_left(today),
            "notes": p.notes,
        }
        for p in registry.list()
    ]


def task_rows(home: Path, config: Config | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = load_config_or_default(home)
    budget = config.tasks
    rows = []
    now = fsio.utc_now()
    all_tasks = TaskStore(home).list()
    # One pass over the listing we already have: dependencies are read, never
    # materialized, so every view stays a pure file reader.
    deps = dependency_states(all_tasks)
    for t in all_tasks:
        last = read_reports(home, t.id, limit=1)
        git_state = None
        if t.status not in TERMINAL_STATUSES or (
            (now - fsio.parse_iso(t.updated_at)).total_seconds()
            < GIT_PROBE_TERMINAL_HOURS * 3600
        ):
            git_state = workdir_git_state(t)
        spent = usage.total(r.usage for r in t.runs)
        rows.append(
            {
                "id": t.id,
                "id_short": t.short_id,
                "project": t.project,
                "prompt": t.prompt,
                "status": t.status,
                "harness": t.harness,
                "running": runner_alive(home, t.id),
                "attached": t.attached,
                # A task that is never expected to finish (task add
                # --perpetual): views badge it, so "still running after 40
                # runs" reads as working, not stuck.
                "perpetual": t.perpetual,
                # The user's ordering hint and parking brake. Rendered here
                # and nowhere sorted: `task_rows` stays in the store's
                # chronological order whatever the priorities say, because
                # deciding what runs next is the manager's job, not a view's.
                "priority": t.priority,
                "held": t.held,
                "attached_state": attached_state(home, t.id) if t.attached else None,
                "runs": len(t.runs),
                # Absent (None) whenever no run reported usage — the common
                # case for harnesses that say nothing, and never read as 0.
                "usage": spent,
                # The same thing rendered once, here, so the CLI, TUI and the
                # browser all show a spend the same way.
                "usage_text": usage.describe(spent),
                "budget_overages": usage.run_overages(
                    t.runs, budget.max_cost_per_run, budget.max_tokens_per_run
                ),
                # True while the *last* run is over budget: `task run`
                # refuses the next one until --force or a cheaper run
                # (runner.budget_blockers). Rendered, never enforced, here.
                "budget_gated": bool(
                    usage.last_run_overages(
                        t.runs, budget.max_cost_per_run, budget.max_tokens_per_run
                    )
                ),
                # Where the task came from: the full url, plus the short
                # `#62` every surface renders (tasks.issue_ref, so the CLI,
                # TUI and browser abbreviate it identically). "" when the
                # task was not queued from an issue.
                "issue_url": t.issue_url,
                "issue_ref": issue_ref(t.issue_url),
                "pr_url": t.pr_url,
                # What the forge last said about that PR (open/merged/closed)
                # and when. The one field here that came from a probe rather
                # than from the task itself — materialized by the manager
                # tick precisely so this stays a pure file read. None means
                # "never observed", not "not merged".
                "pr_state": t.pr_state,
                "pr_state_at": t.pr_state_at,
                # Dependencies as the views render them: short ids
                # throughout, since every consumer here displays rather than
                # links. `t.depends_on` holds the full ids for anyone who
                # needs one.
                "depends_on": [short_handle(d) for d in t.depends_on],
                # Only `waiting_on` blocks. `dep_failed`/`dep_missing` name
                # dependencies that can never be satisfied — shown so the
                # decision is visible, not waited on.
                "waiting_on": deps.get(t.id, {}).get("waiting_on", []),
                "dep_failed": deps.get(t.id, {}).get("failed", []),
                "dep_missing": deps.get(t.id, {}).get("missing", []),
                "dep_cycle": deps.get(t.id, {}).get("cycle", False),
                "git": git_state,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
                "last_report": last[-1].get("text", "") if last else "",
            }
        )
    return rows


# -- task history ----------------------------------------------------------
#
# One chronological list of what happened to a task, read back out of the
# files that already record it — `task.json` (queued, runs, the PR
# observation), `reports.jsonl`, the task's inbox and the message archive
# (guidance), every agent's action journal (what was done to it), and the
# archive directory (that it was pruned). Nothing here is recorded for the
# list's sake: if a fact is missing, the fix is to record it where it
# happens, never to cache it here. See docs/architecture.md ("Task history").

#: how far back into an agent's journal the history looks. Journals are
#: append-only and unbounded, and a task's actions can be anywhere in one, so
#: this is a completeness bound, not a tail: well past the digest's own
#: window, and a history over an older journal says nothing about what fell
#: outside it — an agent that acted on a task a hundred megabytes ago is a
#: home nobody has pruned.
HISTORY_JOURNAL_BYTES = 8 * 1024 * 1024

# Row kinds, in the order the list emits them for one instant — a stable sort
# on `at` keeps this order among rows stamped the same second, so a task
# queued and launched inside one second still reads queued → run started.
_HISTORY_KINDS = (
    "queued",
    "action",
    "guidance",
    "run.started",
    "report",
    "run.ended",
    "pr_state",
    "archived",
)


def _human_at(at: str) -> str:
    return str(at or "").replace("T", " ").rstrip("Z")


def history_line(row: dict[str, Any]) -> str:
    """One history row as the line every surface prints: `[at] text`."""
    return f"[{_human_at(row.get('at', ''))}] {row.get('text', '')}"


def _run_started_text(n: int, fresh: bool, live: bool) -> str:
    text = f"run {n} started"
    if fresh:
        text += " · fresh session"
    if live:
        text += " · still running"
    return text


def _run_ended_text(n: int, run: Any) -> str:
    parts = [f"run {n} ended"]
    code = run.exit_code
    if run.stopped:
        how = "stopped by `task stop`"
        if isinstance(code, int) and code < 0:
            try:
                import signal

                how += f" ({signal.Signals(-code).name})"
            except ValueError:
                pass
        parts.append(how)
    elif code is None:
        parts.append("exit —")
    else:
        parts.append(f"exit {code}")
    if run.stalled:
        parts.append("stalled (no harness output)")
    spent = usage.describe(run.usage)
    if spent:
        parts.append(spent)
    if run.auto_commit:
        parts.append(run.auto_commit)
    return " · ".join(parts)


def _journal_rows(home: Path, task: Task) -> list[dict[str, Any]]:
    """Every agent's journaled actions on this task: entries whose `target` is
    the task's short id, plus a `task.prune` whose args name it (a prune is
    journaled once per command, listing the tasks it swept)."""
    from .actor import journal_path

    paths = [journal_path(home, "manager")]
    agents_root = home / "state" / "agents"
    if agents_root.is_dir():
        for entry in sorted(agents_root.iterdir()):
            if entry.is_dir() and not fsio.is_tmp(entry.name) and entry.name != "manager":
                paths.append(journal_path(home, entry.name))
    rows = []
    for path in paths:
        for e in fsio.read_jsonl_tail(path, max_bytes=HISTORY_JOURNAL_BYTES):
            if not isinstance(e, dict):
                continue
            action = str(e.get("action") or "")
            args = str(e.get("args") or "")
            if e.get("target") == task.short_id:
                pass
            elif action == "task.prune" and task.short_id in _pruned_ids(args):
                pass
            else:
                continue
            actor = str(e.get("actor") or "?")
            text = f"{actor}: {action}"
            if args:
                text += f" — {args}"
            then = e.get("target_status")
            if then:
                text += f" (status then {then})"
            rows.append(
                {
                    "at": str(e.get("at") or ""),
                    "kind": "action",
                    "text": text,
                    "actor": actor,
                    "action": action,
                    "args": args or None,
                    "target_status": then,
                    "agent_run": e.get("run") or None,
                }
            )
    return rows


def _pruned_ids(args: str) -> set[str]:
    """The short ids a `task.prune` journal entry lists: `"N task(s): a, b
    +worktrees"` → {a, b}. Anything not in that shape yields nothing."""
    if ":" not in args:
        return set()
    listing = args.split(":", 1)[1].replace("+worktrees", "")
    return {part.strip() for part in listing.split(",") if part.strip()}


def _guidance_rows(home: Path, task: Task) -> list[dict[str, Any]]:
    """Guidance sent to the task's inbox, in the three states it can be in on
    disk: waiting (`new/`), claimed but not yet acked (`cur/`), and consumed
    (the message archive — what a run acks after injecting it, and also what
    `task inbox --clear` archives without delivering; the record does not say
    which). Stamped when it was *sent*: delivery itself writes no time."""
    bus = MessageBus(home)
    inbox = inbox_name(task.id)
    try:
        since = fsio.parse_iso(task.created_at)
    except ValueError:
        since = None
    found: list[tuple[str, Any]] = []
    found += [("delivered", m) for m in bus.archived_direct(inbox, since=since)]
    found += [("claimed", m) for m in bus.inbox_messages(inbox, "cur")]
    found += [("waiting", m) for m in bus.inbox_messages(inbox, "new")]
    rows = []
    for state, m in found:
        note = str(m.payload.get("text", ""))
        marker = "" if state == "delivered" else f" ({state})"
        rows.append(
            {
                "at": m.created_at,
                "kind": "guidance",
                "text": f"guidance from {m.sender}{marker}: {note}",
                "from": m.sender,
                "note": note,
                "state": state,
                "id": m.id,
            }
        )
    return rows


def task_history(home: Path, task: Task, root: Path | None = None) -> list[dict[str, Any]]:
    """Everything that happened to one task, oldest first.

    A pure reader over what is already on disk; every row carries `at`
    (ISO-8601 UTC), `kind` (one of `_HISTORY_KINDS`) and `text` (the line
    every surface prints — `history_line`), plus the raw fields of its kind.
    `root` is the task's directory, which for a pruned task is under
    `tasks/.archive/` (the caller resolved it there; see
    `prune.resolve_archived`) — the one row with no record of its own,
    `archived`, is stamped from that directory's ctime, which a rename
    updates.

    Bounded reads throughout (`HISTORY_JOURNAL_BYTES`; the archive from the
    task's own month on) and fail-soft in the read model's way: a torn line
    or an unreadable file costs the rows it held, never the list.
    """
    home = Path(home)
    root = Path(root) if root is not None else task_dir(home, task.id)
    rows: list[dict[str, Any]] = []
    queued = f"queued on {task.project} · harness {task.harness}"
    if ref := issue_ref(task.issue_url):
        queued += f" · from {ref}"
    if task.depends_on:
        queued += " · after " + ", ".join(short_handle(d) for d in task.depends_on)
    if task.attached:
        queued = f"adopted on {task.project} · harness {task.harness} (a live session)"
    rows.append(
        {
            "at": task.created_at,
            "kind": "queued",
            "text": queued,
            "project": task.project,
            "harness": task.harness,
            "issue_url": task.issue_url,
        }
    )
    rows += _journal_rows(home, task)
    rows += _guidance_rows(home, task)
    for n, run in enumerate(task.runs, start=1):
        rows.append(
            {
                "at": run.started_at,
                "kind": "run.started",
                "text": _run_started_text(n, run.fresh_session, live=False),
                "run": n,
                "fresh_session": run.fresh_session,
                "live": False,
            }
        )
        if run.ended_at:
            rows.append(
                {
                    "at": run.ended_at,
                    "kind": "run.ended",
                    "text": _run_ended_text(n, run),
                    "run": n,
                    "exit_code": run.exit_code,
                    "stopped": run.stopped,
                    "stalled": run.stalled,
                    "fresh_session": run.fresh_session,
                    "usage": run.usage,
                    "usage_text": usage.describe(run.usage),
                    "auto_commit": run.auto_commit,
                }
            )
    # The run in progress has no record yet — the runner writes one when it
    # ends — but its lock says when it began, and a live process holds it.
    if runner_alive(home, task.id):
        try:
            started = str(fsio.read_json(root / "runner.lock").get("started_at") or "")
        except (OSError, ValueError, AttributeError):
            started = ""
        if started:
            n = len(task.runs) + 1
            rows.append(
                {
                    "at": started,
                    "kind": "run.started",
                    "text": _run_started_text(n, False, live=True),
                    "run": n,
                    "fresh_session": False,
                    "live": True,
                }
            )
    for r in fsio.read_jsonl(root / "reports.jsonl"):
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or "")
        note = str(r.get("text") or "")
        text = f"reported {status}" + (f": {note}" if note else "")
        if r.get("pr_url"):
            text += f" · {r['pr_url']}"
        rows.append(
            {
                "at": str(r.get("at") or ""),
                "kind": "report",
                "text": text,
                "status": status,
                "note": note,
                "pr_url": r.get("pr_url"),
            }
        )
    if task.pr_state and task.pr_state_at:
        text = f"pr state observed: {task.pr_state}"
        if task.pr_url:
            text += f" · {task.pr_url}"
        rows.append(
            {
                "at": task.pr_state_at,
                "kind": "pr_state",
                "text": text,
                "state": task.pr_state,
                "pr_url": task.pr_url,
            }
        )
    archived = prune.archived_task_dir(home, task.id)
    if archived.is_dir():
        try:
            from datetime import UTC, datetime

            at = fsio.iso(datetime.fromtimestamp(archived.stat().st_ctime, tz=UTC))
        except OSError:
            at = ""
        rows.append(
            {
                "at": at,
                "kind": "archived",
                "text": "archived by `task prune` (moved to tasks/.archive; `mv` restores it)",
            }
        )
    order = {kind: i for i, kind in enumerate(_HISTORY_KINDS)}
    rows.sort(key=lambda r: (r["at"], order.get(r["kind"], len(order))))
    return rows


def board_tail(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    bus = MessageBus(home)
    msgs = []
    for topic in bus.topics():
        for m in bus.read_topic(topic, limit=limit):
            msgs.append(
                {
                    "id": m.id,
                    "short_id": m.short_id,
                    "at": m.created_at,
                    "topic": topic,
                    "from": m.sender,
                    "type": m.type,
                    "text": m.payload.get("text", ""),
                }
            )
    msgs.sort(key=lambda m: m["at"])
    return msgs[-limit:]


def recent_actions(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    return fsio.read_jsonl(home / "logs" / "actions.jsonl")[-limit:]


# The board has no read-state, so "needs a look" is time-bounded rather than
# tracked: recent posts on the escalation topic. Old escalations age out of
# the summary (and are eventually archived by the janitor); a handled one is
# dropped early by acking it (`quorum board ack`, TUI `a`, the web Ack button),
# which archives the message rather than marking it — see
# `MessageBus.ack_board_message`. Each entry therefore carries its id, because
# that is the handle every ack affordance needs.
ATTENTION_WINDOW_DAYS = 7
#: how many of those the *lists* carry — the TUI's `a` picker and the web
#: panel, both of which ack a line and so need one entry per escalation the
#: banner counts. `attention_summary`'s own default stays small for the
#: banner-shaped callers that only ever show a couple.
ATTENTION_LIST_LIMIT = 50


def attention_summary(home: Path, days: int = ATTENTION_WINDOW_DAYS, limit: int = 5) -> dict[str, Any]:
    """Recent posts on the `attention` topic — the manager's ask-a-human channel."""
    floor = fsio.utc_now() - timedelta(days=days)
    msgs = MessageBus(home).read_topic("attention", since=floor)
    return {
        "count": len(msgs),
        "days": days,
        "recent": [
            {
                "id": m.id,
                "short_id": m.short_id,
                "at": m.created_at,
                "from": m.sender,
                "text": m.payload.get("text", ""),
            }
            for m in msgs[-limit:]
        ],
    }


def overview(home: Path) -> dict[str, Any]:
    config = load_config_or_default(home)
    return {
        "home": str(home),
        "supervisor": supervisor_status(home),
        "agents": agent_rows(home, config),
        "tasks": task_rows(home, config),
        "projects": project_rows(home),
        "board": board_tail(home),
        # The full list, not the banner's handful: `overview` is what the
        # web dashboard reads, and its Attention panel offers an Ack button
        # per line — an escalation the panel never renders cannot be acked
        # there at all.
        "attention": attention_summary(home, limit=ATTENTION_LIST_LIMIT),
        "actions": recent_actions(home),
    }
