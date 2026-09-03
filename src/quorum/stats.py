"""Usage and delivery statistics across tasks, harnesses, weeks and agents.

`quorum status` says what one task or one agent has spent. The questions a
person asks after the fact are aggregate: what did this project cost this
week, is one harness cheaper than another per merged PR, how long from
queue to merge, how many tasks needed a rerun. Before this module those
were answered with a hand-written script over `task.json` files; this is
that script, shipped as `quorum usage` (#96, theme #88).

It is a **pure reader** in the views' mold: it opens `tasks/<id>/task.json`,
`tasks/<id>/reports.jsonl` (for the instant a task said `done` — the one
timestamp `task.json` does not hold, since `updated_at` moves on every
edit) and the agent ledgers (`state/manager/usage.jsonl`,
`state/agents/<name>/usage.jsonl`), and nothing else. No network, no cache,
no state of its own: every number is recomputed on every call, and the
command works with the supervisor stopped.

The numbers are what the harness reported and what the manager observed.
Three rules keep them honest:

- **Reuse the reduction, never re-derive it.** Spend is `usage.total` over
  the runs in a group: max within a run (already applied when the run was
  recorded), sum across runs. A task whose runs reported nothing is
  *counted* in `tasks` and `runs` and contributes nothing to `cost` or
  `tokens`; the row says how many tasks reported (`tasks_with_usage`) so a
  total over three reporting tasks out of ten is never read as the cost of
  ten. A harness that reports tokens but no cost gets a token figure and an
  empty cost cell. Nothing is estimated.
- **Delivery figures come only from recorded observations.** `merged` is
  `pr_state == "merged"` on the record (#79), and its share is measured over
  the tasks whose PR the manager *observed at all* (`pr_state` set to any
  state), never over every done task: a home with no `gh`, `[ci]` off or a
  supervisor that was never up while the PR was open has no observation,
  and absence must not read as "not merged". `done_to_merged` runs from the
  `done` report to `pr_state_at` — the manager tick that first saw the
  merge, so it is late by up to one tick and never early; a merge seen
  before the harness said done reads as zero, because the answer to "how
  long after done did it merge" is then "it already had".
- **A task belongs to the moment it was queued.** `--since` and the `week`
  dimension both read `created_at`, so a task is in exactly one week and a
  window is a set of tasks, not a set of runs sliced mid-task. Agent runs
  have no such anchor and filter on the ledger line's own `at`.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import actor, fsio, usage
from .tasks import PR_STATES, Task, TaskStore, read_reports

# The `--by` dimensions. The first three group tasks; `agent` groups ledger
# lines, and its rows carry run outcomes instead of delivery figures.
DIMENSIONS = ("project", "harness", "week", "agent")
TASK_DIMENSIONS = ("project", "harness", "week")

_SINCE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_since(text: str) -> timedelta:
    """`7d` / `36h` / `2w` / `90m` → a timedelta. ValueError for anything else
    (including `0d`: an empty window is a typo, not a request)."""
    m = _SINCE.match(text or "")
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"--since wants a positive count with a unit, e.g. 7d, 36h, 2w: {text!r}")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def _dt(value: Any) -> datetime | None:
    """An aware datetime off a stored timestamp, or None — a hand-edited
    field says nothing rather than failing the report."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return fsio.parse_iso(value)
    except ValueError:
        return None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def week_of(moment: datetime | None) -> str:
    """The ISO week a timestamp falls in, `2026-W36`; `unknown` when the
    record carries no readable timestamp."""
    return moment.strftime("%G-W%V") if moment is not None else "unknown"


def done_at(home: Path, task: Task) -> datetime | None:
    """When the task last reported `done`, off its own reports.jsonl.

    Only a task whose *current* status is `done` has one: a task relaunched
    after a premature `done` is not delivered, however often it said so.
    None when the status was hand-edited (no `done` report exists) — the
    task still counts as done, it just has no queue-to-done figure.
    """
    if task.status != "done":
        return None
    for entry in reversed(read_reports(home, task.id)):
        if isinstance(entry, dict) and entry.get("status") == "done":
            return _dt(entry.get("at"))
    return None


def task_facts(home: Path, task: Task) -> dict[str, Any]:
    """Everything the report reads off one task, computed once.

    `run_usages` keeps the per-run records rather than a task total, so a
    group's spend is one `usage.total` over every run in it — the reduction
    the rest of quorum uses, and the only place a run count comes from.
    """
    created = _dt(task.created_at)
    starts = [s for r in task.runs if (s := _dt(r.started_at)) is not None]
    first_run = min(starts) if starts else None
    finished = done_at(home, task)
    merged_at = _dt(task.pr_state_at) if task.pr_state == "merged" else None
    to_merge = _seconds(finished, merged_at)
    return {
        "id": task.id,
        "project": task.project,
        "harness": task.harness,
        "week": week_of(created),
        "created_at": created,
        "runs": len(task.runs),
        "run_usages": [r.usage for r in task.runs],
        "done": task.status == "done",
        "observed": task.pr_state in PR_STATES,
        "merged": task.pr_state == "merged",
        "queue_to_run": _seconds(created, first_run),
        "queue_to_done": _seconds(created, finished),
        # A merge the manager saw before the harness said done is a zero
        # wait, not a negative one.
        "done_to_merged": None if to_merge is None else max(0.0, to_merge),
    }


