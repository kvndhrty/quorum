"""The manager: quorum's one built-in agent, and it is *itself* harness-driven.

Supervision policy is not code here. Each tick, the manager compiles a
situation digest — every active task's status, liveness, quiet time, recent
reports and output, plus the manager's own recent action journal and any
directives the user sent — renders it into the user-editable
`prompts/manager.md`, and runs the configured coding harness over it. The
harness then acts with real authority by invoking the quorum CLI:
`task add/run/nudge/cancel`, `agent pause/resume/run-now`, `board post`,
`quorum manager note`. Every mutating action it takes is auto-journaled by
the CLI (tagged with this run's id) and capped per run; the journal is fed
back into the next digest so the manager can see which of its interventions
worked and never loops on one that didn't.

There is deliberately **no deterministic fallback**: without a working
harness the tick raises, crash isolation records the failure, and — because
the manager's config sets `auto_pause = false` — the schedule keeps firing,
so the first tick after the LLM service returns reads the situation from
files and reinvokes whatever needs reinvoking. Dead runners keep the wake
condition true precisely so that recovery is automatic.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .. import actor, ci, fsio, herdr, notes, tasks, transcript, usage
from ..actor import DEFAULT_MAX_ACTIONS_PER_RUN, journal_path
from ..agent import Agent
from ..config import TasksConfig, load_config_or_default
from ..runner import guidance_note
from .harness_run import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    agent_cap,
    run_agent_harness,
    self_observations,
)

__all__ = [
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "Manager",
    "build_digest",
    "journal_path",
    "loop_signal",
    "stall_minutes",
    "overlap_signal",
    "transcript_path",
]

TRANSCRIPT_TAIL_LINES = 10
JOURNAL_TAIL_ENTRIES = 15
RECENT_TERMINAL_HOURS = 24
# Every other input to the digest is a file read; the CI probe is a network
# call per task (see ci.py), and digest build blocks the tick. Cap how many
# one digest may spend, worst case CI_MAX_PROBES * [ci].timeout_seconds. The
# budget is spent in digest order — active tasks, then attached, then
# recently finished — so a home with more tasks than budget still sees its
# live work.
CI_MAX_PROBES = 12

# --- stall observation -------------------------------------------------------
# A live runner whose transcript has not grown for this long is probably hung:
# the process is alive and the lock is fresh, so every other liveness signal
# says "working". Like `possible-loop` this is a plain constant and an
# observation the manager judges — never a rail. (The *rail-shaped* version of
# this is `[tasks].run_stall_timeout_seconds`, the runner's own watchdog, which
# is off by default and kills the run rather than reporting it.)
#
# Tuned to prefer false negatives, and by a wide margin: real harnesses go
# quiet for a long time inside one tool call (a full test suite, a cold build,
# a long provider turn), and a stall flag that fires on healthy work teaches
# the manager to ignore it. 30 minutes is far past any of those and still far
# inside the "cost a whole night" failure this exists to catch.
STALL_QUIET_MINUTES = 30

# --- possible-loop heuristic -------------------------------------------------
# The action journal remembers what the *manager* did; nothing else watches a
# task harness spinning inside a single run (same failing tool call for an
# hour, transcript mtime fresh, runner.lock live — indistinguishable from
# healthy work). These constants tune a repetition read over the transcript
# tail. They are deliberately plain constants, not config: the flag is an
# observation the manager judges, never a rail, so a wrong threshold costs a
# noisy digest line rather than a killed run.
#
# The tradeoff is set to prefer false negatives. The scan is bounded twice —
# LOOP_SCAN_LINES entries, read from at most LOOP_SCAN_BYTES of transcript
# (the byte cap is the binding one on transcripts with big tool payloads, so
# the effective scan depth is data-dependent; it is sized so that even a loop
# of ~60KB entries keeps well over a window of calls in view). Only the last
# LOOP_WINDOW_CALLS *tool calls* are scored, and a flag needs BOTH a call
# repeated LOOP_REPEAT_THRESHOLD times AND that repetition dominating the
# window (distinct/total ratio at or below LOOP_DISTINCT_RATIO). The ratio
# gate is what keeps legitimate patterns quiet — polling that interleaves
# other work, retries with backoff, a handful of repeated reads amid varied
# calls — at the cost of missing a slow loop that only repeats three times
# in the window.
LOOP_SCAN_LINES = 120
LOOP_SCAN_BYTES = 2 * 1024 * 1024
LOOP_WINDOW_CALLS = 12
LOOP_REPEAT_THRESHOLD = 4
LOOP_DISTINCT_RATIO = 0.5

# Tool-call extraction is harness-shape-dependent and lives in `transcript.py`
# — the one place that knows how each harness spells an event, shared with the
# narrative renderer so a harness added there is read here too. It only sees
# structured JSON events: a harness that prints plain text (the shipped
# opencode template, most custom scripts) is unobservable here, and the
# absence of a flag says nothing about it.
LOOP_RECURSION_DEPTH = 8


def transcript_path(home: Path) -> Path:
    return actor.transcript_path(home, "manager")


def _tool_fingerprints(node: object, depth: int = 0) -> list[tuple[str | None, str]]:
    """Best-effort tool calls in one transcript entry: (call id, fingerprint).

    Walks the event structure rather than pattern-matching one harness's
    schema, asking `transcript.tool_call` what each node is; a call's
    fingerprint is `name + sha256(arguments)[:12]`. No recognized arg key
    hashes `null` alone — a coarser signal, but a predictable one (a
    whole-node fallback made any per-call counter or output field render
    every iteration of a genuine loop unique, so it could never fire). The
    call id, when present, lets the caller count a call once even when the
    harness emits paired started/completed events for it.

    The hash keeps argument text (paths, secrets, file contents) off the
    rendered *flag line*; it is not a secrecy boundary — the adjacent `out|`
    tail lines still quote raw events, truncated.

    A matched call is not descended into: its arguments are data, and a
    tool-call-shaped dict inside another call's payload is not a call this
    run made.
    """
    if depth > LOOP_RECURSION_DEPTH:
        return []
    if isinstance(node, list):
        return [fp for item in node for fp in _tool_fingerprints(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    call = transcript.tool_call(node)
    if call is not None:
        payload = json.dumps(call.args, sort_keys=True, default=str, ensure_ascii=False)
        return [(call.id, f"{call.name}:{hashlib.sha256(payload.encode()).hexdigest()[:12]}")]
    out: list[tuple[str | None, str]] = []
    for value in node.values():
        out.extend(_tool_fingerprints(value, depth + 1))
    return out


def loop_signal(entries: list[dict]) -> dict | None:
    """Repetition read over a transcript tail: `None`, or what was observed.

    An *observation*, not a verdict and never a rail — quorum does not halt a
    looping run the way OpenHands' stuck detector does. The manager reads the
    flag and decides. Returns the window it judged so the annotation is
    auditable when these thresholds move.
    """
    calls: list[str] = []
    seen: set[tuple[str, str]] = set()
    for e in entries:
        for call_id, fp in _tool_fingerprints(e.get("event")):
            if call_id is not None:
                # One call, however many events announce it (codex emits
                # item.started AND item.completed with the same id — counting
                # both would halve the effective repeat threshold).
                if (call_id, fp) in seen:
                    continue
                seen.add((call_id, fp))
            calls.append(fp)
    window = calls[-LOOP_WINDOW_CALLS:]
    if len(window) < LOOP_REPEAT_THRESHOLD:
        return None  # too little to say anything: a short tail is not a loop
    fingerprint, repeats = Counter(window).most_common(1)[0]
    distinct = len(set(window))
    if repeats < LOOP_REPEAT_THRESHOLD or distinct / len(window) > LOOP_DISTINCT_RATIO:
        return None
    return {
        # rsplit: the tool name may itself contain a colon (server:read_file);
        # the hex hash never does.
        "tool": fingerprint.rsplit(":", 1)[0],
        "repeats": repeats,
        "window": len(window),
        "distinct": distinct,
    }


# --- overlap observation -----------------------------------------------------
# Two live tasks on one project each see their own worktree and nothing
# else: the digest showed each task's git state in isolation, so a manager
# that launched two branches from the same base found out they collided
# only when both PRs came back MERGE-CONFLICT. `overlap_signal` intersects
# the path sets the worktrees have changed (tasks.worktree_changed_paths —
# read-only git, no network) for every pair of live worktree tasks on the
# same project. Pairs are what the budget counts, because pairs are what
# grows quadratically; the git subprocesses are per *task* (each worktree
# is read once and memoized) and they block the tick. Budget is spent in
# digest order, so a home with more concurrency than budget still sees its
# first OVERLAP_MAX_PAIRS pairs and the rest go unobserved — prefer false
# negatives, like `possible-loop`. The detail line names at
# most OVERLAP_MAX_PATHS shared paths so one wide pair cannot become a wall.
OVERLAP_MAX_PAIRS = 20
OVERLAP_MAX_PATHS = 3


def overlap_signal(live: list[tasks.Task]) -> dict[str, list[dict]]:
    """Which live worktree tasks touch the same files as which others.

    `live` is the caller's already-filtered listing (the digest passes its
    active tasks — nothing terminal, nothing attached).

    Returns {task id: [{"with": short id, "paths": sorted shared paths}, ...]}
    for every task that shares at least one changed path with another live
    task on the same project — both sides of a pair are listed, so both
    digest lines carry the mark. Attached tasks and tasks run without a
    worktree are never compared: that directory is the human's checkout,
    and its diff is theirs, not the task's. Pure over the worktrees (no
    state file, nothing written), fail-soft per task (an unreadable
    worktree contributes no paths and no pair), and bounded by
    OVERLAP_MAX_PAIRS.

    Like every other flag in the digest this is an observation, not a rail:
    parallel edits to one file are sometimes exactly the job, and the
    manager decides whether to serialize, nudge, or ignore.
    """
    by_project: dict[str, list[tasks.Task]] = {}
    for t in live:
        if t.attached or not t.use_worktree or not t.workdir:
            continue
        by_project.setdefault(t.project, []).append(t)
    paths: dict[str, set[str] | None] = {}

    def changed(t: tasks.Task) -> set[str] | None:
        if t.id not in paths:
            paths[t.id] = tasks.worktree_changed_paths(t)
        return paths[t.id]

    result: dict[str, list[dict]] = {}
    budget = OVERLAP_MAX_PAIRS
    for group in by_project.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if budget <= 0:
                    return result
                budget -= 1
                pa, pb = changed(a), changed(b)
                if not pa or not pb:
                    continue
                shared = sorted(pa & pb)
                if not shared:
                    continue
                result.setdefault(a.id, []).append({"with": b.short_id, "paths": shared})
                result.setdefault(b.id, []).append({"with": a.short_id, "paths": shared})
    return result


def _overlap_marks(overlaps: list[dict] | None) -> str:
    """The greppable part: ` overlaps=<short-id> paths=N` per partner,
    appended to the task line. Empty for the overwhelming majority of tasks."""
    if not overlaps:
        return ""
    return "".join(f" overlaps={o['with']} paths={len(o['paths'])}" for o in overlaps)


def _overlap_lines(overlaps: list[dict] | None) -> list[str]:
    """The prose behind the marks: up to OVERLAP_MAX_PATHS shared paths per
    partner, and how many more there are."""
    if not overlaps:
        return []
    lines = []
    for o in overlaps:
        shown = ", ".join(o["paths"][:OVERLAP_MAX_PATHS])
        more = len(o["paths"]) - OVERLAP_MAX_PATHS
        tail = f" (+{more} more)" if more > 0 else ""
        lines.append(
            f"  overlap: with {o['with']} on {shown}{tail} — both branches change these "
            "files; an observation, not a rail"
        )
    return lines


def _usage_lines(task: tasks.Task, budget: TasksConfig) -> list[str]:
    """What a task has spent, and whether any run went over budget.

    Absent entirely when the harness reported nothing — silence is unknown
    spend, never zero. A BUDGET-EXCEEDED line on an *earlier* run is an
    observation of the same class as `possible-loop`. On the *last* run it
    also names the consequence: the runner's budget gate refuses the next
    run (`runner.budget_blockers`), so the manager reads why its relaunch
    failed and what it can do instead — the gate rate-limits, it never
    decides for it.
    """
    lines = []
    spent = usage.total(r.usage for r in task.runs)
    if spent:
        lines.append(
            f"  usage: {usage.describe(spent)} over {int(spent['runs'])} reporting run(s)"
        )
    last = f"run {len(task.runs)}:"
    for note in usage.run_overages(
        task.runs, budget.max_cost_per_run, budget.max_tokens_per_run
    ):
        if note.startswith(last):
            lines.append(f"  BUDGET-EXCEEDED: {note} (next run gated; --force to override)")
        else:
            lines.append(f"  BUDGET-EXCEEDED: {note} (an earlier run; a later one cleared the gate)")
    return lines


def stall_minutes(home: Path, task: tasks.Task, now: datetime) -> int | None:
    """Minutes since this task's transcript last grew — or, when there is no
    transcript at all, minutes since the live run started. None only when
    neither is readable.

    Deliberately the transcript's own mtime rather than `last_activity`: the
    question a stall asks is "has the *harness* said anything", and
    last_activity also counts the runner lock and the reports file, both of
    which a hung run can leave fresh.

    The fallback to the run's own start is the case that matters most: a
    *first* run that hangs before printing anything (the stream-json stdin
    block of #24) has no transcript, and reading that as "no answer" would
    hide the loudest hang there is behind the one thing it cannot produce.
    The lock's `started_at` is when this run began; its mtime is the
    fallback's fallback, for a lock written before the field existed.
    """
    try:
        mtime = tasks.transcript_path(home, task.id).stat().st_mtime
    except OSError:
        started = _run_started_at(home, task)
        if started is None:
            return None
        return int((now - started).total_seconds() // 60)
    return int((now - datetime.fromtimestamp(mtime, UTC)).total_seconds() // 60)


def _run_started_at(home: Path, task: tasks.Task) -> datetime | None:
    """When the live run acquired its lock, from the lock itself."""
    lock = tasks.runner_lock_path(home, task.id)
    try:
        return fsio.parse_iso(str(fsio.read_json(lock)["started_at"]))
    except (OSError, KeyError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(lock.stat().st_mtime, UTC)
    except OSError:
        return None


def _restart_marks(task: tasks.Task) -> str:
    """What has already been done to this task's runs — `stopped=` and
    `fresh_sessions=` counts, and whether the last run ended stalled.

    The manager has no memory between ticks beyond its journal (a bounded
    tail) and its notebook, so "have I restarted this before?" has to be
    readable off the task line. Empty for a task nobody has intervened on.
    """
    marks = ""
    stopped = sum(1 for r in task.runs if r.stopped)
    fresh = sum(1 for r in task.runs if r.fresh_session)
    if stopped:
        marks += f" stopped={stopped}"
    if fresh:
        marks += f" fresh_sessions={fresh}"
    if task.runs and task.runs[-1].stalled:
        marks += " last-run=stalled"
    return marks


def _dependency_marks(state: dict | None) -> str:
    """The greppable part of a dependency observation, appended to the task
    line: `waiting-on=<short ids>` while a prerequisite is unfinished, plus
    the flags. Empty for the overwhelming majority of tasks."""
    if not state:
        return ""
    marks = ""
    if state["waiting_on"]:
        marks += f" waiting-on={','.join(state['waiting_on'])}"
    if state["failed"]:
        marks += " DEP-FAILED"
    if state["missing"]:
        marks += " DEP-MISSING"
    if state["cycle"]:
        marks += " DEP-CYCLE"
    return marks


def _dependency_lines(state: dict | None) -> list[str]:
    """The prose behind the marks. Every one of these is an *observation* the
    manager judges — quorum refuses a premature `task run` (a substrate rail,
    like the attached refusal), but it never cancels, re-queues or rewrites a
    dependency for you."""
    if not state:
        return []
    lines = []
    if state["waiting_on"]:
        lines.append(
            f"  deps: waiting on {', '.join(state['waiting_on'])} — do not `task run` "
            "this task until they reach a terminal status (the runner refuses anyway)"
        )
    if state["failed"]:
        lines.append(
            f"  DEP-FAILED: dependency {', '.join(state['failed'])} ended blocked or "
            "cancelled, so this task will never become runnable on its own — decide "
            "(nudge the dependency, cancel this task, or escalate) and journal it"
        )
    if state["missing"]:
        lines.append(
            f"  DEP-MISSING: dependency {', '.join(state['missing'])} has no task "
            "record — its directory is gone, so it can never reach `done`. Like "
            "DEP-FAILED this does NOT hold the task back (nothing waits on an "
            "upstream that cannot finish): decide whether its premise still "
            "holds, then launch it or cancel it, and journal which"
        )
    if state["cycle"]:
        lines.append(
            "  DEP-CYCLE: this task's dependency chain loops back on itself "
            "(only possible by hand-editing task.json) — it can never start"
        )
    return lines


def _budget(home: Path, tasks_config: TasksConfig | None) -> TasksConfig:
    """The task budget the digest judges spend against; defaults (no budget)
    when the caller has no config and none can be read."""
    if tasks_config is not None:
        return tasks_config
    return load_config_or_default(home).tasks


def build_digest(
    home: Path,
    all_tasks: list[tasks.Task],
    now: datetime,
    directives: list[str],
    tasks_config: TasksConfig | None = None,
    cap: int = DEFAULT_MAX_ACTIONS_PER_RUN,
) -> str:
    """The manager's whole world, compiled from files. Greppable: task lines
    look like `- [status] shortid ...` so both models and tests can parse
    them.

    Almost pure — two fail-soft probes reach outside the files: `herdr` for
    an attached pane's agent state, and `ci` for the pull request behind a
    task's branch. Both degrade to nothing rather than raise, so a digest
    always builds.
    """
    home = Path(home)
    budget = _budget(home, tasks_config)
    # Dependencies, read once over the listing we already hold (tasks.py).
    deps = tasks.dependency_states(all_tasks)
    live = [t for t in all_tasks if t.status not in tasks.TERMINAL_STATUSES]
    active = [t for t in live if not t.attached]
    attached = [t for t in live if t.attached]
    lines = [f"# Situation digest — {fsio.iso(now)}", ""]
    # What supervision itself costs and how its own recent ticks went, read
    # from the manager's own usage ledger: the one part of the situation
    # nothing else in the digest accounts for. Spend is absent when the
    # harness reports nothing, like every other figure; the outcome line
    # survives that, because a run that times out reports nothing at all.
    lines += self_observations(home, "manager", cap) + [""]
    # The notebook comes first, and under its own caps (notes.py): it is the
    # only part of the digest a *previous* you wrote deliberately for this
    # run, and it must not compete for room with per-task output that grows
    # with the number of live tasks.
    lines += notes.digest_section(home, "manager", now=now) + [""]
    # Resolved once: without gh (or with [ci].enabled = false) no task is
    # probed at all.
    ci_budget = CI_MAX_PROBES if ci.available(home) else 0

    def ci_state(task: tasks.Task) -> dict | None:
        nonlocal ci_budget
        if ci_budget <= 0 or ci.probeable(task) is None:
            # A workdir-less (queued) task costs no subprocess, so it must
            # not spend budget either — the budget exists so live and
            # finished work always gets probed.
            return None
        if task.pr_state == "merged":
            # Merge is final: there is nothing left for the forge to tell us
            # about this task, and it would otherwise be probed on every tick
            # for as long as it stays in the recently-finished window. The
            # recorded observation is rendered instead (`pr_state=` on the
            # task's line), so nothing is lost by not asking.
            return None
        ci_budget -= 1
        state = ci.pr_state(home, task)
        if state is not None and (
            task.status in tasks.TERMINAL_STATUSES or state["state"] != "open"
        ):
            # The one place a probe result is written to disk: what the forge
            # says about the PR outlives the probe, so views that never make
            # a network call can still badge a merged task. An observation,
            # never a status change — see tasks.record_pr_state and
            # docs/architecture.md ("The merged observation").
            #
            # `open` is written only for a *finished* task. A live task's
            # task.json is being written concurrently by its own detached
            # runner — the one file race quorum has no lock for — and `open`,
            # the state a PR sits in for the whole of a task's working life,
            # is rendered by no surface, so taking that race for it buys
            # nothing. A merge (or a close) is the opposite: durable, badged
            # everywhere, and a PR can land while its task is still running —
            # and a perpetual task never reaches a terminal status at all, so
            # waiting for one would mean never recording its merge.
            tasks.record_pr_state(home, task, state["state"], now=now)
        return state

    # A perpetual task is never supposed to finish, so one that reported a
    # terminal status is either the user ending it (cancelled — fine) or a
    # harness that ignored its cycle instructions. The digest lists only
    # live tasks, so without this line the failure would be invisible.
    ended = [
        t for t in all_tasks
        if t.perpetual and t.status in tasks.TERMINAL_STATUSES and t.status != "cancelled"
    ]
    for t in ended:
        lines.append(
            f"PERPETUAL-ENDED {t.short_id}: reported {t.status!r} — a perpetual task never "
            "finishes; relaunch it with a nudge about its cycle instructions unless the user "
            "ended it"
        )
    if ended:
        lines.append("")
    lines.append("## Active tasks")
    if not active:
        lines.append("(none)")
    # Read once over the active listing: attached tasks are excluded by
    # construction here and again inside (their checkout is the human's).
    overlaps = overlap_signal(active)
    for t in active:
        alive = tasks.runner_alive(home, t.id)
        seen = tasks.last_activity(home, t.id)
        quiet = f"{int((now - seen).total_seconds() // 60)}m" if seen else "never-ran"
        # Only a *live* runner can be stalled: a dead one is just a task to
        # relaunch, which the manager already handles.
        silent = stall_minutes(home, t, now) if alive else None
        stalled = silent if silent is not None and silent >= STALL_QUIET_MINUTES else None
        lines.append(
            f"- [{t.status}] {t.short_id} project={t.project} harness={t.harness} "
            f"runner={'alive' if alive else 'dead'} runs={len(t.runs)} quiet={quiet}"
            # What the forge last said, off task.json rather than off a
            # probe — the same field the finished section renders. A live
            # task whose PR has landed is not probed again, so without this
            # the merge would be visible for exactly one tick.
            + (f" pr_state={t.pr_state}" if t.pr_state else "")
            # Only when true: an ordinary task's line stays as it was, and
            # the marker reads as the exception it is.
            + (" perpetual=true" if t.perpetual else "")
            # The user's two hands on the queue. `priority=` only when it is
            # not 0, so an ordinary task's line is unchanged and the mark
            # reads as the exception it is; `held=true` always, because a
            # held task looks launchable (runner=dead, status queued) and
            # nothing else on the line says otherwise.
            + (f" priority={t.priority}" if t.priority else "")
            + (" held=true" if t.held else "")
            # Where the work came from, when it came from a forge issue
            # (`task add --issue`): the short form, so you can tell the user
            # "the task for #62 is done". `quorum task show` has the url.
            + (f" issue={ref}" if (ref := tasks.issue_ref(t.issue_url)) else "")
            + _restart_marks(t)
            + (" STALLED" if stalled is not None else "")
            + _dependency_marks(deps.get(t.id))
            + _overlap_marks(overlaps.get(t.id))
        )
        first = t.prompt.strip().splitlines()[0] if t.prompt.strip() else ""
        lines.append(f"  prompt: {first[:120]}")
        if t.held:
            lines.append(
                "  held: the user parked this task with `task hold` — do not launch it "
                "(the runner refuses it anyway) and do not release it; only the user does"
            )
        if stalled is not None:
            lines.append(
                f"  STALLED: runner=alive but the harness has printed nothing for "
                f"{stalled}m — the hang a `quiet=` line alone cannot tell from slow "
                "work. An observation, not a verdict: read the tail, and if nothing "
                f"is happening `task stop {t.short_id}` then relaunch it"
            )
        # The mark stays on the line either way (a relaunched task still had
        # a run stall), but this reads the situation, so it only fires while
        # that stalled run is the current state of the task.
        if not alive and t.runs and t.runs[-1].stalled:
            lines.append(
                "  last run ended stalled: the runner's own stall watchdog "
                "([tasks].run_stall_timeout_seconds) stopped a silent harness, so the "
                "runner is already dead — relaunch it (with --fresh-session if it "
                "keeps happening)"
            )
        lines.extend(_dependency_lines(deps.get(t.id)))
        lines.extend(_overlap_lines(overlaps.get(t.id)))
        git = tasks.workdir_git_state(t)
        if git and (git["dirty"] or git["unpushed"]):
            unpushed = "no-remote" if git["unpushed"] is None else git["unpushed"]
            lines.append(
                f"  git: branch={git['branch']} dirty={git['dirty']} unpushed={unpushed}"
            )
        pr = ci_state(t)
        if pr:
            lines.append(f"  ci: {ci.describe(pr)}")
        lines.extend(_usage_lines(t, budget))
        for r in tasks.read_reports(home, t.id, limit=3):
            lines.append(f"  report [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')[:160]}")
        scan = tasks.read_transcript_tail(
            home, t.id, limit=LOOP_SCAN_LINES, max_bytes=LOOP_SCAN_BYTES
        )
        # Loop evidence must be current: only a live runner (the transcript is
        # append-only, so a dead task would stay flagged forever), and only
        # entries newer than the last *completed* run — after a relaunch, the
        # previous run's spinning must not indict the fresh one.
        # A perpetual task is exempt: cycling over the same few tool calls
        # forever IS its job, so the repetition read has nothing to say about
        # it and the flag would fire every tick, teaching the manager to
        # ignore a signal that still means something on ordinary tasks.
        loop = None
        if alive and not t.perpetual:
            boundary = t.runs[-1].ended_at if t.runs else None
            current = [e for e in scan if not boundary or e.get("at", "") >= boundary]
            loop = loop_signal(current)
        if loop:
            lines.append(
                f"  possible-loop: tool={loop['tool'][:80]} repeated={loop['repeats']}x "
                f"in last {loop['window']} tool calls (distinct={loop['distinct']}) "
                f"— an observation, not a verdict"
            )
        for e in scan[-TRANSCRIPT_TAIL_LINES:]:
            text = e.get("line") if "line" in e else json.dumps(e.get("event"), ensure_ascii=False)
            lines.append(f"  out| {str(text)[:160]}")
    lines.append("")

    if attached:
        lines.append("## Attached sessions (live interactive work — never `task run` these)")
        for t in attached:
            st = tasks.attached_state(home, t.id)
            if st:
                try:
                    age = int((now - fsio.parse_iso(st["at"])).total_seconds() // 60)
                except (KeyError, ValueError):
                    age = None
                event = st.get("event", "adopt")
                label = {"stop": "last-stop", "session-end": "session-ended"}.get(event, "adopted")
                seen = f"{label} {age}m-ago" if age is not None else label
            else:
                seen = "no-signal"
            lines.append(
                f"- [attached] {t.short_id} project={t.project} dir={t.workdir} {seen}"
            )
            if t.herdr_pane:
                state = herdr.agent_state(home, t.herdr_pane)
                if state:
                    lines.append(f"  herdr: state={state}")
            first = t.prompt.strip().splitlines()[0] if t.prompt.strip() else ""
            lines.append(f"  prompt: {first[:120]}")
            git = tasks.workdir_git_state(t)
            if git and (git["dirty"] or git["unpushed"]):
                unpushed = "no-remote" if git["unpushed"] is None else git["unpushed"]
                lines.append(
                    f"  git: branch={git['branch']} dirty={git['dirty']} unpushed={unpushed}"
                )
            pr = ci_state(t)
            if pr:
                lines.append(f"  ci: {ci.describe(pr)}")
            for r in tasks.read_reports(home, t.id, limit=3):
                lines.append(
                    f"  report [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')[:160]}"
                )
        lines.append("")

    recent_terminal = [
        t for t in all_tasks
        if t.status in tasks.TERMINAL_STATUSES
        and (now - fsio.parse_iso(t.updated_at)).total_seconds() < RECENT_TERMINAL_HOURS * 3600
    ]
    if recent_terminal:
        lines.append("## Recently finished (last 24h)")
        for t in recent_terminal:
            line = (
                f"- [{t.status}] {t.short_id} project={t.project}"
                + (f" issue={ref}" if (ref := tasks.issue_ref(t.issue_url)) else "")
                + (f" pr={t.pr_url}" if t.pr_url else "")
                # What the forge last said, off task.json rather than off a
                # probe: a merged task is not re-probed, so this is where
                # "delivered" stays visible for the rest of the window.
                + (f" pr_state={t.pr_state}" if t.pr_state else "")
            )
            git = tasks.workdir_git_state(t)
            if git and (git["dirty"] or git["unpushed"]):
                unpushed = "no-remote" if git["unpushed"] is None else git["unpushed"]
                line += f" STRANDED-WORK dirty={git['dirty']} unpushed={unpushed}"
            lines.append(line)
            # A task that finished over red checks is the STRANDED-WORK story
            # one step later: delivered, but not actually working. Mark it
            # loudly, and leave the judgement to the prompt.
            pr = ci_state(t)
            if pr:
                # A merged PR is delivered, full stop: its checks are
                # historical and its "conflict" is meaningless. Suppressed
                # explicitly rather than by construction, so a forge that
                # keeps reporting a stale red rollup after the merge cannot
                # send the manager to relaunch finished work.
                red = pr["summary"] == "failing" or pr["conflict"]
                bad = "CI-FAILING " if red and not pr["merged"] else ""
                lines.append(f"  ci: {bad}{ci.describe(pr)}")
            lines.extend(_usage_lines(t, budget))
        lines.append("")

    lines.append("## Your recent actions (journal)")
    entries = fsio.read_jsonl_tail(journal_path(home), limit=JOURNAL_TAIL_ENTRIES)
    if not entries:
        lines.append("(none yet)")
    store_by_short = {t.short_id: t for t in all_tasks}
    for e in entries:
        target = e.get("target") or ""
        then = e.get("target_status") or "-"
        now_status = store_by_short[target].status if target in store_by_short else "-"
        if then == "-":
            changed = ""
        elif then == now_status:
            changed = " (UNCHANGED since)"
        else:
            changed = " (changed)"
        line = f"- [{e.get('at', '')}] {e.get('action', '')} target={target or '-'}"
        if e.get("args"):
            line += f" args={str(e['args'])[:100]}"
        if target:
            line += f" status_then={then} status_now={now_status}{changed}"
        lines.append(line)
    lines.append("")

    lines.append("## Directives from the user")
    if directives:
        lines.extend(f"- {d}" for d in directives)
    else:
        lines.append("(none)")
    lines.append("")
    return "\n".join(lines)


class Manager(Agent):
    default_schedule = "every 5m"

    def tick(self) -> None:
        home = self.ctx.home
        all_tasks = tasks.TaskStore(home).list()
        active = any(t.status not in tasks.TERMINAL_STATUSES for t in all_tasks)
        claimed = list(self.ctx.bus.claim("manager"))
        if not active and not claimed:
            return  # nothing to manage; don't spend a harness run on an idle home

        directives = [guidance_note(c.message) for c in claimed]
        try:
            digest = build_digest(
                home,
                all_tasks,
                self.ctx.now(),
                directives,
                self.ctx.config.tasks if self.ctx.config else None,
                cap=agent_cap(self.ctx),
            )
            prompt = self.ctx.prompt("manager", digest=digest)
            # the digest, not the whole prompt, is what a later reader needs:
            # the constitution above it is the same every tick and on disk
            run_agent_harness(self.ctx, prompt, snapshot=digest)
        except BaseException:
            for c in claimed:
                c.reject()  # directives go straight back to new/ for the next tick
            raise
        for c in claimed:
            c.ack()
        self.ctx.log_action("manager.run", "manager run complete")
