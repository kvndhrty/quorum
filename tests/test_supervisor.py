from __future__ import annotations

from pathlib import Path

from quorum import fsio
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
