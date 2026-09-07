from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from quorum.config import AgentConfig, Config, SandboxConfig
from quorum.sandbox import SandboxUnavailable, self_sandbox

# These tests are installation-independent: nono-py presence is controlled by
# monkeypatching sys.modules, so they pin down both the fail-closed behavior
# and the capability sets quorum derives.


@pytest.fixture
def no_nono(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "nono_py", None)  # import raises ImportError


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

    mod.apply = lambda caps: mod.calls.append({"apply": caps})
    monkeypatch.setitem(sys.modules, "nono_py", mod)
    return mod


def test_self_sandbox_raises_friendly_error_without_nono(home: Path, no_nono):
    with pytest.raises(SandboxUnavailable, match=r"nono-py is not installed"):
        self_sandbox(home, Config())


def test_missing_system_policy_fails_closed(home: Path, fake_nono):
    """A nono-py that cannot supply the system-read baseline must raise, not
    hand back a capability set in which nothing can exec."""
    from quorum.sandbox import build_capabilities

    def boom():
        raise RuntimeError("no embedded policy")

    fake_nono.load_embedded_policy = boom
    with pytest.raises(SandboxUnavailable, match=r"system read policy"):
        build_capabilities(home, Config())


def test_capabilities_reflect_config(home: Path, fake_nono, tmp_path: Path):
    from quorum.projects import ProjectRegistry
    from quorum.sandbox import build_capabilities

    pdir = tmp_path / "proj"
    pdir.mkdir()
    ProjectRegistry(home).add(pdir, name="proj")
    watch = tmp_path / "downloads"
    watch.mkdir()
    config = Config()  # no profile network grant -> network blocked
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


def test_profile_file_grants_are_merged(home: Path, fake_nono, tmp_path: Path):
    """[sandbox].profile_file: the user's own nono-style profile is additive —
    its fs_read/fs_write land in the capability set alongside the derived
    grants, and a non-empty network list keeps the network open."""
    import json

    from quorum.sandbox import build_capabilities

    shared = tmp_path / "shared"
    shared.mkdir()
    secret = tmp_path / "token.txt"
    secret.write_text("t")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "fs_write": [str(shared)],
        "fs_read": [str(secret), str(tmp_path / "missing")],
        "network": ["api.example.com"],
    }))

    config = Config(sandbox=SandboxConfig(use_nono=True, profile_file=str(profile)))
    caps = build_capabilities(home, config)

    assert (str(shared), FakeAccessMode.READ_WRITE) in caps.paths
    assert (str(secret), FakeAccessMode.READ) in caps.files  # files via allow_file
    assert not any("missing" in p for p, _ in caps.paths)  # nonexistent: skipped
    assert (str(home), FakeAccessMode.READ_WRITE) in caps.paths  # derived floor stays
    assert caps.network_blocked is False  # profile's network list keeps it open

    # without a profile, the network is blocked
    caps2 = build_capabilities(home, Config(sandbox=SandboxConfig(use_nono=True)))
    assert caps2.network_blocked is True


def test_profile_file_reaches_task_capabilities(home: Path, fake_nono, tmp_path: Path):
    import json

    from quorum.sandbox import build_task_capabilities
    from quorum.tasks import TaskStore

    shared = tmp_path / "shared"
    shared.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"fs_write": [str(shared)]}))
    config = Config(sandbox=SandboxConfig(use_nono=True, profile_file=str(profile)))
    task = TaskStore(home).add("proj", "x", "fake")
    workdir = tmp_path / "wt"
    workdir.mkdir()

    caps = build_task_capabilities(home, config, task, workdir)
    assert (str(shared), FakeAccessMode.READ_WRITE) in caps.paths
    assert (str(workdir), FakeAccessMode.READ_WRITE) in caps.paths


def test_unreadable_profile_file_fails_closed(home: Path, fake_nono, tmp_path: Path):
    from quorum.sandbox import build_capabilities

    config = Config(sandbox=SandboxConfig(use_nono=True, profile_file=str(tmp_path / "nope.json")))
    with pytest.raises(SandboxUnavailable, match="profile_file"):
        build_capabilities(home, config)

    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    config = Config(sandbox=SandboxConfig(use_nono=True, profile_file=str(bad)))
    with pytest.raises(SandboxUnavailable, match="profile_file"):
        build_capabilities(home, config)
