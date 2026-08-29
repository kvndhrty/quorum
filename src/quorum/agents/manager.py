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

import json
from datetime import datetime
from pathlib import Path

from .. import actor, fsio, herdr, tasks
from ..actor import journal_path
from ..agent import Agent
from ..runner import guidance_note
from .harness_run import DEFAULT_RUN_TIMEOUT_SECONDS, run_agent_harness

__all__ = ["DEFAULT_RUN_TIMEOUT_SECONDS", "Manager", "build_digest", "journal_path", "transcript_path"]

TRANSCRIPT_TAIL_LINES = 10
JOURNAL_TAIL_ENTRIES = 15
RECENT_TERMINAL_HOURS = 24


def transcript_path(home: Path) -> Path:
    return actor.transcript_path(home, "manager")


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
        for e in tasks.read_transcript_tail(home, t.id, limit=TRANSCRIPT_TAIL_LINES):
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
