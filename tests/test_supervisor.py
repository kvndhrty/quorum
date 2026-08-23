from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quorum import fsio, views
from quorum.config import Config, load_config
from quorum.messages import MessageBus
from quorum.registry import AgentResolutionError, resolve
from quorum.supervisor import MAX_CONSECUTIVE_FAILURES, Supervisor


def write_config(home: Path, body: str) -> Config:
    (home / "config.toml").write_text(body)
    return load_config(home)


def test_plugin_agent_resolution(home: Path):
    (home / "plugins" / "myagent.py").write_text(
        "from quorum.agent import Agent\n"
        "class Custom(Agent):\n"
        "    def tick(self):\n"
        "        self.ctx.bus.post(self.name, 'custom', text='hi from plugin')\n"
    )
    cls = resolve("myagent:Custom", home)
    assert cls.__name__ == "Custom"
    try:
        resolve("nonsense", home)
        raise AssertionError("should have raised")
    except AgentResolutionError:
        pass


def test_crash_isolation_and_auto_pause(home: Path):
    (home / "plugins" / "bomb.py").write_text(
        "from quorum.agent import Agent\n"
        "class Bomb(Agent):\n"
        "    def tick(self):\n"
        "        raise RuntimeError('boom')\n"
    )
    config = write_config(
        home,
        '[agents.bomb]\ntype = "bomb:Bomb"\nschedule = "every 1h"\n',
    )
    sup = Supervisor(home, config)
    assert "bomb" in sup.agents

    for _ in range(MAX_CONSECUTIVE_FAILURES):
        sup.run_agent_tick("bomb")  # must not raise: crash is isolated

    hb = fsio.read_json(home / "state/agents/bomb/heartbeat.json")
    assert hb["status"] == "paused"
    assert hb["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES

    system = MessageBus(home).read_topic("system")
    assert any(m.type == "agent.error" for m in system)
    assert any(m.type == "agent.paused" for m in system)


def test_healthy_tick_writes_heartbeat(home: Path):
    (home / "plugins" / "ok.py").write_text(
        "from quorum.agent import Agent\n"
        "class Ok(Agent):\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    config = write_config(home, '[agents.ok]\ntype = "ok:Ok"\nschedule = "every 5m"\n')
    sup = Supervisor(home, config)
    sup.run_agent_tick("ok")
    hb = fsio.read_json(home / "state/agents/ok/heartbeat.json")
    assert hb["status"] == "idle"
    assert hb["duration_ms"] >= 0


def test_bad_agent_type_does_not_kill_startup(home: Path):
    config = write_config(home, '[agents.ghost]\ntype = "missing:Ghost"\n')
    sup = Supervisor(home, config)
    assert sup.agents == {}
    assert "ghost" in sup.errors


def test_startup_failure_propagates_and_releases_lock(home: Path):
    """A raise before scheduler.start() must survive the finally block.

    APScheduler resolves trigger plugins lazily, so add_job is a real failure
    point (a sandboxed supervisor hit exactly this). Shutting down a scheduler
    that never started used to raise from the finally, replacing the original
    traceback and skipping the lock release.
    """

    class Boom(RuntimeError):
        pass

    def explode(*args, **kwargs):
        raise Boom("no trigger plugin")

    sup = Supervisor(home, write_config(home, ""))
    sup.scheduler.add_job = explode

    with pytest.raises(Boom):
        sup.run()
    assert not (home / "supervisor.lock").exists()


def test_dead_pid_is_not_reported_alive(home: Path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # reaped: this pid is now certainly gone

    lock = home / "supervisor.lock"
    fsio.atomic_write_json(lock, {"role": "supervisor", "pid": proc.pid})
    assert views.supervisor_status(home)["alive"] is False  # fresh mtime, dead pid

    fsio.atomic_write_json(lock, {"role": "supervisor", "pid": os.getpid()})
    assert views.supervisor_status(home)["alive"] is True


def test_failed_load_is_visible_in_agent_rows(home: Path):
    config = write_config(home, '[agents.ghost]\ntype = "missing:Ghost"\n')
    Supervisor(home, config)

    row = next(r for r in views.agent_rows(home, config) if r["name"] == "ghost")
    assert row["status"] == "error"  # not "never-ran"
    assert "failed to load" in row["error"]

    # once the config is fixed, the stale failure must not linger
    (home / "plugins" / "ghost.py").write_text(
        "from quorum.agent import Agent\n"
        "class Ghost(Agent):\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    fixed = write_config(home, '[agents.ghost]\ntype = "ghost:Ghost"\n')
    Supervisor(home, fixed)
    row = next(r for r in views.agent_rows(home, fixed) if r["name"] == "ghost")
    assert row["status"] == "never-ran"
    assert row["error"] is None
