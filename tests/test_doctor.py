"""`quorum doctor` — every check, passing and failing.

Doctor exists because the rest of quorum fails soft: the whole point is that
it *notices*. So each check gets both halves here — the green path against
the scaffolded conftest home, and the specific broken state it is supposed
to name. The smoke probe is driven for real against tests/bin/fake_harness.py,
including the stream-json inject configuration whose absence caused the
2026-08-30 hang (#24) and a harness that never emits a result at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import doctor, fsio, installed_version
from quorum import home as home_mod
from quorum.cli import app
from quorum.config import (
    CIConfig,
    Config,
    HarnessConfig,
    HerdrConfig,
    NotifyConfig,
    SandboxConfig,
    TasksConfig,
)
from quorum.doctor import NA, OK, PROBLEM
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore, runner_lock_path

runner = CliRunner()

FAKE_HARNESS = Path(__file__).parent / "bin" / "fake_harness.py"
FAKE_GH = Path(__file__).parent / "bin" / "fake_gh.py"

# A pid that is certainly not running: above every platform's pid_max.
DEAD_PID = 2_147_483_647


def statuses(checks: list[doctor.Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def find(checks: list[doctor.Check], name: str) -> doctor.Check:
    match = [c for c in checks if c.name == name]
    assert match, f"no check named {name!r} in {[c.name for c in checks]}"
    return match[0]


def make_repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


def fake_harness_config(
    mode: str = "echo",
    inject: str = "",
    resume: list[str] | None = None,
    name: str = "fake",
    extra_argv: list[str] | None = None,
) -> Config:
    harness = HarnessConfig(
        start=[sys.executable, str(FAKE_HARNESS), *(extra_argv or [])],
        resume=resume or [],
        env={"FAKE_HARNESS_MODE": mode},
        inject=inject,
    )
    return Config(harness={name: harness}, tasks=TasksConfig(default_harness=name))


@pytest.fixture
def bin_without_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding only real git, so `gh` is provably absent until a test
    installs the fake one (the dev machine's real gh would hit the network)."""
    d = tmp_path / "doctorbin"
    d.mkdir()
    git = shutil.which("git")
    assert git, "these tests need git"
    (d / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(d))
    return d


def install_gh(bindir: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "pr") -> None:
    body = FAKE_GH.read_text().split("\n", 1)[1]  # the shebang must be this interpreter
    shim = bindir / "gh"
    shim.write_text(f"#!{sys.executable}\n{body}")
    shim.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_MODE", mode)


# -- home and config ---------------------------------------------------------


def test_home_check_sees_an_initialized_home(home: Path):
    assert doctor.check_home(home).status == OK


def test_home_check_names_init_when_there_is_no_home(tmp_path: Path):
    check = doctor.check_home(tmp_path / "nowhere")
    assert check.status == PROBLEM
    assert "quorum init" in check.fix


def test_config_check_parses_strictly(home: Path):
    check, config = doctor.check_config(home)
    assert check.status == OK
    assert config is not None


def test_config_check_refuses_to_paper_over_a_broken_config(home: Path):
    """Every other reader falls back to defaults here — this is the one place
    the user is told the file is broken."""
    (home / "config.toml").write_text("[tasks\nbroken = ", encoding="utf-8")
    check, config = doctor.check_config(home)
    assert check.status == PROBLEM
    assert config is None
    assert "does not load" in check.summary
    # and nothing downstream is reported on a config nobody could read
    names = [c.name for c in doctor.run_checks(home)]
    assert names == ["home", "config", "git"]


# -- harnesses ---------------------------------------------------------------


def test_harness_binary_check_resolves_argv0():
    harness = HarnessConfig(start=[sys.executable, "-c", "pass"])
    assert doctor.check_harness_binary("fake", harness).status == OK


def test_harness_binary_check_flags_a_missing_binary():
    check = doctor.check_harness_binary("ghost", HarnessConfig(start=["no-such-binary-xyz"]))
    assert check.status == PROBLEM
    assert "not found on PATH" in check.summary


