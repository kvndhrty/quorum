"""Shared read-model for `quorum status`, the web dashboard, and the TUI.

Everything here is assembled purely from files under QUORUM_HOME, so every
view works whether or not the supervisor is running.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import fsio, usage
from .config import Config, load_config_or_default, parse_schedule
from .messages import MessageBus
from .projects import ProjectRegistry
from .supervisor import LOCK_TOUCH_SECONDS
from .tasks import (
    TERMINAL_STATUSES,
    TaskStore,
    attached_state,
    dependency_states,
    read_reports,
    runner_alive,
    short_handle,
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
        "attention": attention_summary(home),
        "actions": recent_actions(home),
    }
