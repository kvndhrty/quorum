"""What a run spent: token/cost usage read back out of harness result events.

Harnesses already say what a run cost — claude's terminal `result` event
carries `total_cost_usd` plus a `usage` block, codex's `turn.completed` and
`token_count` carry token counts. The runner streams those events to
`transcript.jsonl` anyway, so capturing usage costs one more look at each
parsed event; the result lands on the run's entry in `task.json` and is
surfaced by views, `quorum status` and the manager digest.

An *agent's* harness runs (the manager's tick, any prompt agent) have no
task record to hang a number on, so they get a ledger instead: one line per
run in `state/manager/usage.jsonl` / `state/agents/<name>/usage.jsonl`, read
back over a bounded tail by `agent_usage`. That is how the cost of
supervision itself becomes visible — to the views, and to the manager. The
same line also carries how the run *went* (`outcome`, `duration_seconds`,
read back by `agent_runs`): a timed-out run reports no usage at all, so
without that the runs an agent most needs to see about itself would be the
invisible ones.

Three properties this module is built around:

- **Fail-soft.** A harness that reports nothing (the shipped opencode
  template, most custom scripts, anything printing plain text) is fully
  supported: `usage` is simply absent everywhere, and no reader may assume
  it is there. Nothing here raises on a malformed event.
- **Loose over the wire, canonical on disk.** Extraction accepts the key
  spellings the field actually uses (`input_tokens` / `inputTokens` /
  `prompt_tokens`, ...) and normalizes to one small set of keys, so readers
  never branch on which harness ran.
- **Prefer under-counting.** Within one run, values are reduced
  *elementwise max*, not summed: the harnesses that report usage report
  run-cumulative totals (verified against real claude transcripts, where a
  run's single `result` event holds the whole run's tokens), and a
  multi-turn pumped run emits one such event per turn. Max equals "the last
  event that reported anything" for a cumulative reporter, and merely
  under-counts a per-turn one — the same false-negative-preferring tradeoff
  as the `possible-loop` signal, and the honest direction for a number a
  budget may later be judged against. Across *runs* (a task total) the
  reduction is a sum, because runs really are separate spends.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from . import actor, fsio

# The event shapes that report what a run spent. Matched against an event's
# "type"/"item_type"; an event carrying a top-level cost key counts too, so a
# harness quorum has never seen still gets its cost recorded.
RESULT_EVENT_TYPES = frozenset(
    {"result", "turn.completed", "turn_completed", "run.completed", "token_count", "usage"}
)

# canonical key -> the source spellings seen in the wild, in priority order
TOKEN_FIELDS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens"),
    "cache_read_tokens": (
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cached_input_tokens",
    ),
    "cache_creation_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens"),
}
COST_KEYS = ("total_cost_usd", "cost_usd", "costUSD", "totalCostUsd")
TOTAL_TOKEN_KEYS = ("total_tokens", "totalTokens")

# Every numeric key a usage dict may hold. Readers iterate this rather than
# the dict, so a stray key can never leak into a sum.
NUMERIC_FIELDS = ("cost_usd", *TOKEN_FIELDS, "total_tokens", "events")


def _number(value: Any) -> float | None:
    """A finite, non-negative number, or None. bools are not numbers here.

    Int-ness is preserved: token counts stay integers through task.json, so
    a record stays readable to a human with `jq`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


# The same check, for readers outside this module (`stats.py`, the CLI's
# usage cells) that re-read a figure off disk and must degrade the same way.
number = _number


def _token_source(event: dict) -> dict:
    """The sub-dict of a result event that holds its token counts.

    `usage` for claude and codex's `turn.completed`; codex's `token_count`
    nests the cumulative counts under `info.total_token_usage`. A flat event
    (some harnesses put counts at the top level) is its own source. Only one
    level is consulted — claude's `usage` also holds a per-message
    `iterations` list, and descending into it would double-count.
    """
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    info = event.get("info")
    if isinstance(info, dict):
        total = info.get("total_token_usage")
        return total if isinstance(total, dict) else info
    return event


