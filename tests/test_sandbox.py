from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from quorum.config import Config, LLMConfig, SandboxConfig
from quorum.llm import LLMClient
from quorum.sandbox import SandboxUnavailable, make_sandboxed_runner, self_sandbox

# These tests are installation-independent: nono-py presence is controlled by
# monkeypatching sys.modules, so they pin down both the fail-closed behavior
# and the glue's mapping onto nono_py.sandboxed_exec.


@pytest.fixture
def no_nono(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "nono_py", None)  # import raises ImportError


class FakeExecResult:
    def __init__(self, exit_code=0, stdout="sandboxed output\n", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FakeAccessMode:
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


class FakeCapabilitySet:
    def __init__(self):
        self.paths: list[tuple[str, str]] = []
        self.network_blocked = False

    def allow_path(self, path, mode):
        self.paths.append((path, mode))

    def block_network(self):
        self.network_blocked = True


@pytest.fixture
def fake_nono(monkeypatch: pytest.MonkeyPatch):
    mod = types.ModuleType("nono_py")
    mod.AccessMode = FakeAccessMode
    mod.CapabilitySet = FakeCapabilitySet
    mod.calls = []

    def sandboxed_exec(caps, command, timeout_secs=None, env=None, inherit_env=False):
        mod.calls.append(
            {"caps": caps, "command": command, "timeout_secs": timeout_secs,
             "env": env, "inherit_env": inherit_env}
        )
        # echo back the staged prompt when the command is the sh redirect hop
        if command[0] == "/bin/sh":
            prompt_path = command[2].split("< ")[1].strip("'\"")
            return FakeExecResult(stdout=Path(prompt_path).read_text())
        return FakeExecResult()

    mod.sandboxed_exec = sandboxed_exec
    mod.apply = lambda caps: mod.calls.append({"apply": caps})
    monkeypatch.setitem(sys.modules, "nono_py", mod)
    return mod


def test_self_sandbox_raises_friendly_error_without_nono(home: Path, no_nono):
    with pytest.raises(SandboxUnavailable, match=r"nono-py is not installed"):
        self_sandbox(home, Config())


def test_use_nono_without_nono_fails_closed(home: Path, fake_llm, no_nono):
    """use_nono=true + missing nono-py: complete() returns None (logged),
    never an unsandboxed execution of the CLI."""
    llm_cfg = LLMConfig(
        executable=fake_llm[0], args=fake_llm[1:],
        env={"FAKE_LLM_MODE": "ok", "FAKE_LLM_OUTPUT": "should never appear"},
    )
    config = Config(llm=llm_cfg, sandbox=SandboxConfig(use_nono=True))
    client = LLMClient.from_config(
        llm_cfg, home=home, sandbox_config=config.sandbox, full_config=config
    )
    assert client.enabled
    assert client.complete("hi") is None  # loud in logs, closed by default


def test_stdin_prompt_staged_and_cleaned(home: Path, fake_nono):
    config = Config(llm=LLMConfig(executable="fake-llm"))
    runner = make_sandboxed_runner(home, config)
    proc = runner(["fake-llm", "-p"], input="the prompt", timeout=30, env=None)
    assert proc.returncode == 0
    assert proc.stdout == "the prompt"  # round-tripped through the staged file
    call = fake_nono.calls[-1]
    assert call["command"][0] == "/bin/sh" and call["command"][3:] == ["fake-llm", "-p"]
    assert call["timeout_secs"] == 30 and call["inherit_env"] is True
    assert not list((home / "state" / "llm").glob("prompt-*"))  # staging cleaned up


def test_argv_mode_and_env_tuples(home: Path, fake_nono):
    config = Config(llm=LLMConfig(executable="fake-llm"))
    runner = make_sandboxed_runner(home, config)
    proc = runner(["fake-llm", "the prompt"], env={"A": "1"})
    assert proc.stdout.startswith("sandboxed output")
    call = fake_nono.calls[-1]
    assert call["command"] == ["fake-llm", "the prompt"]
    assert call["env"] == [("A", "1")] and call["inherit_env"] is False


def test_capabilities_reflect_config(home: Path, fake_nono, tmp_path: Path):
    from quorum.projects import ProjectRegistry
    from quorum.sandbox import build_capabilities

    pdir = tmp_path / "proj"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="proj")
    watch = tmp_path / "downloads"
    watch.mkdir()
    config = Config()  # no [llm] -> network blocked
    config.agents = {}
    from quorum.config import AgentConfig

    config.agents["steward"] = AgentConfig(
        type="steward", settings={"watch": [str(watch)], "rules": [{"match": "*", "dest": str(tmp_path / "papers")}]}
    )
    caps = build_capabilities(home, config)
    modes = dict(caps.paths)
    assert modes[str(home)] == FakeAccessMode.READ_WRITE
    assert modes[str(pdir.resolve())] == FakeAccessMode.READ
    assert modes[str(watch)] == FakeAccessMode.READ_WRITE
    assert modes[str(tmp_path / "papers")] == FakeAccessMode.READ_WRITE
    assert caps.network_blocked is True

    config.llm = LLMConfig(executable="claude")
    caps2 = build_capabilities(home, config)
    assert caps2.network_blocked is False
