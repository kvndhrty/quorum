"""Monitor: the one built-in agent — shepherds harness-driven tasks.

Each tick it walks every non-terminal task and does the smallest useful
thing:

* queued, never run          -> launch a detached run
* runner alive, activity     -> nothing (progress speaks for itself)
* runner alive, gone quiet   -> one stall warning on the board + a nudge in
                                the task's inbox (a cooperative harness that
                                checks `quorum task inbox` sees it mid-run)
* runner dead, not terminal  -> poke + relaunch (a resume run picks up the
                                nudge), up to `max_resumes`; then mark the
                                task blocked and escalate to the human

Status strings are the harness's own words; the monitor only treats
tasks.TERMINAL_STATUSES as "stop attending". With an LLM configured the
nudge is drafted from the transcript tail; without one a canned nudge is
sent — either way the poke happens (invariant 3: degrade, don't stop).

All decisions are re-derivable from files, so ticks are idempotent; dedupe
of announcements lives in the agent's private state.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from .. import fsio, runner, tasks
from ..agent import Agent

# How long a detached launch may take to produce a live runner before the
# monitor treats it as dead-on-arrival and spends a resume on it.
BOOTSTRAP_GRACE_SECONDS = 120


class Monitor(Agent):
    default_schedule = "every 2m"

    def tick(self) -> None:
        home = self.ctx.home
        store = tasks.TaskStore(home)
        state = self.ctx.load_state()
        stall_warned = state.setdefault("stall_warned", {})
        launched = state.setdefault("launched", {})
        now = self.ctx.now()
        stall_minutes = int(self.ctx.settings.get("stall_minutes", self._tasks_config().stall_minutes))
        max_resumes = int(self.ctx.settings.get("max_resumes", self._tasks_config().max_resumes))

        active_ids = set()
        for task in store.list():
            if task.status in tasks.TERMINAL_STATUSES:
                continue
            active_ids.add(task.id)
            alive = tasks.runner_alive(home, task.id)

            if alive:
                last_activity = self._last_activity(home, task.id)
                if last_activity is None:
                    continue
                quiet = now - last_activity
                if quiet < timedelta(minutes=stall_minutes):
                    stall_warned.pop(task.id, None)
                    continue
                mark = last_activity.isoformat()
                if stall_warned.get(task.id) == mark:
                    continue  # already warned about this silence
                stall_warned[task.id] = mark
                self._nudge(task, reason="stalled")
                text = (
                    f"task {task.short_id} ({task.project}) has been quiet for "
                    f"{int(quiet.total_seconds() // 60)}m (status: {task.status})"
                )
                self.ctx.bus.post(
                    self.name, tasks.BOARD_TOPIC, "task.stalled", text=text,
                    payload={"task": task.id},
                )
                self.ctx.log_action("task.stalled", text, task=task.id)
                continue

            stall_warned.pop(task.id, None)
            if task.status == "queued" and not task.runs and task.id not in launched:
                launched[task.id] = fsio.iso(now)
                self._launch(store, task, kind="task.started",
                             text=f"task {task.short_id} ({task.project}) started: {_headline(task)}")
                continue
            if not task.runs and task.id in launched:
                age = now - fsio.parse_iso(launched[task.id])
                if age < timedelta(seconds=BOOTSTRAP_GRACE_SECONDS):
                    continue  # a fresh launch is still booting; don't stack another
                launched[task.id] = fsio.iso(now)  # dead on arrival: re-enter the grace window

            # A run ended without the harness reporting a terminal status
            # (or a launch died before the runner got going): poke and retry
            # within the resume budget.
            if task.resumes < max_resumes:
                self._nudge(task, reason="exited")
                store.update(task.id, resumes=task.resumes + 1)
                self._launch(store, task, kind="task.resumed",
                             text=f"task {task.short_id} resumed "
                                  f"(attempt {task.resumes + 1}/{max_resumes}, status: {task.status})")
            else:
                store.update(task.id, status="blocked")
                text = (
                    f"task {task.short_id} ({task.project}) marked blocked after "
                    f"{max_resumes} resume(s) without a terminal report — needs a human "
                    f"(`quorum task show {task.short_id}`)"
                )
                self.ctx.bus.post(
                    self.name, tasks.BOARD_TOPIC, "task.blocked", text=text,
                    payload={"task": task.id},
                )
                self.ctx.log_action("task.blocked", text, task=task.id)

        # keep dedupe state bounded to tasks that still matter
        state["stall_warned"] = {k: v for k, v in stall_warned.items() if k in active_ids}
        state["launched"] = {k: v for k, v in launched.items() if k in active_ids}
        self.ctx.save_state(state)

    # -- helpers -----------------------------------------------------------

    def _tasks_config(self):
        if self.ctx.config is not None:
            return self.ctx.config.tasks
        from ..config import TasksConfig

        return TasksConfig()

    def _launch(self, store: tasks.TaskStore, task: tasks.Task, kind: str, text: str) -> None:
        try:
            runner.launch_detached(self.ctx.home, task.id)
        except OSError as e:
            self.ctx.bus.post(
                self.name, tasks.BOARD_TOPIC, "task.error",
                text=f"could not launch task {task.short_id}: {e}", payload={"task": task.id},
            )
            return
        self.ctx.bus.post(self.name, tasks.BOARD_TOPIC, kind, text=text, payload={"task": task.id})
        self.ctx.log_action(kind, text, task=task.id)

    def _last_activity(self, home: Path, task_id: str):
        """The newest sign of life: transcript, reports, or the lock itself."""
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
        from datetime import UTC, datetime

        return datetime.fromtimestamp(newest, tz=UTC)

    def _nudge(self, task: tasks.Task, reason: str) -> None:
        """Put guidance in the task's inbox; the next run (or a cooperative
        harness mid-run) will read it."""
        text = None
        if self.ctx.llm.enabled:
            tail = tasks.read_transcript_tail(self.ctx.home, task.id, limit=20)
            prompt = self.ctx.prompt(
                "monitor-nudge",
                task_id=task.short_id,
                status=task.status,
                reason=reason,
                transcript_tail="\n".join(json.dumps(e, ensure_ascii=False) for e in tail),
            )
            text = self.ctx.llm.complete(prompt)
        if not text:
            if reason == "exited":
                text = (
                    "Your previous run ended without reporting a terminal status. "
                    "Review where you left off, continue the task, and keep reporting "
                    "progress with `quorum task report`. If you are stuck, report "
                    "status 'blocked' with what you need."
                )
            else:
                text = (
                    "You have been quiet for a while. If you are making progress, "
                    "report it with `quorum task report`; if you are stuck, report "
                    "status 'blocked' with what you need."
                )
        self.ctx.bus.send(self.name, tasks.inbox_name(task.id), type="nudge", text=text)


def _headline(task: tasks.Task) -> str:
    first = task.prompt.strip().splitlines()[0] if task.prompt.strip() else ""
    return first[:80] + ("…" if len(first) > 80 else "")