def usage_from_event(event: object) -> dict[str, float] | None:
    """Canonical usage reported by one transcript event, or None.

    None means "this event says nothing about spend" — the overwhelmingly
    common case, and never an error.
    """
    if not isinstance(event, dict):
        return None
    cost = next((c for k in COST_KEYS if (c := _number(event.get(k))) is not None), None)
    kinds = {str(event.get(k)) for k in ("type", "item_type") if event.get(k) is not None}
    if cost is None and not (kinds & RESULT_EVENT_TYPES):
        return None

    out: dict[str, float] = {}
    if cost is not None:
        out["cost_usd"] = cost
    source = _token_source(event)
    matched: dict[str, str] = {}
    for canonical, keys in TOKEN_FIELDS.items():
        for key in keys:
            value = _number(source.get(key))
            if value is not None:
                out[canonical] = value
                matched[canonical] = key
                break
    reported_total = next(
        (n for k in TOTAL_TOKEN_KEYS if (n := _number(source.get(k))) is not None), None
    )
    if reported_total is not None:
        out["total_tokens"] = reported_total
    else:
        # Everything the model processed, cache reads included: that is the
        # work done, and the only total a harness-agnostic budget can mean.
        # Except codex's `cached_input_tokens`, which is a *subset* of its
        # `input_tokens` (claude's cache fields are disjoint from input, this
        # one is not — its own `token_count` totals exclude it): summing it
        # in would over-count, and this number prefers under.
        tokens = [
            out[k]
            for k in TOKEN_FIELDS
            if k in out
            and not (k == "cache_read_tokens" and matched[k] == "cached_input_tokens")
        ]
        if tokens:
            out["total_tokens"] = sum(tokens)
    return out or None


def _reduce(a: dict[str, float], b: dict[str, float], op) -> dict[str, float]:
    # Values are re-checked through _number: `total` reduces usage dicts read
    # back from task.json, and a hand-edited or corrupted file must degrade
    # to "that value says nothing", never raise out of a view.
    out = dict(a)
    for key in NUMERIC_FIELDS:
        vb = _number(b.get(key))
        if vb is None:
            continue
        va = _number(out.get(key))
        out[key] = op(va, vb) if va is not None else vb
    return out


class UsageCollector:
    """Accumulates a single run's usage as its events stream past.

    Fed from the runner's `on_event` hook, so capture costs nothing beyond
    the parse the transcript writer already did.
    """

    def __init__(self) -> None:
        self._usage: dict[str, float] = {}
        self.events = 0

    def add(self, event: object) -> None:
        reported = usage_from_event(event)
        if reported is None:
            return
        self.events += 1
        self._usage = _reduce(self._usage, reported, max)

    def result(self) -> dict[str, float] | None:
        """The run's usage record, or None when the harness reported none."""
        if not self.events:
            return None
        return {**self._usage, "events": self.events}


def total(usages: Iterable[dict[str, float] | None]) -> dict[str, float] | None:
    """Sum per-run usages into a task total; None when none reported."""
    out: dict[str, float] = {}
    runs = 0
    for usage in usages:
        if not usage or not isinstance(usage, dict):
            continue
        runs += 1
        out = _reduce(out, usage, lambda x, y: x + y)
    if not runs:
        return None
    out.pop("events", None)  # per-event multiplicity says nothing at task level
    return {**out, "runs": runs}


# How many recorded agent runs a cumulative figure looks back over. An
# agent's ledger is append-only and unbounded (the manager ticks every 5
# minutes forever), so its "total" is honestly a *recent* total — the window
# is reported alongside the number rather than implied.
AGENT_USAGE_TAIL = 200


# How many recent runs a self-observation line reports on. Small on purpose:
# what an agent needs is the *shape* of its last few ticks (are they finishing
# at all?), not a log — and the line shares a digest header with its spend.
RECENT_RUNS = 5

# What a recorded run ended as. A ledger line written before #59 has none, and
# `None` (rendered `?`) is the honest reading of it — never `ok`.
RUN_OUTCOMES = ("ok", "raised", "timeout")


