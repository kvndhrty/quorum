"""The supervisor: one long-running process hosting APScheduler.

`quorum up` runs this in the foreground (background it with nohup/tmux — no
daemonization, no cron, no systemd, no elevated permissions). Each enabled
agent becomes one scheduler job; a wrapper isolates crashes, writes heartbeat
files for the dashboards, and pauses an agent that fails repeatedly. An
internal janitor handles message archival and stale-claim recovery even when
every agent is disabled.

BackgroundScheduler (threads) rather than AsyncIO: agent work is file scans
and subprocess calls — blocking and thread-friendly — and synchronous tick()
keeps third-party agents trivial to write.
"""

from __future__ import annotations

import logging
import signal
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from . import fsio
from .agent import Agent, AgentContext
from .config import Config, parse_schedule
from .messages import MessageBus
from .registry import resolve

MAX_CONSECUTIVE_FAILURES = 5
LOCK_TOUCH_SECONDS = 60

log = logging.getLogger("quorum.supervisor")


class Supervisor:
    def __init__(self, home: Path, config: Config):
        self.home = Path(home)
        self.config = config
        self.bus = MessageBus(self.home)
        self.scheduler = BackgroundScheduler()
        self.lock_path = self.home / "supervisor.lock"
        self._stop = threading.Event()
        self._failures: dict[str, int] = {}
        self.agents: dict[str, Agent] = {}
        self.errors: dict[str, str] = {}
        self._build_agents()

    def _build_agents(self) -> None:
        for name, acfg in self.config.agents.items():
            if not acfg.enabled:
                continue
            try:
                cls = resolve(acfg.type, self.home)
                ctx = AgentContext(
                    home=self.home, name=name, settings=acfg.settings, config=self.config
                )
                self.agents[name] = cls(ctx)
            except Exception as e:  # config errors must not kill startup
                self.errors[name] = str(e)
                log.error("agent %s failed to load: %s", name, e)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Blocking foreground run until SIGINT/SIGTERM."""
        _setup_logging(self.home)
        fsio.acquire_pid_lock(self.lock_path, meta={"role": "supervisor"})
        try:
            for name, agent in self.agents.items():
                self._schedule_agent(name, agent)
            self.scheduler.add_job(
                self._janitor, id="_janitor", trigger="interval", hours=1, coalesce=True
            )
            self.scheduler.add_job(
                lambda: fsio.touch_lock(self.lock_path),
                id="_lock_heartbeat",
                trigger="interval",
                seconds=LOCK_TOUCH_SECONDS,
            )
            self.scheduler.start()
            for name, err in self.errors.items():
                self.bus.post(
                    "supervisor", "system", "agent.error", text=f"agent {name} failed to load: {err}"
                )
            log.info("supervisor up with %d agent(s): %s", len(self.agents), ", ".join(self.agents))
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
            self._janitor()
            self._stop.wait()
        finally:
            self.scheduler.shutdown(wait=True)
            fsio.release_pid_lock(self.lock_path)
            log.info("supervisor stopped")

    def _handle_signal(self, signum, frame) -> None:
        log.info("received signal %s, shutting down", signum)
        self._stop.set()

    # -- agent jobs ----------------------------------------------------------

    def _schedule_agent(self, name: str, agent: Agent) -> None:
        kwargs = parse_schedule(self.config.agents[name].schedule)
        self.scheduler.add_job(
            lambda: self.run_agent_tick(name),
            id=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            **kwargs,
        )

    def run_agent_tick(self, name: str) -> None:
        """Crash-isolating wrapper around one agent tick."""
        agent = self.agents[name]
        started = fsio.utc_now()
        self._write_heartbeat(name, status="running", last_start=fsio.iso(started))
        try:
            agent.tick()
        except Exception:
            err = traceback.format_exc()
            log.error("agent %s tick failed:\n%s", name, err)
            self._failures[name] = self._failures.get(name, 0) + 1
            self._write_heartbeat(
                name,
                status="error",
                last_start=fsio.iso(started),
                last_end=fsio.iso(fsio.utc_now()),
                error=err.strip().splitlines()[-1],
                consecutive_failures=self._failures[name],
            )
            self.bus.post(
                "supervisor",
                "system",
                "agent.error",
                text=f"agent {name} tick failed: {err.strip().splitlines()[-1]}",
                payload={"agent": name},
            )
            if self._failures[name] >= MAX_CONSECUTIVE_FAILURES:
                self._pause_agent(name)
            return
        self._failures[name] = 0
        ended = fsio.utc_now()
        self._write_heartbeat(
            name,
            status="idle",
            last_start=fsio.iso(started),
            last_end=fsio.iso(ended),
            duration_ms=int((ended - started).total_seconds() * 1000),
        )

    def _pause_agent(self, name: str) -> None:
        try:
            job = self.scheduler.get_job(name)
            if job:
                job.pause()
        except Exception:
            pass
        self._write_heartbeat(name, status="paused", error="auto-paused after repeated failures")
        self.bus.post(
            "supervisor",
            "system",
            "agent.paused",
            text=f"agent {name} auto-paused after {MAX_CONSECUTIVE_FAILURES} consecutive failures "
            "(fix the cause and restart `quorum up`)",
            payload={"agent": name},
        )

    def _write_heartbeat(self, name: str, **fields) -> None:
        path = self.home / "state" / "agents" / name / "heartbeat.json"
        current = {}
        try:
            current = fsio.read_json(path)
        except (OSError, ValueError):
            pass
        current.update(fields)
        try:
            job = self.scheduler.get_job(name)
            if job and job.next_run_time:
                current["next_run"] = fsio.iso(job.next_run_time)
        except Exception:
            pass
        fsio.atomic_write_json(path, current)

    # -- janitor -------------------------------------------------------------

    def _janitor(self) -> None:
        try:
            archived = self.bus.archive_old(self.config.quorum.retention_days)
            recovered = self.bus.recover_stale_claims()
            if archived or recovered:
                log.info("janitor: archived %d message(s), recovered %d claim(s)", archived, recovered)
        except Exception:
            log.error("janitor failed:\n%s", traceback.format_exc())


def _setup_logging(home: Path) -> None:
    logdir = home / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logdir / "supervisor.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("quorum")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
