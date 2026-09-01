"""The supervisor: one long-running process hosting APScheduler.

`quorum up` runs this in the foreground (background it with nohup/tmux — no
daemonization, no cron, no systemd, no elevated permissions). Each enabled
agent becomes one scheduler job; a wrapper isolates crashes, writes heartbeat
files for the dashboards, and pauses an agent that fails repeatedly — or,
when that agent is exempt from pausing (`auto_pause = false`), escalates its
streak once to the `attention` board so it still reaches a human. An
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
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from . import fsio
from .agent import Agent, AgentContext, tick_lock_path, write_heartbeat
from .config import Config, parse_schedule
from .messages import MessageBus
from .registry import resolve

MAX_CONSECUTIVE_FAILURES = 5
LOCK_TOUCH_SECONDS = 60
CONTROL_POLL_SECONDS = 15

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
                self._clear_load_error(name)
            except Exception as e:  # config errors must not kill startup
                self.errors[name] = str(e)
                log.error("agent %s failed to load: %s", name, e)
                # Persist it: the views are pure file readers, so an agent that
                # never got built would otherwise be indistinguishable from one
                # that simply has not ticked yet.
                self._write_heartbeat(
                    name, status="error", error=f"failed to load: {e}", load_error=True
                )

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
            self.scheduler.add_job(
                self._control,
                id="_control",
                trigger="interval",
                seconds=CONTROL_POLL_SECONDS,
                coalesce=True,
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
            self._shutdown_scheduler()
            fsio.release_pid_lock(self.lock_path)
            log.info("supervisor stopped")

    def _shutdown_scheduler(self) -> None:
        """Shut the scheduler down without ever raising.

        Startup can fail before (or during) scheduler.start() — APScheduler
        imports its trigger plugins lazily, for one. A raise from this path
        would replace the original traceback and skip the lock release,
        leaving a stale supervisor.lock that makes `quorum status` report a
        dead process as running.
        """
        try:
            self.scheduler.shutdown(wait=True)
        except Exception:
            log.debug("scheduler shutdown skipped", exc_info=True)

    def _handle_signal(self, signum, frame) -> None:
        log.info("received signal %s, shutting down", signum)
        self._stop.set()

    # -- agent jobs ----------------------------------------------------------

    def _schedule_agent(self, name: str, agent: Agent) -> None:
        kwargs = parse_schedule(self.config.agents[name].schedule)
        # Durable pause: a paused heartbeat survives supervisor restarts, so
        # the job is created paused rather than silently resuming.
        if self._read_heartbeat(name).get("status") == "paused":
            kwargs["next_run_time"] = None
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
        lock = tick_lock_path(self.home, name)
        try:
            fsio.acquire_pid_lock(lock, meta={"role": "tick", "agent": name})
        except fsio.LockError:
            # someone is ticking this agent by hand (`quorum agent run-once`);
            # skipping is safe — ticks are idempotent and the schedule returns
            log.info("agent %s tick skipped: tick lock held elsewhere", name)
            return
        try:
            self._run_agent_tick_locked(name, agent)
        finally:
            fsio.release_pid_lock(lock)

    def _run_agent_tick_locked(self, name: str, agent: Agent) -> None:
        started = fsio.utc_now()
        self._write_heartbeat(name, status="running", last_start=fsio.iso(started))
        try:
            agent.tick()
        except Exception:
            err = traceback.format_exc()
            log.error("agent %s tick failed:\n%s", name, err)
            self._failures[name] = self._failures.get(name, 0) + 1
            self._finish_heartbeat(
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
            acfg = self.config.agents.get(name)
            if self._failures[name] >= MAX_CONSECUTIVE_FAILURES:
                if acfg is None or acfg.auto_pause:
                    self._pause_agent(name)
                else:
                    self._escalate_failing(name, self._failures[name])
            return
        self._failures[name] = 0
        ended = fsio.utc_now()
        # Read before the write below clears it: the escalation stamp is how a
        # closed streak is told from one that never reached the banner.
        escalated_at = self._read_heartbeat(name).get("escalated_at")
        # Heartbeat writes are merges: recovery must clear the failure fields
        # explicitly, or a long-fixed agent reads as broken in every dashboard.
        self._finish_heartbeat(
            name,
            status="idle",
            last_start=fsio.iso(started),
            last_end=fsio.iso(ended),
            duration_ms=int((ended - started).total_seconds() * 1000),
            error=None,
            consecutive_failures=0,
            escalated_at=None,
        )
        if escalated_at:
            self._announce_recovery(name, escalated_at)

    def _finish_heartbeat(self, name: str, **fields) -> None:
        """End-of-tick heartbeat write that respects an external pause.

        A `quorum agent pause` (or auto-pause) that lands while a tick is in
        flight writes status="paused"; the tick's completion write must not
        clobber that back to idle/error, or every dashboard would show a
        paused agent as healthy forever (the paused job never runs again to
        correct it). Timing fields still land either way.
        """
        if self._read_heartbeat(name).get("status") == "paused":
            fields.pop("status", None)
        self._write_heartbeat(name, **fields)

    def _escalate_failing(self, name: str, failures: int) -> None:
        """Put a sustained failure streak of a never-paused agent in front of a human.

        An agent with `auto_pause = false` (the manager) is exempt from the
        auto-pause that would otherwise post to `attention` — which left the
        one agent that must never stop as the one whose sustained failure
        never reached the banner `quorum status`, the TUI and the web header
        read (`views.attention_summary`). It posts once per *streak*, not per
        tick: the `escalated_at` heartbeat stamp is the dedupe, and the
        success path clears it alongside the other failure fields.
        """
        hb = self._read_heartbeat(name)
        if hb.get("escalated_at"):
            return
        self._write_heartbeat(name, escalated_at=fsio.iso(fsio.utc_now()))
        error = hb.get("error") or "see the agent transcript"
        self.bus.post(
            "supervisor",
            "attention",
            "agent.failing",
            text=f"agent {name} has failed {failures} consecutive ticks and is not "
            f"auto-paused (auto_pause = false), so it keeps retrying — last error: {error}",
            payload={"agent": name, "consecutive_failures": failures},
        )
        log.error("agent %s escalated to attention after %d consecutive failures", name, failures)

    def _announce_recovery(self, name: str, escalated_at: str) -> None:
        """Close the story an `agent.failing` post opened. The banner is
        time-windowed, so this costs nothing long-term — but a human who saw
        the escalation would otherwise have no way to learn it ended."""
        self.bus.post(
            "supervisor",
            "attention",
            "agent.recovered",
            text=f"agent {name} is ticking again (failing since {escalated_at})",
            payload={"agent": name, "escalated_at": escalated_at},
        )
        log.info("agent %s recovered after an escalated failure streak", name)

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
            "(fix the cause and `quorum agent resume` it)",
            payload={"agent": name},
        )

    def _read_heartbeat(self, name: str) -> dict:
        """An agent's heartbeat, or {} when it has none (or an unreadable one)."""
        try:
            return fsio.read_json(self.home / "state" / "agents" / name / "heartbeat.json")
        except (OSError, ValueError):
            return {}

    def _write_heartbeat(self, name: str, **fields) -> None:
        try:
            job = self.scheduler.get_job(name)
            if job and job.next_run_time:
                fields["next_run"] = fsio.iso(job.next_run_time)
        except Exception:
            pass
        write_heartbeat(self.home, name, **fields)

    def _clear_load_error(self, name: str) -> None:
        """Drop a previous run's load failure once the agent builds again.

        Without this a fixed config.toml would keep showing the old error
        until the agent's first tick landed.
        """
        path = self.home / "state" / "agents" / name / "heartbeat.json"
        try:
            current = fsio.read_json(path)
        except (OSError, ValueError):
            return
        if not current.pop("load_error", False):
            return
        current.pop("status", None)
        current.pop("error", None)
        fsio.atomic_write_json(path, current)

    # -- control channel ------------------------------------------------------

    def _control(self) -> None:
        """Apply `quorum agent pause|resume|run-now` commands from the
        supervisor's inbox. This is the only runtime lever that doesn't
        require editing config.toml and restarting."""
        for claimed in self.bus.claim("supervisor"):
            msg = claimed.message
            name = (msg.payload or {}).get("agent", "")
            try:
                self._apply_control(msg.type, name)
            except Exception:
                log.error("control command %s(%s) failed:\n%s", msg.type, name, traceback.format_exc())
            claimed.ack()

    def _apply_control(self, command: str, name: str) -> None:
        if command == "agent.reload":
            # Must come before the job lookup: a freshly created agent has no
            # job yet — the whole point of the reload is to give it one.
            self._reload_agent(name)
            return
        job = self.scheduler.get_job(name)
        if job is None:
            log.warning("control command %s for unknown/unscheduled agent %r", command, name)
            return
        if command == "agent.pause":
            job.pause()
            self._write_heartbeat(name, status="paused", error="paused by user")
            log.info("agent %s paused by user", name)
        elif command == "agent.resume":
            self._failures[name] = 0
            job.resume()
            self._write_heartbeat(name, status="idle", error=None)
            log.info("agent %s resumed by user", name)
        elif command == "agent.run-now":
            job.modify(next_run_time=fsio.utc_now())
            log.info("agent %s scheduled to run now", name)
        else:
            log.warning("unknown control command %r", command)

    def _reload_agent(self, name: str) -> None:
        """Pick up a created, edited, or removed `agents/<name>.toml` (or a
        config.toml agent change) without a restart. The file is the source
        of truth; the reload message is only a poke."""
        from .config import ConfigError, load_config

        try:
            fresh = load_config(self.home)
        except ConfigError as e:
            log.error("agent.reload(%s): config is unloadable: %s", name, e)
            return
        self.config = fresh
        try:
            if self.scheduler.get_job(name):
                self.scheduler.remove_job(name)
        except Exception:
            log.debug("agent.reload(%s): job removal skipped", name, exc_info=True)
        acfg = fresh.agents.get(name)
        if acfg is None or not acfg.enabled:
            self.agents.pop(name, None)
            self._failures.pop(name, None)
            write_heartbeat(self.home, name, status="removed", error=None, next_run=None)
            log.info("agent %s removed via reload", name)
            return
        try:
            cls = resolve(acfg.type, self.home)
            ctx = AgentContext(home=self.home, name=name, settings=acfg.settings, config=self.config)
            self.agents[name] = cls(ctx)
            self._clear_load_error(name)
        except Exception as e:
            self.errors[name] = str(e)
            log.error("agent %s failed to load on reload: %s", name, e)
            self._write_heartbeat(
                name, status="error", error=f"failed to load: {e}", load_error=True
            )
            return
        self._failures[name] = 0
        self._schedule_agent(name, self.agents[name])
        self._write_heartbeat(name)
        log.info("agent %s (re)loaded: %s @ %s", name, acfg.type, acfg.schedule)

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
    # UTC, in fsio.iso()'s shape: every other timestamp quorum writes is UTC,
    # so a local-time log cannot be lined up against the board or heartbeats.
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger("quorum")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