def record_agent_run(
    home: Any,
    name: str,
    run_id: str,
    usage: dict[str, float] | None,
    now: Any = None,
    outcome: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Append one agent harness run to `state/.../usage.jsonl`.

    Written for every run, including the ones that reported nothing (`usage:
    null`) and the ones that failed — a timed-out run still spent what it
    spent, and a run count only means something if every run is in it. Never
    raises: a spend ledger must not be able to fail a tick.

    `outcome` (`ok`/`raised`/`timeout`) and `duration_seconds` make the same
    line the record of *how the run went*, not only what it cost — the one
    thing an agent could never see about its own recent history. They ride
    the existing line deliberately: a run that times out reports no usage at
    all, so the outcome of the runs that matter most would need a second file
    to live anywhere else.
    """
    entry: dict[str, Any] = {
        "at": fsio.iso(now or fsio.utc_now()),
        "run": run_id,
        "usage": usage,
    }
    if outcome is not None:
        entry["outcome"] = outcome
    if duration_seconds is not None:
        entry["duration_seconds"] = round(float(duration_seconds), 3)
    try:
        fsio.append_jsonl(actor.usage_path(home, name), entry)
    except OSError:
        pass


def agent_runs(home: Any, name: str, limit: int = RECENT_RUNS) -> list[dict[str, Any]]:
    """The agent's `limit` most recent ledger lines, newest last.

    Read off the same bounded tail as `agent_usage` — and separately from it,
    because a run that timed out or died reported no usage, and those are
    exactly the runs an outcome line exists to show. Each entry is
    `{"run", "at", "outcome", "duration_seconds", "usage"}`, with `outcome`
    and `duration_seconds` `None` for a line written before they existed (or
    hand-edited into nonsense). Never raises.
    """
    entries = fsio.read_jsonl_tail(actor.usage_path(home, name), limit=AGENT_USAGE_TAIL)
    out = []
    # `entries[-0:]` is the whole list, so ask for nothing explicitly
    for e in entries[-limit:] if limit > 0 else []:
        if not isinstance(e, dict):
            continue  # a torn or hand-edited line is silence, like everywhere else
        outcome = e.get("outcome")
        out.append(
            {
                "run": e.get("run") or "",
                "at": e.get("at") or "",
                "outcome": outcome if outcome in RUN_OUTCOMES else None,
                "duration_seconds": _number(e.get("duration_seconds")),
                "usage": e.get("usage") if isinstance(e.get("usage"), dict) else None,
            }
        )
    return out


def agent_usage(home: Any, name: str, limit: int = AGENT_USAGE_TAIL) -> dict[str, Any] | None:
    """What an agent's recent runs spent, or None when the ledger says nothing.

    `{"last": <the newest run that reported anything>, "total": <sum over the
    window>, "runs": <runs in the window that reported>, "window": <runs
    read>}`. None means no run in the window reported usage — the ordinary
    case for a harness that says nothing about spend, and never zero.
    """
    entries = fsio.read_jsonl_tail(actor.usage_path(home, name), limit=limit)
    # A hand-edited or truncated line may parse to anything; only dicts with
    # a dict `usage` count, everything else is silence (never a raise out of
    # status / the web / the digest).
    reported = [
        e["usage"] for e in entries if isinstance(e, dict) and isinstance(e.get("usage"), dict)
    ]
    if not reported:
        return None
    spent = total(reported)
    if spent is None:
        return None
    return {
        "last": reported[-1],
        "total": spent,
        "runs": int(spent["runs"]),
        "window": len(entries),
        # True when the ledger is longer than the tail read: the total is a
        # recent-runs figure, not all-time, and readers must say so.
        "truncated": len(entries) >= limit,
    }


def describe_agent(spent: dict[str, Any] | None) -> str:
    """One compact line for an agent row: last run, then the window total."""
    if not spent or not isinstance(spent, dict):
        return ""
    parts = []
    last = describe(spent.get("last"))
    if last:
        parts.append(f"last {last}")
    window = describe(spent.get("total"))
    if window and int(spent.get("runs") or 0) > 1:
        label = "recent runs" if spent.get("truncated") else "runs"
        parts.append(f"{window} over {int(spent['runs'])} {label}")
    return " · ".join(parts)


def format_tokens(count: float) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(int(count))


def format_duration(seconds: float) -> str:
    """A run's wall time, at the granularity a human judges a tick by."""
    total_seconds = int(seconds)
    if total_seconds >= 3600:
        return f"{total_seconds // 3600}h{(total_seconds % 3600) // 60:02d}m"
    return f"{total_seconds // 60}m{total_seconds % 60:02d}s"


def describe_runs(runs: Iterable[dict[str, Any]]) -> str:
    """`ok 2m10s · ok 1m48s · TIMEOUT 15m00s · ? · ok 0m52s`, oldest first.

    Anything but `ok` is upper-cased: the point of the line is that a run of
    TIMEOUTs is legible at a glance. "" when there is nothing to say.
    """
    parts = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        outcome = run.get("outcome")
        label = "?" if outcome not in RUN_OUTCOMES else ("ok" if outcome == "ok" else outcome.upper())
        duration = _number(run.get("duration_seconds"))
        parts.append(f"{label} {format_duration(duration)}" if duration is not None else label)
    return " · ".join(parts)


def format_cost(cost: float) -> str:
    return f"${cost:.4f}" if 0 < cost < 0.01 else f"${cost:.2f}"


def describe(usage: dict[str, float] | None) -> str:
    """One compact line for a status row or digest, "" when nothing to say.

    Values come back off disk here, so each is re-checked through _number —
    and a zero is omitted rather than shown as $0.00: it says nothing.
    """
    if not usage or not isinstance(usage, dict):
        return ""
    parts = []
    cost = _number(usage.get("cost_usd"))
    if cost:
        parts.append(format_cost(cost))
    tokens = _number(usage.get("total_tokens"))
    if tokens:
        parts.append(f"{format_tokens(tokens)} tok")
    return " · ".join(parts)


def overages(
    usage: dict[str, float] | None, max_cost: float = 0.0, max_tokens: int = 0
) -> list[str]:
    """How one run's usage exceeded the configured budget, if it did.

    A budget in the rate-limit family: this reports, it never vetoes. Both
    limits are off at 0, and a run that reported nothing can never be over
    budget — silence is not evidence of spend.
    """
    if not usage or not isinstance(usage, dict):
        return []
    out = []
    cost = _number(usage.get("cost_usd"))
    if max_cost > 0 and cost is not None and cost > max_cost:
        out.append(f"cost {format_cost(cost)} > max_cost_per_run {format_cost(max_cost)}")
    tokens = _number(usage.get("total_tokens"))
    if max_tokens > 0 and tokens is not None and tokens > max_tokens:
        out.append(
            f"tokens {format_tokens(tokens)} > max_tokens_per_run {format_tokens(max_tokens)}"
        )
    return out


def run_overages(runs: Iterable[Any], max_cost: float = 0.0, max_tokens: int = 0) -> list[str]:
    """Budget notes for every run of a task that went over, newest last."""
    out = []
    for i, run in enumerate(runs, start=1):
        for note in overages(getattr(run, "usage", None), max_cost, max_tokens):
            out.append(f"run {i}: {note}")
    return out


def last_run_overages(
    runs: Sequence[Any], max_cost: float = 0.0, max_tokens: int = 0
) -> list[str]:
    """How the task's *last* run exceeded the budget — the condition the
    runner's budget gate reads (`runner.budget_blockers`).

    Only the last run counts: the gate is a rate limit on relaunching a task
    that just blew its budget, not a sentence for a task that once did. So a
    later run that came in under budget — or reported nothing, since silence
    is not evidence of spend — clears it, and a task with no runs is never
    gated. Both limits off (0) means an empty list for any history.
    """
    if not runs:
        return []
    return overages(getattr(runs[-1], "usage", None), max_cost, max_tokens)
