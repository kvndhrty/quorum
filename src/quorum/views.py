"""Shared read-model for `quorum status` and the TUI.

Everything here is assembled purely from files under QUORUM_HOME, so every
view works whether or not the supervisor is running.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import fsio, prune, usage
from .actor import SelfRun
from .agent import read_heartbeat
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
    meta = fsio.read_json_or(lock, {})
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
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
        base = fsio.parse_iso_or(hb.get("last_end"), now)
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
        hb = read_heartbeat(home, name)
        status = hb.get("status", "never-ran")
        next_run = hb.get("next_run")
        estimated = False
        if not acfg.enabled or status in ("paused", "removed"):
            next_run = None
        else:
            due = fsio.parse_iso_or(next_run)
            if due is None or due < now:
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
    book = notes.agent_notebook(home, name)
    row["notes"] = book.active()
    row["notes_text"] = "\n".join(book.render())
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


# -- one rendering of a task row -------------------------------------------
#
# `task_rows` returns facts; these four turn them into the marks a person
# reads, once, for every surface. `quorum status --legend` names exactly
# this set of glyphs, and `usage_text` above set the precedent: a thing
# shown two ways is rendered here and copied there.


def task_marker(row: dict[str, Any]) -> str:
    """The one-character state marker that leads a task's id cell.

    Liveness first, because it is what a reader is looking for: an adopted
    session, a live run, then the terminal statuses quorum itself knows
    (`done`, `blocked`), then `·` for a status only the harness understands.
    """
    if row.get("attached"):
        return "⚭"
    if row.get("running"):
        return "▶"
    return {"done": "✓", "blocked": "✗"}.get(row.get("status") or "", "·")


def task_badges(row: dict[str, Any]) -> str:
    """The marks that follow a task's status word: `∞` for a perpetual task,
    then the forge's word about its pull request.

    "done ✔" is delivered and "done ⊘" is a pull request somebody closed
    unmerged. The absence of both means nothing was ever observed — the
    manager tick materializes `pr_state`, so a home with no `gh` never
    badges one.
    """
    marks = " ∞" if row.get("perpetual") else ""
    return marks + {"merged": " ✔", "closed": " ⊘"}.get(row.get("pr_state") or "", "")


def task_flags(row: dict[str, Any]) -> str:
    """Stranded work and unsatisfied dependencies: the observations that ask
    a reader (or the manager) to decide something.

    Only `waiting-on` blocks a run. `DEP-FAILED` / `DEP-MISSING` /
    `DEP-CYCLE` name dependencies that can never finish, so nothing waits on
    them and the decision is a person's.
    """
    flags = []
    git = row.get("git")
    if git and (git["dirty"] or git["unpushed"]):
        risks = []
        if git["dirty"]:
            risks.append(f"{git['dirty']} uncommitted")
        if git["unpushed"]:
            risks.append(f"{git['unpushed']} unpushed")
        flags.append("⚠ " + ", ".join(risks))
    if row.get("waiting_on"):
        flags.append(f"waiting-on {','.join(row['waiting_on'])}")
    if row.get("dep_failed"):
        flags.append(f"DEP-FAILED {','.join(row['dep_failed'])}")
    if row.get("dep_missing"):
        flags.append(f"DEP-MISSING {','.join(row['dep_missing'])}")
    if row.get("dep_cycle"):
        flags.append("DEP-CYCLE")
    return "  ".join(flags)


def usage_badge(row: dict[str, Any]) -> str:
    """A task's spend with the budget marks on it, or "" when the harness
    reported nothing.

    `$!` says a run went over `[tasks].max_cost_per_run` /
    `max_tokens_per_run`; `$! GATED` says the *last* run did, which is the
    one the runner refuses to follow until `--force` or a cheaper run. The
    gate is the sharper case, so it is spelled out rather than left to the
    refusal.
    """
    text = row.get("usage_text") or ""
    if row.get("budget_gated"):
        return f"{text} $! GATED".strip()
    if row.get("budget_overages"):
        return f"{text} $!".strip()
    return text


# -- one rendering of a task record ----------------------------------------
#
# The whole record as rows, so the surfaces that print it cannot disagree
# about what it says. `task show` used to hand-write every line and return
# the bare record for `--json`, and the two had already drifted: the text
# printed `dependents:` and the handoff body, the JSON printed neither.

#: the sections of a task record, in the order `task_detail` emits them.
#: `self` is the one a record only has when it is read from inside the run it
#: describes (`task_self_detail`); everything else is there for any reader.
_DETAIL_SECTIONS = ("record", "self", "reports", "notebook", "handoff", "more")

#: how many of a task's reports the record shows, newest last.
DETAIL_REPORTS = 10


def detail_line(row: dict[str, Any]) -> str:
    """One `task_detail` row as the line every surface prints: a `heading` at
    the margin, a `field` whose value is aligned one column past the longest
    common label, a `body` line indented under the heading it belongs to."""
    text = str(row.get("text", ""))
    if row.get("kind") == "heading":
        return text
    label = row.get("label") or ""
    if not label:
        return f"  {text}"
    return f"  {label + ':':<9} {text}"


def task_detail(
    home: Path,
    task: Task,
    config: Config | None = None,
    reports: int = DETAIL_REPORTS,
) -> list[dict[str, Any]]:
    """One task's record in full, as rows: its fields, its dependencies in
    both directions, what its runs spent, its recent reports, its notebook
    and its handoff.

    A pure reader like the rest of this module — `task.json` (already
    loaded), the task listing (for dependents), `reports.jsonl`, the
    notebook and the handoff file — and fail-soft in the same way: an
    unreadable file costs the rows it held, never the list.

    Every row carries `section` (one of `_DETAIL_SECTIONS`), `kind`
    (`heading`, `field` or `body`), `label`, `text` (the rest of the line
    every surface prints — `detail_line`) and `style` ("" or `warning`),
    plus the raw fields of its kind, so a consumer reads a fact instead of
    parsing a line back out of a rendered string.
    """
    from . import notes as notes_mod
    from .tasks import dependency_state, read_handoff

    home = Path(home)
    if config is None:
        config = load_config_or_default(home)
    rows: list[dict[str, Any]] = []

    def add(section: str, kind: str, label: str, text: str, **fields: Any) -> None:
        rows.append(
            {
                "section": section,
                "kind": kind,
                "label": label,
                "text": text,
                "style": fields.pop("style", ""),
                **fields,
            }
        )

    running = runner_alive(home, task.id)
    # The badges every listing shows, then the words this surface has room
    # for. `task_badges` reads a row, and the two fields it wants are on the
    # task itself.
    state = task.status + task_badges({"perpetual": task.perpetual, "pr_state": task.pr_state})
    if task.attached:
        state += " (attached to a live session)"
    elif running:
        state += " (runner alive)"
    if task.perpetual:
        state += " [perpetual — only you end it]"
    add("record", "heading", "", f"task {task.short_id}  ({task.id})", id=task.id, id_short=task.short_id)
    add("record", "field", "project", task.project, project=task.project)
    add(
        "record",
        "field",
        "status",
        state,
        status=task.status,
        running=running,
        attached=task.attached,
        perpetual=task.perpetual,
    )
    add("record", "field", "harness", task.harness, harness=task.harness)
    add("record", "field", "prompt", task.prompt, prompt=task.prompt)
    add(
        "record",
        "field",
        "workdir",
        task.workdir or "(worktree created on first run)",
        workdir=task.workdir,
    )
    if task.session:
        add("record", "field", "session", task.session, session=task.session)
    if task.issue_url:
        # The full url here, `#62` everywhere a listing has one column: this
        # is the page a human opens.
        add("record", "field", "issue", task.issue_url, issue_url=task.issue_url)
    if task.pr_url:
        add("record", "field", "pr", task.pr_url, pr_url=task.pr_url)
    if task.pr_state:
        # Observed by the manager tick, so it can be older than "now" — say
        # when, rather than implying it was just checked.
        add(
            "record",
            "field",
            "pr state",
            f"{task.pr_state} (observed {task.pr_state_at})",
            pr_state=task.pr_state,
            pr_state_at=task.pr_state_at,
        )
    all_tasks = TaskStore(home).list()
    if task.depends_on:
        deps = dependency_state(task, {t.id: t for t in all_tasks})
        text = ", ".join(short_handle(d) for d in task.depends_on)
        if deps["waiting_on"]:
            text += f"  (waiting on {', '.join(deps['waiting_on'])})"
        if deps["failed"]:
            text += f"  DEP-FAILED: {', '.join(deps['failed'])}"
        if deps["missing"]:
            text += f"  DEP-MISSING: {', '.join(deps['missing'])}"
        if deps["cycle"]:
            text += "  DEP-CYCLE"
        add(
            "record",
            "field",
            "after",
            text,
            depends_on=[short_handle(d) for d in task.depends_on],
            waiting_on=deps["waiting_on"],
            dep_failed=deps["failed"],
            dep_missing=deps["missing"],
            dep_cycle=deps["cycle"],
        )
    # The other direction: who is waiting on this task. This is how a running
    # task learns it should leave a handoff — the preamble tells it to look
    # here.
    dependents = [t.short_id for t in all_tasks if task.id in t.depends_on]
    if dependents:
        add(
            "record",
            "field",
            "dependents",
            f"{', '.join(dependents)}  (leave them a handoff: "
            f"`task report {task.short_id} --status done --handoff <file|->`)",
            dependents=dependents,
        )
    if task.runs:
        last = task.runs[-1]
        add(
            "record",
            "field",
            "runs",
            f"{len(task.runs)} (last: {last.started_at} → {last.ended_at or 'running'}, "
            f"exit {last.exit_code if last.exit_code is not None else '—'})",
            runs=len(task.runs),
            last_started_at=last.started_at,
            last_ended_at=last.ended_at,
            last_exit_code=last.exit_code,
        )
        spent = usage.total(r.usage for r in task.runs)
        if spent_text := usage.describe(spent):
            add(
                "record",
                "field",
                "usage",
                f"{spent_text} (as reported by the harness)",
                usage=spent,
                usage_text=spent_text,
            )
        budget = config.tasks
        for note in usage.run_overages(
            task.runs, budget.max_cost_per_run, budget.max_tokens_per_run
        ):
            add("record", "field", "budget", note, style="warning", budget_overage=note)
        if usage.last_run_overages(
            task.runs, budget.max_cost_per_run, budget.max_tokens_per_run
        ):
            add(
                "record",
                "field",
                "gated",
                "the last run exceeded its budget — `task run` refuses the "
                "next one (--force overrides)",
                style="warning",
                budget_gated=True,
            )
    add("record", "field", "updated", task.updated_at, updated_at=task.updated_at)
    entries = read_reports(home, task.id, limit=reports)
    if entries:
        add("reports", "heading", "", "recent reports:", count=len(entries))
        for r in entries:
            add(
                "reports",
                "body",
                "",
                f"[{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')}",
                at=r.get("at", ""),
                status=r.get("status", ""),
                note=r.get("text", ""),
                pr_url=r.get("pr_url"),
            )
    # The notebook, exactly as the runner renders it into the task's prompt
    # (header line included, so what a reader sees here is what the harness
    # reads). The digest never carries it: the manager reads reports.
    book = notes_mod.task_notebook(home, task.id)
    standing = book.active()
    kept = book.render_notes(standing, unscanned=book.unscanned_bytes())
    if kept:
        add("notebook", "heading", "", "notebook:", count=len(standing))
        for line in kept:
            add("notebook", "body", "", line)
    if not standing:
        # A notebook can render lines and still hold no live note — the file
        # has outgrown its read window — so the hint hangs off the notes, not
        # off the rendering.
        empty = (
            f'(empty — `quorum task remember {task.short_id} "…"` keeps state '
            "between its runs)"
        )
        if kept:
            add("notebook", "body", "", empty, empty=True)
        else:
            add("notebook", "field", "notebook", empty, empty=True)
    handoff = read_handoff(home, task.id)
    if handoff is not None:
        # In full: dependents see it capped in their prompt, and this is
        # where the clip points them.
        add(
            "handoff",
            "heading",
            "",
            "handoff (what this task left for the tasks that depend on it):",
            handoff=handoff,
        )
        for line in handoff.rstrip("\n").splitlines():
            add("handoff", "body", "", line)
    add(
        "more",
        "heading",
        "",
        f"more: `quorum task log {task.short_id}` for the transcript, "
        "`--json` for these rows and the raw record",
    )
    return rows


# -- what a run can read about itself (#94) ---------------------------------
#
# `show self` is the record a person reads plus the run-scoped facts a run
# cannot see from outside itself: what its per-run budget actually is before
# it is refused for exceeding it, how much of its action cap is left, how
# full its notebook is, and whether anything is waiting on it. Every fact is
# already on disk or already in the run's own environment — nothing is
# measured, nothing is recorded, and reading a cap is not a way around it.
#
# Resolving *who* is asking lives in actor.py and the facts arrive here as
# an `actor.SelfRun`, which is what keeps this module a pure file reader.


def _budget_rows(config: Config, task: Task) -> list[tuple[str, str, dict[str, Any]]]:
    """The per-run budget as two facts — the limits and what the last run
    spent against them — rather than as the refusal `task run` raises once
    they are exceeded. `(label, text, fields)` each, for the caller to add.
    """
    budget = config.tasks
    cost = usage.format_cost(budget.max_cost_per_run) if budget.max_cost_per_run > 0 else "off"
    tokens = (
        usage.format_tokens(budget.max_tokens_per_run) if budget.max_tokens_per_run > 0 else "off"
    )
    limits = (
        f"max_cost_per_run {cost}, max_tokens_per_run {tokens}"
        " — `task run` refuses the next run when the last one exceeds either"
        if budget.max_cost_per_run > 0 or budget.max_tokens_per_run > 0
        else "max_cost_per_run off, max_tokens_per_run off — no run is refused for spend"
    )
    rows = [
        (
            "limits",
            limits,
            {
                "max_cost_per_run": budget.max_cost_per_run,
                "max_tokens_per_run": budget.max_tokens_per_run,
            },
        )
    ]
    # What the last run spent, and the caveat that makes the number readable:
    # a run's usage is written when it ends, so the run doing the reading has
    # no figure of its own yet.
    last = task.runs[-1] if task.runs else None
    spent = usage.describe(getattr(last, "usage", None))
    text = f"last run {spent}" if spent else "no run has reported what it spent"
    if last is not None and last.ended_at is None:
        text += "; this run's own spend is recorded when it ends"
    rows.append(
        (
            "spent",
            text,
            {"last_run_usage": getattr(last, "usage", None), "total": usage.total(
                r.usage for r in task.runs
            )},
        )
    )
    return rows


def _notebook_text(size: dict[str, int], remember_cmd: str) -> str:
    """One line of the numbers behind a notebook's rendering, so a run can
    consolidate before the budget starts dropping its oldest notes."""
    text = (
        f"{size['notes']} note(s), {size['bytes']} of {size['max_bytes']} bytes "
        f"and {size['shown']} of {size['max_entries']} entries"
    )
    if size["dropped"]:
        text += (
            f" — {size['dropped']} older note(s) are already being dropped; consolidate "
            f'with one superseding `{remember_cmd} "…"`'
        )
    if size["unscanned"]:
        text += f" — {size['unscanned']} bytes are past the read window and invisible"
    return text


def task_self_detail(
    home: Path, task: Task, run: SelfRun, config: Config | None = None
) -> list[dict[str, Any]]:
    """`task_detail` with the run-scoped facts spliced in after the record's
    own fields: the actor tag, the action cap (a task has none), the per-run
    budget, the notebook's fill, and whether a handoff is owed.

    The same rows in the same shape as the rest of the record, so one
    surface prints both and `--json` dumps both.
    """
    from . import notes as notes_mod
    from .tasks import read_handoff

    home = Path(home)
    if config is None:
        config = load_config_or_default(home)
    rows = task_detail(home, task, config=config)
    extra: list[dict[str, Any]] = []

    def add(kind: str, label: str, text: str, **fields: Any) -> None:
        extra.append(
            {
                "section": "self",
                "kind": kind,
                "label": label,
                "text": text,
                "style": fields.pop("style", ""),
                **fields,
            }
        )

    add("heading", "", "this run:", actor=run.actor)
    add("field", "actor", run.actor, actor=run.actor)
    # A task run carries identity and no cap (actor.py), so the honest answer
    # to "how many actions have I left" is that nothing is counting. Said
    # rather than omitted: a run that cannot find the number otherwise
    # assumes there is one.
    add(
        "field",
        "actions",
        "not capped for a task run — the runner is the rail, and reports.jsonl "
        "plus the transcript are the record of what this run did",
        capped=False,
    )
    for label, text, fields in _budget_rows(config, task):
        add("field", label, text, **fields)
    book = notes_mod.task_notebook(home, task.id)
    size = book.size()
    add(
        "field",
        "notebook",
        _notebook_text(size, f"quorum task remember {task.short_id}"),
        notebook=size,
    )
    # The check the preamble sends a task here for before it reports done:
    # who is waiting, and whether they have been left anything.
    dependents = [t.short_id for t in TaskStore(home).list() if task.id in t.depends_on]
    handoff = read_handoff(home, task.id)
    if not dependents:
        text = "no task depends on this one — no handoff is owed"
    elif handoff is None:
        text = (
            f"{len(dependents)} task(s) depend on this one ({', '.join(dependents)}) and no "
            f"handoff is written — `quorum task report {task.short_id} --status done "
            "--handoff <file>`"
        )
    else:
        text = (
            f"{len(dependents)} task(s) depend on this one ({', '.join(dependents)}); a "
            f"handoff of {len(handoff.encode('utf-8'))} bytes is written"
        )
    add(
        "field",
        "handoff",
        text,
        dependents=dependents,
        handoff_written=handoff is not None,
        style="warning" if dependents and handoff is None else "",
    )
    # Straight after the record's own fields, which is where
    # `_DETAIL_SECTIONS` says the section goes: these are more of what this
    # task is, not a postscript under its reports.
    at = next(i for i, row in enumerate(rows) if row["section"] != "record")
    return rows[:at] + extra + rows[at:]


def agent_detail_rows(
    home: Path, name: str, run: SelfRun | None = None, config: Config | None = None
) -> list[dict[str, Any]] | None:
    """One agent's record as rows, in `task_detail`'s shape — None when no
    agent of that name is configured.

    `run` is the actor tag of the process asking, when it is this agent's
    own run: it adds the `self` section (`quorum agent show self`), which is
    the one place an agent can read how much of its action cap it has left.
    """
    from . import notes as notes_mod

    home = Path(home)
    if config is None:
        config = load_config_or_default(home)
    row = next((r for r in agent_rows(home, config) if r["name"] == name), None)
    if row is None:
        return None
    acfg = config.agents.get(name)
    settings = dict(acfg.settings) if acfg else {}
    rows: list[dict[str, Any]] = []

    def add(section: str, kind: str, label: str, text: str, **fields: Any) -> None:
        rows.append(
            {
                "section": section,
                "kind": kind,
                "label": label,
                "text": text,
                "style": fields.pop("style", ""),
                **fields,
            }
        )

    add("record", "heading", "", f"agent {name}  ({row['type']})", name=name, type=row["type"])
    add("record", "field", "status", row["status"], status=row["status"], error=row["error"])
    if row["error"]:
        add("record", "field", "error", row["error"], style="warning", error=row["error"])
    add(
        "record",
        "field",
        "schedule",
        row["schedule"] if row["enabled"] else f"{row['schedule']} (disabled)",
        schedule=row["schedule"],
        enabled=row["enabled"],
    )
    if row["next_run"]:
        add(
            "record",
            "field",
            "next run",
            row["next_run"] + (" (estimated)" if row["next_run_estimated"] else ""),
            next_run=row["next_run"],
            next_run_estimated=row["next_run_estimated"],
        )
    if row["last_end"]:
        add(
            "record",
            "field",
            "last run",
            f"{row['last_start']} → {row['last_end']}",
            last_start=row["last_start"],
            last_end=row["last_end"],
            duration_ms=row["duration_ms"],
        )
    harness = settings.get("harness") or config.tasks.default_harness
    if harness:
        add("record", "field", "harness", str(harness), harness=str(harness))
    if row["usage_text"]:
        add(
            "record",
            "field",
            "usage",
            f"{row['usage_text']} (as reported by the harness)",
            usage=row["usage"],
        )
    recent = usage.agent_runs(home, name)
    if outcomes := usage.describe_runs(recent):
        add("record", "field", "runs", outcomes, runs=recent)
    if run is not None:
        add("self", "heading", "", "this run:", actor=run.actor)
        add("self", "field", "actor", f"{run.actor} (run {run.run})", actor=run.actor, run=run.run)
        left = max(0, run.cap - run.actions)
        add(
            "self",
            "field",
            "actions",
            f"{run.actions} of {run.cap} used this run, {left} left — a refused action "
            "waits for your next scheduled run",
            actions=run.actions,
            cap=run.cap,
            remaining=left,
            style="warning" if left == 0 else "",
        )
        book = notes_mod.agent_notebook(home, name)
        size = book.size()
        add(
            "self",
            "field",
            "notebook",
            _notebook_text(size, f"quorum {'manager' if name == 'manager' else f'agent {name}'} remember"),
            notebook=size,
        )
    book = notes_mod.agent_notebook(home, name)
    if kept := book.render():
        add("notebook", "heading", "", "notebook:", count=len(book.active()))
        for line in kept[1:]:  # the rendering's own header is this heading
            add("notebook", "body", "", line)
    add(
        "more",
        "heading",
        "",
        f"more: `quorum agent log {name}` for the transcript, "
        "`--json` for these rows and the agent's row",
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


def _at_parses(at: Any) -> bool:
    """Whether a row's `at` is a stamp the list can order by. Everything
    quorum writes goes through `fsio.iso`, so a value that fails here came
    off a torn line, a hand-edited file, or a harness that wrote its own."""
    return fsio.parse_iso_or(str(at)) is not None


def _human_at(at: Any) -> str:
    """The stamp as a surface prints it, with a leading `?` when it does not
    parse. A row quorum cannot place in time is still shown — dropping it
    would lose the event — so the line says the time is not to be trusted
    instead of presenting a position in the list it did not earn."""
    text = fsio.display_ts(at)
    if _at_parses(at):
        return text
    return f"? {text}".rstrip()


def history_line(row: dict[str, Any]) -> str:
    """One history row as the line every surface prints: `[at] text`."""
    at = row.get("at_text") or _human_at(row.get("at", ""))
    return f"[{at}] {row.get('text', '')}"


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
    # One row per message, however many places it is sitting in. `ack()`
    # appends to the archive *before* it unlinks the `cur/` copy, so a
    # consumer that dies between the two leaves the message in both — and the
    # janitor then returns the orphaned claim to `new/`, where it stays,
    # because nothing re-archives an already archived message. Without this
    # the same nudge is listed twice for good. The furthest-along state is
    # the true one: a message in the archive was delivered whatever copy of
    # it is still lying around.
    rank = {"waiting": 0, "claimed": 1, "delivered": 2}
    best: dict[str, tuple[str, Any]] = {}
    for state, m in found:
        seen = best.get(m.id)
        if seen is None or rank[state] > rank[seen[0]]:
            best[m.id] = (state, m)
    rows = []
    for state, m in best.values():
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
    (ISO-8601 UTC as it was written), `at_text` (that stamp as a surface
    prints it, behind a `?` when it does not parse), `kind` (one of
    `_HISTORY_KINDS`) and `text` (the rest of the line every surface prints
    — `history_line`), plus the raw fields of its kind.
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
        started = str(fsio.read_json_or(root / "runner.lock", {}).get("started_at") or "")
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
    # Total over whatever is on disk. A stamp that parses orders by its own
    # string (every writer here uses `fsio.iso`, so string order is time
    # order) then by kind; one that does not parse sorts *after* every real
    # row rather than landing mid-list by string comparison — an empty `at`
    # would otherwise read as the first thing that ever happened to the task,
    # and `not-a-date` as the last. `at_text` carries the `?` that says so.
    order = {kind: i for i, kind in enumerate(_HISTORY_KINDS)}
    for row in rows:
        row["at_text"] = _human_at(row.get("at", ""))
    rows.sort(
        key=lambda r: (
            0 if _at_parses(r.get("at")) else 1,
            str(r.get("at") or ""),
            order.get(r.get("kind"), len(order)),
        )
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
# dropped early by acking it (`quorum board ack`, TUI `a`),
# which archives the message rather than marking it — see
# `MessageBus.ack_board_message`. Each entry therefore carries its id, because
# that is the handle every ack affordance needs.
ATTENTION_WINDOW_DAYS = 7
#: how many of those the *lists* carry — the TUI's `a` picker, which acks a
#: line and so needs one entry per escalation the
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
        # TUI reads, and its `a` picker acks one line at a time — an
        # escalation the picker never renders cannot be acked there at all.
        "attention": attention_summary(home, limit=ATTENTION_LIST_LIMIT),
        "actions": recent_actions(home),
    }