def _summary(values: Iterable[float | None]) -> dict[str, float] | None:
    """`{n, median_seconds, mean_seconds}` over the figures that exist,
    None when none does — an empty cell, never a zero."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return {
        "n": len(known),
        "median_seconds": float(statistics.median(known)),
        "mean_seconds": float(statistics.fmean(known)),
    }


def _task_row(key: str, group: Sequence[dict[str, Any]]) -> dict[str, Any]:
    spent = usage.total(u for f in group for u in f["run_usages"])
    observed = sum(1 for f in group if f["observed"])
    merged = sum(1 for f in group if f["merged"])
    return {
        "key": key,
        "tasks": len(group),
        "tasks_with_usage": sum(1 for f in group if usage.total(f["run_usages"]) is not None),
        "runs": sum(f["runs"] for f in group),
        # Every run after a task's first: what a relaunch or a resume cost in
        # attempts. An attached task has no runs and so no reruns.
        "reruns": sum(max(0, f["runs"] - 1) for f in group),
        "usage": spent,
        "done": sum(1 for f in group if f["done"]),
        "observed": observed,
        "merged": merged,
        "share_merged": (merged / observed) if observed else None,
        "queue_to_run": _summary(f["queue_to_run"] for f in group),
        "queue_to_done": _summary(f["queue_to_done"] for f in group),
        "done_to_merged": _summary(f["done_to_merged"] for f in group),
    }


def task_rows(
    facts: Sequence[dict[str, Any]], by: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One row per distinct `by` value, sorted by it, plus a `total` row over
    every fact (None when there is nothing to total)."""
    if by not in TASK_DIMENSIONS:
        raise ValueError(f"--by wants one of {', '.join(DIMENSIONS)}: {by!r}")
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        groups.setdefault(str(f[by]), []).append(f)
    rows = [_task_row(key, groups[key]) for key in sorted(groups)]
    return rows, (_task_row("total", list(facts)) if facts else None)


def agent_ledgers(home: Path) -> dict[str, Path]:
    """Every agent ledger on disk, by agent name — the manager first, then
    `state/agents/<name>/usage.jsonl` for each name that has one. Read off
    the filesystem rather than the config, so an agent removed from
    config.toml still accounts for what it spent."""
    home = Path(home)
    out: dict[str, Path] = {}
    manager = actor.usage_path(home, "manager")
    if manager.is_file():
        out["manager"] = manager
    root = home / "state" / "agents"
    if root.is_dir():
        for entry in sorted(p for p in root.iterdir() if p.is_dir() and not fsio.is_tmp(p.name)):
            ledger = actor.usage_path(home, entry.name)
            if entry.name != "manager" and ledger.is_file():
                out[entry.name] = ledger
    return out


def _agent_row(key: str, entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    spent = usage.total(e.get("usage") for e in entries)
    outcomes = dict.fromkeys((*usage.RUN_OUTCOMES, "unknown"), 0)
    for e in entries:
        outcome = e.get("outcome")
        outcomes[outcome if outcome in usage.RUN_OUTCOMES else "unknown"] += 1
    return {
        "key": key,
        "runs": len(entries),
        "runs_with_usage": int(spent["runs"]) if spent else 0,
        "outcomes": outcomes,
        "usage": spent,
        "duration": _summary(usage.number(e.get("duration_seconds")) for e in entries),
    }


def agent_rows(
    home: Path, cutoff: datetime | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One row per ledger, the whole file read (this is a report, not a
    tick, so the bounded tail `views` takes would under-count on purpose);
    `cutoff` keeps the lines whose `at` is on or after it."""
    ledgers = agent_ledgers(home)
    rows = []
    everything: list[dict[str, Any]] = []
    for name, path in ledgers.items():
        entries = [
            e
            for e in fsio.read_jsonl(path)
            if isinstance(e, dict)
            and (cutoff is None or ((at := _dt(e.get("at"))) is not None and at >= cutoff))
        ]
        if not entries:
            continue
        rows.append(_agent_row(name, entries))
        everything.extend(entries)
    return rows, (_agent_row("total", everything) if everything else None)


def report(
    home: Path, by: str = "project", since: timedelta | None = None, now: Any = None
) -> dict[str, Any]:
    """The whole `quorum usage` payload, JSON-ready.

    `{"by", "since_seconds", "cutoff", "rows", "total"}` — `rows` per
    distinct value of the dimension, `total` over all of them (None when
    there is nothing). Task rows and agent rows have different keys
    (delivery figures against run outcomes); readers branch on `by`.
    """
    if by not in DIMENSIONS:
        raise ValueError(f"--by wants one of {', '.join(DIMENSIONS)}: {by!r}")
    home = Path(home)
    moment = now() if callable(now) else (now or fsio.utc_now())
    cutoff = moment - since if since is not None else None
    if by == "agent":
        rows, total = agent_rows(home, cutoff)
    else:
        facts = [task_facts(home, t) for t in TaskStore(home).list()]
        if cutoff is not None:
            facts = [f for f in facts if f["created_at"] is not None and f["created_at"] >= cutoff]
        rows, total = task_rows(facts, by)
    return {
        "by": by,
        "since_seconds": since.total_seconds() if since is not None else None,
        "cutoff": fsio.iso(cutoff) if cutoff is not None else None,
        "rows": rows,
        "total": total,
    }


def format_span(seconds: float) -> str:
    """A delivery interval at the granularity a person judges it by: `4m`,
    `3h12m`, `2d04h`."""
    whole = int(seconds)
    if whole >= 86400:
        return f"{whole // 86400}d{(whole % 86400) // 3600:02d}h"
    if whole >= 3600:
        return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"
    return f"{whole // 60}m"


def describe_summary(summary: dict[str, float] | None) -> str:
    """The median of a `_summary`, `""` when there is none."""
    if not summary:
        return ""
    return format_span(summary["median_seconds"])