def test_harness_template_check_accepts_a_prompt_placeholder():
    check = doctor.check_harness_template("c", HarnessConfig(start=["cli", "{prompt}"]))
    assert check.status == OK
    assert "{prompt} in argv" in check.summary


def test_harness_template_check_accepts_an_inject_harness():
    harness = HarnessConfig(
        start=["cli", "--input-format", "stream-json", "{prompt}"], inject="stream-json"
    )
    assert doctor.check_harness_template("c", harness).status == OK


def test_harness_template_check_catches_stream_json_without_inject():
    """The 2026-08-30 outage, statically: a stream-json CLI ignores an argv
    prompt entirely, so every run hangs until something times it out."""
    harness = HarnessConfig(start=["claude", "-p", "{prompt}", "--input-format", "stream-json"])
    check = doctor.check_harness_template("claude", harness)
    assert check.status == PROBLEM
    assert "inject" in check.summary
    assert 'inject = "stream-json"' in check.fix


def test_harness_template_check_catches_resume_without_session():
    harness = HarnessConfig(start=["cli", "{prompt}"], resume=["cli", "--resume", "{prompt}"])
    check = doctor.check_harness_template("cli", harness)
    assert check.status == PROBLEM
    assert "{session}" in check.summary


def test_harness_template_check_is_neutral_about_an_appended_prompt():
    check = doctor.check_harness_template("cli", HarnessConfig(start=["cli", "run"]))
    assert check.status == NA
    assert "appends the prompt" in check.summary


def test_harnesses_check_reads_a_fresh_home_as_undecided_not_broken():
    """One unmade decision must not read as two failures: `quorum init`
    leaves exactly this state, and a fresh home has nothing wrong with it."""
    checks = doctor.check_harnesses(Config())
    assert [c.status for c in checks] == [NA]
    assert "no harness configured yet" in checks[0].summary


def test_harnesses_check_flags_a_default_naming_a_table_that_is_not_there():
    """The same emptiness *is* a fault once the config contradicts itself."""
    config = Config(tasks=TasksConfig(default_harness="claude"))
    checks = doctor.check_harnesses(config)
    assert checks[0].status == PROBLEM
    assert "no [harness.<name>] table at all" in checks[0].summary


def test_default_harness_check_covers_unset_unknown_and_good():
    config = fake_harness_config()
    assert doctor.check_default_harness(config).status == OK

    config.tasks.default_harness = ""
    assert doctor.check_default_harness(config).status == PROBLEM

    config.tasks.default_harness = "nope"
    check = doctor.check_default_harness(config)
    assert check.status == PROBLEM
    assert "names no [harness.nope] table" in check.summary


# -- git and projects --------------------------------------------------------


def test_git_check_finds_git():
    assert doctor.check_git().status == OK


def test_git_check_flags_a_missing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    check = doctor.check_git()
    assert check.status == PROBLEM
    assert "not on PATH" in check.summary


def test_projects_check_is_quiet_about_an_empty_registry(home: Path):
    checks = doctor.check_projects(home)
    assert [c.status for c in checks] == [NA]


def test_projects_check_passes_a_registered_repo(home: Path, tmp_path: Path):
    repo = make_repo(tmp_path / "proj")
    ProjectRegistry(home).add(repo, name="proj")
    assert [c.status for c in doctor.check_projects(home)] == [OK]


def test_projects_check_flags_a_vanished_directory(home: Path, tmp_path: Path):
    repo = make_repo(tmp_path / "gone")
    ProjectRegistry(home).add(repo, name="gone")
    shutil.rmtree(repo)
    check = doctor.check_projects(home)[0]
    assert check.status == PROBLEM
    assert "does not exist" in check.summary


