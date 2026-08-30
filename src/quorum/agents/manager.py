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
from datetime import datetime
from pathlib import Path

from .. import actor, fsio, herdr, tasks
from ..actor import journal_path
from ..agent import Agent
from ..runner import guidance_note
from .harness_run import DEFAULT_RUN_TIMEOUT_SECONDS, run_agent_harness

__all__ = [
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "Manager",
    "build_digest",
    "journal_path",
    "loop_signal",
    "transcript_path",
]

TRANSCRIPT_TAIL_LINES = 10
JOURNAL_TAIL_ENTRIES = 15
RECENT_TERMINAL_HOURS = 24

# --- possible-loop heuristic -------------------------------------------------
# The action journal remembers what the *manager* did; nothing else watches a
# task harness spinning inside a single run (same failing tool call for an
# hour, transcript mtime fresh, runner.lock live — indistinguishable from
# healthy work). These constants tune a repetition read over the transcript
# tail. They are deliberately plain constants, not config: the flag is an
# observation the manager judges, never a rail, so a wrong threshold costs a
# noisy digest line rather than a killed run.
#
# The tradeoff is set to prefer false negatives. LOOP_SCAN_LINES bounds the
# read (the digest stays cheap); only the last LOOP_WINDOW_CALLS *tool calls*
# are scored, and a flag needs BOTH a call repeated LOOP_REPEAT_THRESHOLD
# times AND that repetition dominating the window (distinct/total ratio at or
# below LOOP_DISTINCT_RATIO). The ratio gate is what keeps legitimate
# patterns quiet — polling that interleaves other work, retries with backoff,
# a handful of repeated reads amid varied calls — at the cost of missing a
# slow loop that only repeats three times in the window.
LOOP_SCAN_LINES = 120
LOOP_WINDOW_CALLS = 12
LOOP_REPEAT_THRESHOLD = 4
LOOP_DISTINCT_RATIO = 0.5

# Tool-call extraction is harness-shape-dependent, so it is loose by design:
# any nested dict tagged with one of these kinds counts as a call, whatever
# harness emitted it (claude `tool_use`, codex `command_execution`, ...).
TOOL_CALL_KINDS = frozenset(
    {"tool_use", "tool_call", "function_call", "command_execution", "local_shell_call"}
)
TOOL_NAME_KEYS = ("name", "tool_name", "tool", "item_type", "type")
TOOL_ARG_KEYS = ("input", "arguments", "args", "parameters", "command", "cmd")
# Per-call identifiers and timings differ every call; including them would
# make every call look unique and the detector would never fire.
VOLATILE_KEYS = frozenset(
    {"id", "at", "call_id", "tool_use_id", "timestamp", "time", "duration", "duration_ms"}
)
LOOP_RECURSION_DEPTH = 8


def transcript_path(home: Path) -> Path:
    return actor.transcript_path(home, "manager")


def _tool_fingerprints(node: object, depth: int = 0) -> list[str]:
    """Best-effort tool calls in one transcript entry, as stable fingerprints.

    Walks the event structure rather than pattern-matching one harness's
    schema, and fingerprints `name + hash(arguments)`. The hash matters twice:
    it makes "the same call" comparable across harnesses, and it keeps raw
    argument text (paths, secrets, whole file contents) out of the digest —
    the rendered flag carries a count and a tool name, never a payload.
    """
    if depth > LOOP_RECURSION_DEPTH:
        return []
    if isinstance(node, list):
        return [fp for item in node for fp in _tool_fingerprints(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    kinds = {str(node.get(k)) for k in ("type", "item_type") if node.get(k) is not None}
    if kinds & TOOL_CALL_KINDS or "tool_name" in node:
        name = next(
            (str(node[k]) for k in TOOL_NAME_KEYS if isinstance(node.get(k), str)), "tool"
        )
        args = next((node[k] for k in TOOL_ARG_KEYS if k in node), None)
        if args is None:
            args = {k: v for k, v in node.items() if k not in VOLATILE_KEYS}
        payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        out.append(f"{name}:{hashlib.sha256(payload.encode()).hexdigest()[:12]}")
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
    calls = [fp for e in entries for fp in _tool_fingerprints(e.get("event"))]
    window = calls[-LOOP_WINDOW_CALLS:]
    if len(window) < LOOP_REPEAT_THRESHOLD:
        return None  # too little to say anything: a short tail is not a loop
    fingerprint, repeats = Counter(window).most_common(1)[0]
    distinct = len(set(window))
    if repeats < LOOP_REPEAT_THRESHOLD or distinct / len(window) > LOOP_DISTINCT_RATIO:
        return None
    return {
        "tool": fingerprint.split(":", 1)[0],
        "repeats": repeats,
        "window": len(window),
        "distinct": distinct,
    }


def build_digest(home: Path, all_tasks: list[tasks.Task], now: datetime, directives: list[str]) -> str:
    """The manager's whole world, compiled from files. Pure and greppable:
    task lines look like `- [status] shortid ...` so both models and tests
    can parse them."""
    home = Path(home)
    live = [t for t in all_tasks if t.status not in tasks.TERMINAL_STATUSES]
    active = [t for t in live if not t.attached]
    attached = [t for t in live if t.attached]
    lines = [f"# Situation digest — {fsio.iso(now)}", ""]

    lines.append("## Active tasks")
    if not active:
        lines.append("(none)")
    for t in active:
        alive = tasks.runner_alive(home, t.id)
        seen = tasks.last_activity(home, t.id)
        quiet = f"{int((now - seen).total_seconds() // 60)}m" if seen else "never-ran"
        lines.append(
            f"- [{t.status}] {t.short_id} project={t.project} harness={t.harness} "
            f"runner={'alive' if alive else 'dead'} runs={len(t.runs)} quiet={quiet}"
        )
        first = t.prompt.strip().splitlines()[0] if t.prompt.strip() else ""
        lines.append(f"  prompt: {first[:120]}")
        git = tasks.workdir_git_state(t)
        if git and (git["dirty"] or git["unpushed"]):
            unpushed = "no-remote" if git["unpushed"] is None else git["unpushed"]
            lines.append(
                f"  git: branch={git['branch']} dirty={git['dirty']} unpushed={unpushed}"
            )
        for r in tasks.read_reports(home, t.id, limit=3):
            lines.append(f"  report [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')[:160]}")
        scan = tasks.read_transcript_tail(home, t.id, limit=LOOP_SCAN_LINES)
        loop = loop_signal(scan)
        if loop:
            lines.append(
                f"  possible-loop: tool={loop['tool']} repeated={loop['repeats']}x "
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
            line = f"- [{t.status}] {t.short_id} project={t.project}" + (
                f" pr={t.pr_url}" if t.pr_url else ""
            )
            git = tasks.workdir_git_state(t)
            if git and (git["dirty"] or git["unpushed"]):
                unpushed = "no-remote" if git["unpushed"] is None else git["unpushed"]
                line += f" STRANDED-WORK dirty={git['dirty']} unpushed={unpushed}"
            lines.append(line)
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
            digest = build_digest(home, all_tasks, self.ctx.now(), directives)
            prompt = self.ctx.prompt("manager", digest=digest)
            run_agent_harness(self.ctx, prompt)
        except BaseException:
            for c in claimed:
                c.reject()  # directives go straight back to new/ for the next tick
            raise
        for c in claimed:
            c.ack()
        self.ctx.log_action("manager.run", "manager run complete")
