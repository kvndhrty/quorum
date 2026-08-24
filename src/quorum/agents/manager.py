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
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

from .. import fsio, tasks
from ..agent import Agent
from ..runner import build_harness_argv

DEFAULT_RUN_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ACTIONS_PER_RUN = 20

TRANSCRIPT_TAIL_LINES = 10
JOURNAL_TAIL_ENTRIES = 15
RECENT_TERMINAL_HOURS = 24


def journal_path(home: Path) -> Path:
    return Path(home) / "state" / "manager" / "journal.jsonl"


def transcript_path(home: Path) -> Path:
    return Path(home) / "state" / "manager" / "transcript.jsonl"


def last_activity(home: Path, task_id: str) -> datetime | None:
    """The newest sign of life: transcript, reports, or the runner lock."""
    newest = None
    for path in (
        tasks.transcript_path(home, task_id),
        tasks.reports_path(home, task_id),
        tasks.runner_lock_path(home, task_id),
    ):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=UTC)


def build_digest(home: Path, store: tasks.TaskStore, now: datetime, directives: list[str]) -> str:
    """The manager's whole world, compiled from files. Pure and greppable:
    task lines look like `- [status] shortid ...` so both models and tests
    can parse them."""
    home = Path(home)
    all_tasks = store.list()
    active = [t for t in all_tasks if t.status not in tasks.TERMINAL_STATUSES]
    lines = [f"# Situation digest — {fsio.iso(now)}", ""]

    lines.append("## Active tasks")
    if not active:
        lines.append("(none)")
    for t in active:
        alive = tasks.runner_alive(home, t.id)
        seen = last_activity(home, t.id)
        quiet = f"{int((now - seen).total_seconds() // 60)}m" if seen else "never-ran"
        lines.append(
            f"- [{t.status}] {t.short_id} project={t.project} harness={t.harness} "
            f"runner={'alive' if alive else 'dead'} runs={len(t.runs)} quiet={quiet}"
        )
        first = t.prompt.strip().splitlines()[0] if t.prompt.strip() else ""
        lines.append(f"  prompt: {first[:120]}")
        for r in tasks.read_reports(home, t.id, limit=3):
            lines.append(f"  report [{r.get('at', '')}] {r.get('status', '')}: {r.get('text', '')[:160]}")
        for e in tasks.read_transcript_tail(home, t.id, limit=TRANSCRIPT_TAIL_LINES):
            text = e.get("line") if "line" in e else json.dumps(e.get("event"), ensure_ascii=False)
            lines.append(f"  out| {str(text)[:160]}")
    lines.append("")

    recent_terminal = [
        t for t in all_tasks
        if t.status in tasks.TERMINAL_STATUSES
        and (now - fsio.parse_iso(t.updated_at)).total_seconds() < RECENT_TERMINAL_HOURS * 3600
    ]
    if recent_terminal:
        lines.append("## Recently finished (last 24h)")
        for t in recent_terminal:
            lines.append(f"- [{t.status}] {t.short_id} project={t.project}"
                         + (f" pr={t.pr_url}" if t.pr_url else ""))
        lines.append("")

    lines.append("## Your recent actions (journal)")
    entries = fsio.read_jsonl(journal_path(home))[-JOURNAL_TAIL_ENTRIES:]
    if not entries:
        lines.append("(none yet)")
    store_by_short = {t.short_id: t for t in all_tasks}
    for e in entries:
        target = e.get("target") or ""
        then = e.get("target_status") or "-"
        now_status = store_by_short[target].status if target in store_by_short else "-"
        changed = "" if then in ("-", now_status) else " (changed)"
        if then == now_status and then != "-":
            changed = " (UNCHANGED since)"
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
        store = tasks.TaskStore(home)
        active = [t for t in store.list() if t.status not in tasks.TERMINAL_STATUSES]
        pending = fsio.sorted_entries(self.ctx.bus.inbox_dir / "manager" / "new")
        if not active and not pending:
            return  # nothing to manage; don't spend a harness run on an idle home

        claimed = list(self.ctx.bus.claim("manager"))
        directives = [
            f"[{c.message.created_at}] {c.message.payload.get('text', '')}" for c in claimed
        ]
        try:
            digest = build_digest(home, store, self.ctx.now(), directives)
            prompt = self.ctx.prompt("manager", digest=digest)
            self._run_harness(prompt)
        except BaseException:
            for c in claimed:
                c.reject()  # directives go straight back to new/ for the next tick
            raise
        for c in claimed:
            c.ack()
        self.ctx.log_action("manager.run", "manager run complete")

    # -- harness invocation ------------------------------------------------

    def _resolve_harness(self):
        config = self.ctx.config
        name = self.ctx.settings.get("harness") or (config.tasks.default_harness if config else "")
        harness = config.harness.get(name) if (config and name) else None
        if harness is None:
            raise RuntimeError(
                f"manager has no usable harness (looked for [harness.{name or '?'}] in "
                "config.toml) — supervision is halted until one is configured"
            )
        return harness

    def _run_harness(self, prompt: str) -> None:
        harness = self._resolve_harness()
        run_id = fsio.ulid()
        timeout = float(self.ctx.settings.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS))
        env = {
            **os.environ,
            **harness.env,
            "QUORUM_HOME": str(self.ctx.home),
            "QUORUM_ACTOR": "manager",
            "QUORUM_MANAGER_RUN": run_id,
        }
        argv = build_harness_argv(harness, prompt)
        transcript = transcript_path(self.ctx.home)
        proc = subprocess.Popen(
            argv,
            cwd=str(self.ctx.home),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                entry: dict = {"at": fsio.iso(fsio.utc_now()), "run": run_id}
                try:
                    entry["event"] = json.loads(line)
                except json.JSONDecodeError:
                    entry["line"] = line
                fsio.append_jsonl(transcript, entry)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(2)
            raise RuntimeError(
                f"manager harness run {run_id} timed out after {int(timeout)}s and was killed"
            ) from None
        reader.join(5)
        if code != 0:
            raise RuntimeError(f"manager harness run {run_id} exited {code}")