def test_projects_check_notes_a_non_git_directory(home: Path, tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    ProjectRegistry(home).add(plain, name="plain")
    check = doctor.check_projects(home)[0]
    assert check.status == NA
    assert "--no-worktree" in check.fix


# -- optional integrations ---------------------------------------------------


def test_gh_check_is_quiet_when_ci_is_off(home: Path):
    check = doctor.check_gh(home, Config(ci=CIConfig(enabled=False)))
    assert check.status == NA


def test_gh_check_is_quiet_when_gh_is_absent(home: Path, bin_without_gh: Path):
    """No gh is a choice, not a fault — the probe advertises that it silently
    does nothing."""
    check = doctor.check_gh(home, Config())
    assert check.status == NA
    assert "not on PATH" in check.summary


def test_gh_check_passes_when_authenticated(home: Path, bin_without_gh: Path, monkeypatch):
    install_gh(bin_without_gh, monkeypatch)
    assert doctor.check_gh(home, Config()).status == OK


def test_gh_check_flags_an_unauthenticated_gh(home: Path, bin_without_gh: Path, monkeypatch):
    """The trap: gh looks configured, and every `ci:` line silently vanishes."""
    install_gh(bin_without_gh, monkeypatch, mode="unauth")
    check = doctor.check_gh(home, Config())
    assert check.status == PROBLEM
    assert "not authenticated" in check.summary
    assert "gh auth login" in check.fix


def test_gh_check_calls_gh_only_through_the_forge_module(home: Path, monkeypatch):
    """forge.py is the one module that shells out to a forge CLI; doctor asks
    it, so the probe's own [ci] settings decide how the question is put."""
    calls: list[Path] = []

    def fake_auth_status(target: Path) -> bool:
        calls.append(Path(target))
        return True

    monkeypatch.setattr("quorum.forge.auth_status", fake_auth_status)
    monkeypatch.setattr(shutil, "which", lambda exe: f"/usr/bin/{exe}")
    assert doctor.check_gh(home, Config()).status == OK
    assert calls == [home]


def test_gh_check_reads_an_offline_gh_as_unknown_not_broken(
    home: Path, bin_without_gh: Path, monkeypatch
):
    """A laptop on a plane is not a misconfigured home: gh that never
    answered says nothing about auth, so it is a `–` and exits 0."""
    (home / "config.toml").write_text("[ci]\ntimeout_seconds = 0.5\n", encoding="utf-8")
    install_gh(bin_without_gh, monkeypatch, mode="hang")
    check = doctor.check_gh(home, Config(ci=CIConfig(timeout_seconds=0.5)))
    assert check.status == NA
    assert "unknown" in check.summary


def test_herdr_check_is_quiet_without_the_table():
    assert doctor.check_herdr(Config()).status == NA


def test_herdr_check_passes_with_a_socket(tmp_path: Path):
    sock = tmp_path / "herdr.sock"
    sock.write_text("")
    config = Config(herdr=HerdrConfig(socket=str(sock)))
    assert doctor.check_herdr(config).status == OK


def test_herdr_check_flags_an_enabled_but_absent_socket(tmp_path: Path):
    config = Config(herdr=HerdrConfig(socket=str(tmp_path / "nope.sock")))
    check = doctor.check_herdr(config)
    assert check.status == PROBLEM
    assert "does not exist" in check.summary


def _fake_nono(monkeypatch: pytest.MonkeyPatch, supported: bool):
    mod = types.ModuleType("nono_py")
    mod.is_supported = lambda: supported
    mod.support_info = lambda: types.SimpleNamespace(details="no Landlock here")
    monkeypatch.setitem(sys.modules, "nono_py", mod)


def test_notify_check_is_quiet_without_the_table():
    check = doctor.check_notify(Config())
    assert check.status == NA
    assert "reach no one" in check.summary


def test_notify_check_passes_with_a_runnable_template():
    config = Config(notify=NotifyConfig(command=[sys.executable, "-c", "{text}"]))
    check = doctor.check_notify(config)
    assert check.status == OK
    assert "attention" in check.summary


def test_notify_check_flags_a_binary_that_is_not_there(tmp_path: Path):
    config = Config(notify=NotifyConfig(command=[str(tmp_path / "nope"), "{text}"]))
    check = doctor.check_notify(config)
    assert check.status == PROBLEM
    assert "not on PATH" in check.summary
    assert "[notify].command" in check.fix


def test_notify_check_notes_a_template_without_text():
    config = Config(notify=NotifyConfig(command=[sys.executable, "-c", "pass"]))
    check = doctor.check_notify(config)
    assert check.status == NA
    assert "appends the text" in check.summary
    assert "{text}" in check.fix


def test_sandbox_check_is_quiet_when_unused():
    assert doctor.check_sandbox(Config()).status == NA


def test_sandbox_check_passes_with_a_supported_nono(monkeypatch: pytest.MonkeyPatch):
    _fake_nono(monkeypatch, supported=True)
    config = Config(sandbox=SandboxConfig(use_nono=True))
    assert doctor.check_sandbox(config).status == OK


def test_sandbox_check_flags_a_missing_nono(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "nono_py", None)  # import raises ImportError
    config = Config(sandbox=SandboxConfig(use_nono=True))
    check = doctor.check_sandbox(config)
    assert check.status == PROBLEM
    assert "nono-py is not installed" in check.summary


def test_sandbox_check_flags_an_unsupported_platform(monkeypatch: pytest.MonkeyPatch):
    _fake_nono(monkeypatch, supported=False)
    config = Config(sandbox=SandboxConfig(use_nono=True))
    check = doctor.check_sandbox(config)
    assert check.status == PROBLEM
    assert "no Landlock here" in check.summary


# -- prompts -----------------------------------------------------------------


def test_prompts_check_is_green_on_a_fresh_scaffold(home: Path):
    checks = doctor.check_prompts(home)
    assert checks and all(c.status == OK for c in checks)


def test_prompts_check_flags_an_unedited_older_default(home: Path, monkeypatch):
    """The overlay problem: an upgrade left a stale seed, so this home is
    quietly running last release's policy."""
    stale = "an older packaged default\n"
    (home / "prompts" / "manager.md").write_text(stale, encoding="utf-8")
    record = fsio.read_json(home_mod.seeded_record_path(home))
    record["manager.md"] = hashlib.sha256(stale.encode()).hexdigest()
    fsio.atomic_write_json(home_mod.seeded_record_path(home), record)
    check = find(doctor.check_prompts(home), "prompts.manager")
    assert check.status == PROBLEM
    assert "quorum init" in check.fix


def test_prompts_check_leaves_an_edited_prompt_alone(home: Path):
    (home / "prompts" / "manager.md").write_text("my own supervision policy\n", encoding="utf-8")
    check = find(doctor.check_prompts(home), "prompts.manager")
    assert check.status == NA
    assert "edited" in check.summary


def test_prompts_check_lists_a_local_overlay_without_judging_it(home: Path):
    """An overlay is additive by construction: reported so a prompt that does
    not read like the file on disk is explicable, never a ✗."""
    (home / "prompts" / "manager.local.md").write_text("also: be terse\n", encoding="utf-8")
    checks = doctor.check_prompts(home)
    overlay = find(checks, "prompts.manager.local")
    assert overlay.status == NA
    assert "overlays" in overlay.summary
    # and the base prompt is still classified exactly as home.py sees it
    assert find(checks, "prompts.manager").status == OK
    assert all(c.status != PROBLEM for c in checks)


def test_prompts_check_agrees_with_home_classification(home: Path):
    """Doctor and `quorum init`/`prompt list` must never disagree about what
    'edited' means — same function, so they cannot."""
    (home / "prompts" / "manager.md").write_text("mine\n", encoding="utf-8")
    states = home_mod.classify_prompts(home)
    assert states["manager.md"] == "edited"
    assert "edited" in find(doctor.check_prompts(home), "prompts.manager").summary


def test_prompts_check_notes_an_unseeded_prompt(home: Path):
    (home / "prompts" / "manager.md").unlink()
    check = find(doctor.check_prompts(home), "prompts.manager")
    assert check.status == NA
    assert "not seeded" in check.summary


# -- state hygiene -----------------------------------------------------------


def write_lock(home: Path, pid: int, version: str | None = None, age_seconds: float = 0.0):
    payload = {"role": "supervisor", "pid": pid, "started_at": "2026-08-31T00:00:00Z"}
    if version is not None:
        payload["version"] = version
    lock = home / "supervisor.lock"
    lock.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    if age_seconds:
        import time

        stamp = time.time() - age_seconds
        os.utime(lock, (stamp, stamp))
    return lock


def test_supervisor_check_is_quiet_when_nothing_runs(home: Path):
    assert doctor.check_supervisor(home).status == NA


def test_supervisor_check_passes_for_a_live_lock(home: Path):
    write_lock(home, pid=1)  # pid 1 is always alive, and never this process
    assert doctor.check_supervisor(home).status == OK


def test_supervisor_check_flags_a_stale_lock(home: Path):
    write_lock(home, pid=DEAD_PID)
    check = doctor.check_supervisor(home)
    assert check.status == PROBLEM
    assert "stale supervisor.lock" in check.summary


def test_supervisor_check_flags_a_wedged_scheduler(home: Path):
    """Alive pid, but the 60s lock heartbeat stopped: the process is up and
    its scheduler is not."""
    write_lock(home, pid=1, age_seconds=3600)
    check = doctor.check_supervisor(home)
    assert check.status == PROBLEM
    assert "wedged" in check.summary


def test_version_check_is_quiet_without_a_supervisor(home: Path):
    assert doctor.check_version(home).status == NA


def test_version_check_passes_when_versions_match(home: Path):
    write_lock(home, pid=1, version=installed_version())
    assert doctor.check_version(home).status == OK


def test_version_check_flags_an_upgrade_that_was_never_restarted(home: Path):
    write_lock(home, pid=1, version="0.0.1-ancient")
    check = doctor.check_version(home)
    assert check.status == PROBLEM
    assert "still executing the old code" in check.summary
    assert "quorum down" in check.fix


def test_version_check_tolerates_a_supervisor_that_recorded_none(home: Path):
    write_lock(home, pid=1)
    check = doctor.check_version(home)
    assert check.status == NA
    assert "recorded no version" in check.summary


def test_runner_lock_check_is_green_with_no_tasks(home: Path):
    assert [c.status for c in doctor.check_runner_locks(home)] == [OK]


def test_runner_lock_check_ignores_a_live_run(home: Path):
    task = TaskStore(home).add(project="p", prompt="x", harness="fake")
    runner_lock_path(home, task.id).write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert [c.status for c in doctor.check_runner_locks(home)] == [OK]


def test_runner_lock_check_flags_an_orphan(home: Path):
    task = TaskStore(home).add(project="p", prompt="x", harness="fake")
    runner_lock_path(home, task.id).write_text(json.dumps({"pid": DEAD_PID}), encoding="utf-8")
    check = doctor.check_runner_locks(home)[0]
    assert check.status == PROBLEM
    assert task.short_id in check.summary


def test_stale_claim_check_is_green_on_a_quiet_home(home: Path):
    assert doctor.check_stale_claims(home).status == OK


def test_stale_claim_check_ignores_a_fresh_claim(home: Path):
    bus = MessageBus(home)
    bus.send("user", "task-1", text="hello")
    for _claimed in bus.claim("task-1"):
        pass  # claimed into cur/, not acked — but only a moment ago
    assert doctor.check_stale_claims(home).status == OK


def test_stale_claim_check_flags_a_crashed_consumer(home: Path):
    import time

    bus = MessageBus(home)
    bus.send("user", "task-1", text="hello")
    for claimed in bus.claim("task-1"):
        stamp = time.time() - 7200
        os.utime(claimed.path, (stamp, stamp))
    check = doctor.check_stale_claims(home)
    assert check.status == PROBLEM
    assert "task-1" in check.summary


def write_heartbeat(home: Path, name: str, **fields) -> None:
    path = home / "state" / "agents" / name / "heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_heartbeat_check_notes_an_agent_that_never_ran(home: Path):
    config = Config(agents=Config().agents)
    from quorum.config import AgentConfig

    config.agents["manager"] = AgentConfig(type="manager", schedule="every 5m")
    check = find(doctor.check_heartbeats(home, config), "agent.manager")
    assert check.status == NA
    assert "never ran" in check.summary


def test_heartbeat_check_passes_a_healthy_agent(home: Path):
    from quorum.config import AgentConfig

    config = Config(agents={"manager": AgentConfig(type="manager")})
    write_heartbeat(home, "manager", status="idle", consecutive_failures=0)
    assert find(doctor.check_heartbeats(home, config), "agent.manager").status == OK


def test_heartbeat_check_flags_a_failure_streak(home: Path):
    from quorum.config import AgentConfig

    config = Config(agents={"manager": AgentConfig(type="manager")})
    write_heartbeat(home, "manager", status="error", consecutive_failures=4, error="harness died")
    check = find(doctor.check_heartbeats(home, config), "agent.manager")
    assert check.status == PROBLEM
    assert "4 consecutive failure(s)" in check.summary
    assert "harness died" in check.summary


def test_heartbeat_check_treats_a_pause_as_deliberate(home: Path):
    from quorum.config import AgentConfig

    config = Config(agents={"manager": AgentConfig(type="manager")})
    write_heartbeat(home, "manager", status="paused", error="paused by user")
    check = find(doctor.check_heartbeats(home, config), "agent.manager")
    assert check.status == NA
    assert "quorum agent resume manager" in check.fix


# -- the smoke probe ---------------------------------------------------------


def test_smoke_drives_a_stream_json_inject_harness_end_to_end(home: Path):
    """The #24 shape, exercised for real: the prompt travels over stdin as a
    user turn, the harness answers with a result event, and the pump closes
    stdin so the run ends by itself."""
    config = fake_harness_config(
        mode="inject", inject="stream-json", extra_argv=["--input-format", "stream-json"]
    )
    checks = doctor.smoke_checks(home, config, timeout=30.0)
    assert statuses(checks) == {
        "smoke.fake.run": OK,
        "smoke.fake.result": OK,
        "smoke.fake.session": OK,
    }
    assert "sess-fake-123" in find(checks, "smoke.fake.session").summary


def test_smoke_flags_a_harness_that_never_emits_a_result(home: Path):
    """A harness quorum cannot tell has finished: the run looks fine and the
    manager never learns a turn ended (and no usage is ever recorded)."""
    config = fake_harness_config(mode="echo")
    checks = doctor.smoke_checks(home, config, timeout=30.0)
    assert find(checks, "smoke.fake.run").status == OK
    result = find(checks, "smoke.fake.result")
    assert result.status == PROBLEM
    assert "no result event" in result.summary
    assert 'inject = "stream-json"' in result.fix


def test_smoke_kills_and_reports_a_hanging_harness(home: Path):
    config = fake_harness_config(mode="hang")
    checks = doctor.smoke_checks(home, config, timeout=1.0)
    run = find(checks, "smoke.fake.run")
    assert run.status == PROBLEM
    assert "killed" in run.summary
    assert find(checks, "smoke.fake.result").status == PROBLEM


def test_smoke_kills_the_whole_process_tree_not_just_the_wrapper(home: Path, tmp_path: Path):
    """Harnesses wrap themselves (a node shim, a shell that execs the real
    binary). Killing only the process we spawned leaves the grandchild
    running and holding the stdout pipe, so the probe overshoots its own
    budget — the child gets its own session and the timeout kills the group."""
    import time

    from quorum import fsio

    pidfile = tmp_path / "grandchild.pid"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    config = Config(
        harness={"wrap": HarnessConfig(start=[sys.executable, str(wrapper), str(pidfile)])},
        tasks=TasksConfig(default_harness="wrap"),
    )
    started = time.monotonic()
    checks = doctor.smoke_checks(home, config, timeout=2.0)
    assert find(checks, "smoke.wrap.run").status == PROBLEM
    assert time.monotonic() - started < 20  # not the grandchild's 120s

    grandchild = int(pidfile.read_text())
    for _ in range(40):  # the kill is a signal, so give the exit a moment
        if not fsio.pid_alive(grandchild):
            break
        time.sleep(0.1)
    assert not fsio.pid_alive(grandchild), "the orphaned grandchild outlived the smoke run"


def test_smoke_never_points_the_harness_at_the_real_home(home: Path, tmp_path: Path):
    """The child inherits the environment, and a harness with quorum's own
    integration hooks installed runs `quorum task hook-session-start` on
    startup. It must land in a throwaway home, never the live one."""
    seen = tmp_path / "seen-home"
    reporter = tmp_path / "reporter.py"
    reporter.write_text(
        "import os, sys\n"
        "open(sys.argv[1], 'w').write(os.environ.get('QUORUM_HOME', ''))\n",
        encoding="utf-8",
    )
    config = Config(
        harness={"env": HarnessConfig(start=[sys.executable, str(reporter), str(seen)])},
        tasks=TasksConfig(default_harness="env"),
    )
    doctor.smoke_checks(home, config, timeout=30.0)
    child_home = Path(seen.read_text())
    assert child_home != home
    assert home not in child_home.parents
    assert child_home.parent.name.startswith("quorum-doctor-")  # the probe's own scratch
    assert not child_home.exists()  # and it went away with the TemporaryDirectory


def test_smoke_flags_a_failing_harness(home: Path):
    config = fake_harness_config(mode="fail")
    check = find(doctor.smoke_checks(home, config, timeout=30.0), "smoke.fake.run")
    assert check.status == PROBLEM
    assert "exited 3" in check.summary


def test_smoke_wants_a_session_id_only_when_resume_needs_one(home: Path):
    """A harness that reports no session id is fine — until a resume template
    promises to reuse one."""
    config = fake_harness_config(mode="fail", resume=[sys.executable, "{session}"])
    check = find(doctor.smoke_checks(home, config, timeout=30.0), "smoke.fake.session")
    assert check.status == PROBLEM

    config = fake_harness_config(mode="fail")
    assert find(doctor.smoke_checks(home, config, timeout=30.0), "smoke.fake.session").status == NA


def test_smoke_names_an_unknown_harness(home: Path):
    checks = doctor.smoke_checks(home, fake_harness_config(), "ghost", timeout=5.0)
    assert checks[0].status == PROBLEM
    assert "no [harness.ghost] table" in checks[0].summary


def test_smoke_needs_a_harness_to_test(home: Path):
    checks = doctor.smoke_checks(home, Config(), timeout=5.0)
    assert checks[0].status == PROBLEM
    assert "--smoke <name>" in checks[0].fix


def test_smoke_writes_nothing_into_the_home(home: Path):
    """Doctor is a pure reader; the one active probe runs in scratch space."""
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    doctor.smoke_checks(home, fake_harness_config(mode="echo"), timeout=30.0)
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before


# -- the command -------------------------------------------------------------


def configure(home: Path, mode: str = "echo", inject: str = "") -> None:
    """A config.toml with a working fake harness and the CI probe off (the
    machine running the tests may well have an unauthenticated gh)."""
    cfg = home / "config.toml"
    text = cfg.read_text()
    text = text.replace('default_harness = ""', 'default_harness = "fake"')
    text = text.replace("[ci]\nenabled = true", "[ci]\nenabled = false")
    inject_line = f'inject = "{inject}"\n' if inject else ""
    text += (
        f"\n[harness.fake]\n"
        f'start = ["{sys.executable}", "{FAKE_HARNESS}"]\n'
        f"{inject_line}"
        f'[harness.fake.env]\nFAKE_HARNESS_MODE = "{mode}"\n'
    )
    cfg.write_text(text, encoding="utf-8")


def test_doctor_command_is_green_and_silent_about_the_inapplicable(home: Path):
    configure(home)
    result = runner.invoke(app, ["doctor", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "all checks passed" in result.output
    assert "✗" not in result.output


def disable_ci(home: Path) -> None:
    """The machine running the tests may well have an unauthenticated gh."""
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("[ci]\nenabled = true", "[ci]\nenabled = false"))


def test_doctor_command_is_green_on_a_freshly_initialized_home(home: Path):
    """`quorum init` then `quorum doctor` must not greet a new user with red:
    there is no harness yet, which is a decision they have not made, not a
    fault. One line says so, and the command exits 0."""
    disable_ci(home)
    result = runner.invoke(app, ["doctor", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "no harness configured yet" in result.output
    assert "✗" not in result.output
    # one line about it, not two: the default_harness line is the same news
    assert "default_harness is unset" not in result.output


def test_doctor_command_exits_nonzero_on_any_problem(home: Path):
    disable_ci(home)
    (home / "config.toml").write_text(
        (home / "config.toml").read_text().replace('default_harness = ""', 'default_harness = "x"')
    )
    result = runner.invoke(app, ["doctor", "--home", str(home)])
    assert result.exit_code == 1
    assert "no [harness.<name>] table at all" in result.output
    assert "problem(s)" in result.output


def test_doctor_command_says_so_when_a_broken_config_skips_the_smoke(home: Path):
    """A `--smoke` that silently never ran reads exactly like one that
    passed."""
    (home / "config.toml").write_text("[tasks\nbroken = ", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--smoke", "--home", str(home)])
    assert result.exit_code == 1
    assert "smoke skipped" in result.output


def test_doctor_command_emits_json(home: Path):
    configure(home)
    result = runner.invoke(app, ["doctor", "--json", "--home", str(home)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["home"] == str(home)
    assert payload["problems"] == 0
    assert {"name", "status", "summary", "fix"} == set(payload["checks"][0])
    assert any(c["name"] == "harness.fake.binary" for c in payload["checks"])


def test_doctor_command_runs_the_smoke_probe_on_request(home: Path):
    configure(home, mode="inject", inject="stream-json")
    result = runner.invoke(
        app, ["doctor", "--json", "--smoke", "--smoke-timeout", "30", "--home", str(home)]
    )
    payload = json.loads(result.output)
    smoke = {c["name"]: c["status"] for c in payload["checks"] if c["name"].startswith("smoke.")}
    assert smoke == {
        "smoke.fake.run": OK,
        "smoke.fake.result": OK,
        "smoke.fake.session": OK,
    }
    assert result.exit_code == 0


def test_doctor_command_takes_a_named_harness_for_the_smoke_run(home: Path):
    configure(home)
    result = runner.invoke(app, ["doctor", "--json", "--smoke", "ghost", "--home", str(home)])
    payload = json.loads(result.output)
    assert any(c["name"] == "smoke.ghost" for c in payload["checks"])
    assert result.exit_code == 1


def test_doctor_command_skips_the_probe_by_default(home: Path):
    configure(home)
    result = runner.invoke(app, ["doctor", "--json", "--home", str(home)])
    assert not [c for c in json.loads(result.output)["checks"] if c["name"].startswith("smoke")]


def test_status_points_at_doctor_when_an_agent_is_failing(home: Path):
    write_heartbeat(home, "manager", status="error", error="harness died", consecutive_failures=3)
    result = runner.invoke(app, ["status", "--home", str(home)])
    assert "`quorum doctor`" in result.output
