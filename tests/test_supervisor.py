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


def test_control_inbox_pause_resume_run_now(home: Path):
    (home / "plugins" / "ctl.py").write_text(
        "from quorum.agent import Agent\n"
        "class Ctl(Agent):\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    config = write_config(home, '[agents.ctl]\ntype = "ctl:Ctl"\nschedule = "every 1h"\n')
    sup = Supervisor(home, config)
    sup.scheduler.start(paused=True)
    try:
        sup._schedule_agent("ctl", sup.agents["ctl"])
        bus = MessageBus(home)

        bus.send("user", "supervisor", type="agent.pause", payload={"agent": "ctl"})
        sup._control()
        assert sup.scheduler.get_job("ctl").next_run_time is None
        hb = fsio.read_json(home / "state/agents/ctl/heartbeat.json")
        assert hb["status"] == "paused"

        sup._failures["ctl"] = 3
        bus.send("user", "supervisor", type="agent.resume", payload={"agent": "ctl"})
        sup._control()
        assert sup.scheduler.get_job("ctl").next_run_time is not None
        assert sup._failures["ctl"] == 0  # manual resume clears the auto-pause counter

        before = sup.scheduler.get_job("ctl").next_run_time
        bus.send("user", "supervisor", type="agent.run-now", payload={"agent": "ctl"})
        sup._control()
        assert sup.scheduler.get_job("ctl").next_run_time < before

        # unknown agent: logged, acked, never raises
        bus.send("user", "supervisor", type="agent.pause", payload={"agent": "ghost"})
        sup._control()
        inbox = bus.inbox_dir / "supervisor"
        assert fsio.sorted_entries(inbox / "new") == []
        assert fsio.sorted_entries(inbox / "cur") == []
    finally:
        sup.scheduler.shutdown(wait=False)


def test_agent_reload_hot_adds_and_removes_file_defined_agents(home: Path):
    (home / "plugins" / "hot.py").write_text(
        "from quorum.agent import Agent\n"
        "class Hot(Agent):\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    config = write_config(home, "")
    sup = Supervisor(home, config)
    sup.scheduler.start(paused=True)
    try:
        bus = MessageBus(home)

        # create after startup: reload gives the new agent a job, no restart
        fsio.atomic_write_text(
            home / "agents" / "hot.toml", 'type = "hot:Hot"\nschedule = "every 10m"\n'
        )
        bus.send("user", "supervisor", type="agent.reload", payload={"agent": "hot"})
        sup._control()
        assert "hot" in sup.agents
        assert sup.scheduler.get_job("hot") is not None

        # edit: the schedule change is picked up on the next reload
        fsio.atomic_write_text(
            home / "agents" / "hot.toml", 'type = "hot:Hot"\nschedule = "every 2m"\n'
        )
        bus.send("user", "supervisor", type="agent.reload", payload={"agent": "hot"})
        sup._control()
        assert sup.config.agents["hot"].schedule == "every 2m"

        # remove: the file disappearing unschedules the agent
        (home / "agents" / "hot.toml").unlink()
        bus.send("user", "supervisor", type="agent.reload", payload={"agent": "hot"})
        sup._control()
        assert "hot" not in sup.agents
        assert sup.scheduler.get_job("hot") is None
    finally:
        sup.scheduler.shutdown(wait=False)


def test_pause_survives_supervisor_restart(home: Path):
    (home / "plugins" / "dur.py").write_text(
        "from quorum.agent import Agent\n"
        "class Dur(Agent):\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    config = write_config(home, '[agents.dur]\ntype = "dur:Dur"\nschedule = "every 1h"\n')
    sup = Supervisor(home, config)
    sup.scheduler.start(paused=True)
    try:
        sup._schedule_agent("dur", sup.agents["dur"])
        MessageBus(home).send("user", "supervisor", type="agent.pause", payload={"agent": "dur"})
        sup._control()
        assert sup.scheduler.get_job("dur").next_run_time is None
    finally:
        sup.scheduler.shutdown(wait=False)

    # a fresh supervisor (the restart) must schedule the agent paused
    sup2 = Supervisor(home, load_config(home))
    sup2.scheduler.start(paused=True)
    try:
        sup2._schedule_agent("dur", sup2.agents["dur"])
        assert sup2.scheduler.get_job("dur").next_run_time is None

        MessageBus(home).send("user", "supervisor", type="agent.resume", payload={"agent": "dur"})
        sup2._control()
        assert sup2.scheduler.get_job("dur").next_run_time is not None
    finally:
        sup2.scheduler.shutdown(wait=False)


def test_scheduled_tick_skips_when_lock_held_elsewhere(home: Path):
    (home / "plugins" / "lk.py").write_text(
        "from quorum.agent import Agent\n"
        "class Lk(Agent):\n"
        "    def tick(self):\n"
        "        self.ctx.bus.post(self.name, 'lk', text='ran')\n"
    )
    config = write_config(home, '[agents.lk]\ntype = "lk:Lk"\nschedule = "every 1h"\n')
    sup = Supervisor(home, config)

    lock = home / "state" / "agents" / "lk" / "tick.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"pid": 1}\n')  # a live foreign pid: run-once in flight
    sup.run_agent_tick("lk")
    assert MessageBus(home).read_topic("lk") == []  # tick was skipped

    lock.unlink()
    sup.run_agent_tick("lk")
    assert len(MessageBus(home).read_topic("lk")) == 1


def test_pause_landing_mid_tick_survives_the_completion_write(home: Path):
    """A pause applied while a tick is in flight must still read as paused
    after the tick's final heartbeat write — the paused job never runs again,
    so nothing else would ever correct the file."""
    (home / "plugins" / "slow.py").write_text(
        "from quorum.agent import Agent\n"
        "from quorum import fsio\n"
        "class Slow(Agent):\n"
        "    def tick(self):\n"
        "        # a pause command lands while this tick is running\n"
        "        from quorum.agent import write_heartbeat\n"
        "        write_heartbeat(self.ctx.home, self.name, status='paused', error='paused by user')\n"
    )
    config = write_config(home, '[agents.slow]\ntype = "slow:Slow"\nschedule = "every 1h"\n')
    sup = Supervisor(home, config)
    sup.run_agent_tick("slow")

    hb = fsio.read_json(home / "state/agents/slow/heartbeat.json")
    assert hb["status"] == "paused"      # not clobbered back to idle
    assert hb["last_end"]                # timing fields still recorded


def test_recovered_tick_clears_stale_failure_fields(home: Path):
    """Heartbeat writes are merges, so a successful tick must actively clear
    `error`/`consecutive_failures` — otherwise an agent that failed once reads
    as broken in every dashboard until someone edits the file by hand."""
    (home / "plugins" / "recovers.py").write_text(
        "from quorum.agent import Agent\n"
        "class Recovers(Agent):\n"
        "    def tick(self):\n"
        "        if not (self.ctx.home / 'fixed').exists():\n"
        "            raise RuntimeError('llm service down')\n"
    )
    config = write_config(
        home,
        '[agents.rec]\ntype = "recovers:Recovers"\nschedule = "every 5m"\nauto_pause = false\n',
    )
    sup = Supervisor(home, config)
    sup.run_agent_tick("rec")
    hb = fsio.read_json(home / "state/agents/rec/heartbeat.json")
    assert hb["status"] == "error" and hb["consecutive_failures"] == 1

    (home / "fixed").write_text("")
    sup.run_agent_tick("rec")
    hb = fsio.read_json(home / "state/agents/rec/heartbeat.json")
    assert hb["status"] == "idle"
    assert not hb.get("error")
    assert hb["consecutive_failures"] == 0


def test_auto_pause_false_keeps_a_failing_agent_scheduled(home: Path):
    """The manager's crash story: LLM down => every tick fails, but the agent
    must never be paused, so the first tick after service returns recovers."""
    (home / "plugins" / "flaky.py").write_text(
        "from quorum.agent import Agent\n"
        "class Flaky(Agent):\n"
        "    def tick(self):\n"
        "        raise RuntimeError('llm service down')\n"
    )
    config = write_config(
        home,
        '[agents.flaky]\ntype = "flaky:Flaky"\nschedule = "every 5m"\nauto_pause = false\n',
    )
    sup = Supervisor(home, config)
    for _ in range(MAX_CONSECUTIVE_FAILURES + 2):
        sup.run_agent_tick("flaky")

    hb = fsio.read_json(home / "state/agents/flaky/heartbeat.json")
    assert hb["status"] == "error"  # loud...
    assert hb["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES + 2
    system = MessageBus(home).read_topic("system")
    assert not any(m.type == "agent.paused" for m in system)  # ...but never paused


FAKE_HARNESS = str(Path(__file__).parent / "bin" / "fake_harness.py")


def write_flaky_manager(home: Path) -> Config:
    """A real manager whose harness is the fake one behind a shim that fails
    until `home/llm-up` exists — the LLM outage of #24, reproduced end to end
    without touching the manager or the supervisor's notion of failure."""
    shim = home / "flaky_harness.py"
    shim.write_text(
        "import os, sys\n"
        f"if not os.path.exists({str(home / 'llm-up')!r}):\n"
        "    sys.exit(3)\n"
        f"os.execv(sys.executable, [sys.executable, {FAKE_HARNESS!r}, *sys.argv[1:]])\n"
    )
    return write_config(
        home,
        "[harness.mgr]\n"
        f'start = ["{sys.executable}", "{shim}"]\n'
        'env = { FAKE_HARNESS_MODE = "echo" }\n'
        "[agents.manager]\n"
        'type = "manager"\n'
        'schedule = "every 5m"\n'
        "auto_pause = false\n"
        "[agents.manager.settings]\n"
        'harness = "mgr"\n'
    )


def test_sustained_failure_of_an_unpausable_agent_escalates_once(home: Path):
    """#38: `auto_pause = false` exempts the manager from the auto-pause that
    would otherwise reach `attention`, so its streak has to escalate on its
    own — exactly once, and closed by one recovery post."""
    config = write_flaky_manager(home)
    bus = MessageBus(home)
    # A directive keeps the manager's wake condition true every tick: a failing
    # tick rejects it straight back to new/ for the next one.
    bus.send("user", "manager", type="directive", text="keep an eye on the queue")
    sup = Supervisor(home, config)

    for _ in range(MAX_CONSECUTIVE_FAILURES):
        sup.run_agent_tick("manager")
    attention = bus.read_topic("attention")
    assert [m.type for m in attention] == ["agent.failing"]
    assert attention[0].payload["agent"] == "manager"
    assert attention[0].payload["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES
    # the harness failure itself, not a generic "agent broke"
    assert "exited 3" in attention[0].payload["text"]
    hb = fsio.read_json(home / "state/agents/manager/heartbeat.json")
    assert hb["escalated_at"]

    # ...and the streak stays one post, however long the outage runs
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        sup.run_agent_tick("manager")
    assert [m.type for m in bus.read_topic("attention")] == ["agent.failing"]
    assert not any(m.type == "agent.paused" for m in bus.read_topic("system"))
    # the per-tick record is unchanged: still loud on the system topic
    assert sum(m.type == "agent.error" for m in bus.read_topic("system")) == (
        2 * MAX_CONSECUTIVE_FAILURES
    )

    # the banner every reader surfaces shows it
    banner = views.attention_summary(home)
    assert banner["count"] == 1
    assert "manager" in banner["recent"][-1]["text"]

    # service returns: one recovery post, dedupe field cleared
    (home / "llm-up").write_text("")
    sup.run_agent_tick("manager")
    hb = fsio.read_json(home / "state/agents/manager/heartbeat.json")
    assert hb["status"] == "idle" and hb["consecutive_failures"] == 0
    assert not hb.get("escalated_at")
    assert [m.type for m in bus.read_topic("attention")] == ["agent.failing", "agent.recovered"]
    assert views.attention_summary(home)["count"] == 2
    assert (home / "state/manager/transcript.jsonl").exists()  # the run really happened


def test_escalation_never_fires_for_an_auto_pausing_agent(home: Path):
    """The pause path is unchanged: a normal agent still pauses, and never
    reports itself as a failing-but-running one."""
    (home / "plugins" / "bomb2.py").write_text(
        "from quorum.agent import Agent\n"
        "class Bomb(Agent):\n"
        "    def tick(self):\n"
        "        raise RuntimeError('boom')\n"
    )
    config = write_config(home, '[agents.bomb2]\ntype = "bomb2:Bomb"\nschedule = "every 1h"\n')
    sup = Supervisor(home, config)
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        sup.run_agent_tick("bomb2")

    hb = fsio.read_json(home / "state/agents/bomb2/heartbeat.json")
    assert hb["status"] == "paused"
    assert "escalated_at" not in hb
    assert not any(m.type == "agent.failing" for m in MessageBus(home).read_topic("attention"))
