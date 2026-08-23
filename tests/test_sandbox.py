from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from quorum.config import AgentConfig, Config, LLMConfig, SandboxConfig
from quorum.llm import LLMClient
from quorum.sandbox import SandboxUnavailable, make_sandboxed_runner, self_sandbox

# These tests are installation-independent: nono-py presence is controlled by
# monkeypatching sys.modules, so they pin down both the fail-closed behavior
# and the glue's mapping onto nono_py.sandboxed_exec.


@pytest.fixture
def no_nono(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "nono_py", None)  # import raises ImportError


class FakeExecResult:
    def __init__(self, exit_code=0, stdout=b"sandboxed output\n", stderr=b""):
        # bytes, like the real nono_py.ExecResult — the glue owes callers text.
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FakeAccessMode:
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


# What nono's real `system_read_*` groups contribute: read-only paths without
# which a sandboxed child cannot exec anything at all.
FAKE_SYSTEM_READS = ("/bin", "/usr/lib")


class FakePolicy:
    def __init__(self, mod):
        self._mod = mod

    def resolve_groups(self, names, caps):
        self._mod.resolved_groups.append(list(names))
        for path in FAKE_SYSTEM_READS:
            caps.allow_path(path, FakeAccessMode.READ)
        return types.SimpleNamespace(names=list(names))


class FakeCapabilitySet:
    def __init__(self):
        self.paths: list[tuple[str, str]] = []
        self.files: list[tuple[str, str]] = []
        self.network_blocked = False
        self.deduplicated = False

    def allow_path(self, path, mode):
        self.paths.append((path, mode))

    def allow_file(self, path, mode):
        self.files.append((path, mode))

    def block_network(self):
        self.network_blocked = True

    def deduplicate(self):
        self.deduplicated = True


@pytest.fixture
def fake_nono(monkeypatch: pytest.MonkeyPatch):
    mod = types.ModuleType("nono_py")
    mod.AccessMode = FakeAccessMode
    mod.CapabilitySet = FakeCapabilitySet
    mod.calls = []
    mod.resolved_groups = []
    mod.load_embedded_policy = lambda: FakePolicy(mod)

    def sandboxed_exec(caps, command, cwd=None, timeout_secs=None, env=None, inherit_env=False):
        mod.calls.append(
            {"caps": caps, "command": command, "cwd": cwd, "timeout_secs": timeout_secs,
             "env": env, "inherit_env": inherit_env}
        )
        # echo back the staged prompt when the command is the sh redirect hop
        if command[0] == "/bin/sh":
            prompt_path = command[2].split("< ")[1].strip("'\"")
            return FakeExecResult(stdout=Path(prompt_path).read_bytes())
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


def test_missing_system_policy_fails_closed(home: Path, fake_nono):
    """A nono-py that cannot supply the system-read baseline must raise, not
    hand back a capability set in which nothing can exec."""
    from quorum.sandbox import build_capabilities

    def boom():
        raise RuntimeError("no embedded policy")

    fake_nono.load_embedded_policy = boom
    with pytest.raises(SandboxUnavailable, match=r"system read policy"):
        build_capabilities(home, Config())


def test_stdin_prompt_staged_and_cleaned(home: Path, fake_nono):
    config = Config(llm=LLMConfig(executable="fake-llm"))
    runner = make_sandboxed_runner(home, config)
    proc = runner(["fake-llm", "-p"], input="the prompt", timeout=30, env=None)
    assert proc.returncode == 0
    assert proc.stdout == "the prompt"  # round-tripped through the staged file
    assert isinstance(proc.stdout, str) and isinstance(proc.stderr, str)
    call = fake_nono.calls[-1]
    assert call["command"][0] == "/bin/sh" and call["command"][3:] == ["fake-llm", "-p"]
    assert call["timeout_secs"] == 30 and call["inherit_env"] is True
    assert call["cwd"] == str(home)  # never the parent's cwd, which is outside the caps
    assert not list((home / "state" / "llm").glob("prompt-*"))  # staging cleaned up


def test_runner_decodes_bytes_to_text(home: Path, fake_nono):
    """nono_py returns bytes; CliBackend is handed the runner as a
    subprocess.run stand-in with text=True and calls str methods on stdout."""
    config = Config(llm=LLMConfig(executable="fake-llm"))
    proc = make_sandboxed_runner(home, config)(["fake-llm", "prompt"])
    assert proc.stdout == "sandboxed output\n"
    assert isinstance(proc.stdout, str) and isinstance(proc.stderr, str)


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
    config.agents["steward"] = AgentConfig(
        type="steward",
        settings={
            "watch": [str(watch)],
            "rules": [{"match": "*", "dest": str(tmp_path / "papers")}],
        },
    )
    caps = build_capabilities(home, config)
    modes = dict(caps.paths)
    assert modes[str(home)] == FakeAccessMode.READ_WRITE
    assert modes[str(pdir.resolve())] == FakeAccessMode.READ
    assert modes[str(watch)] == FakeAccessMode.READ_WRITE
    assert modes[str(tmp_path / "papers")] == FakeAccessMode.READ_WRITE
    assert caps.network_blocked is True
    assert caps.deduplicated is True

    config.llm = LLMConfig(executable="claude")
    caps2 = build_capabilities(home, config)
    assert caps2.network_blocked is False


def test_capabilities_include_exec_baseline(home: Path, fake_nono):
    """Modes 2 and 3 cannot exec without nono's system-read groups, and mode 2
    keeps importing Python after apply() — so the interpreter tree must be
    readable too. Both are read-only; nothing new becomes writable."""
    import sysconfig

    from quorum.sandbox import SYSTEM_READ_GROUPS, build_capabilities

    caps = build_capabilities(home, Config())
    assert fake_nono.resolved_groups[-1] == list(SYSTEM_READ_GROUPS)
    modes = dict(caps.paths)
    for path in FAKE_SYSTEM_READS:
        assert modes[path] == FakeAccessMode.READ
    for path in (sys.prefix, sys.base_prefix, sysconfig.get_path("purelib")):
        assert modes.get(path) == FakeAccessMode.READ
    # the quorum package's own tree, which an editable install puts outside sys.prefix
    import quorum

    assert modes.get(str(Path(quorum.__file__).resolve().parent.parent)) == FakeAccessMode.READ
    writable = {p for p, m in caps.paths if m != FakeAccessMode.READ}
    assert writable == {str(home)}


def test_llm_executable_granted_by_absolute_path(
    home: Path, fake_nono, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A bare `[llm].executable` is resolved via PATH: the sandbox grants paths,
    and a name the child cannot resolve is a name it cannot run."""
    from quorum.sandbox import build_capabilities

    exe = tmp_path / "bin" / "myllm"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\ncat\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent), prepend=os.pathsep)
    caps = build_capabilities(home, Config(llm=LLMConfig(executable="myllm")))
    # allow_file, not allow_path: granting the containing dir would be wider.
    assert (str(exe.resolve()), FakeAccessMode.READ) in caps.files


def test_unresolvable_llm_executable_is_not_granted(home: Path, fake_nono):
    from quorum.sandbox import build_capabilities

    caps = build_capabilities(home, Config(llm=LLMConfig(executable="definitely-not-on-path")))
    assert caps.files == []
