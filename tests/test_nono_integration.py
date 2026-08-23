"""Real nono-py enforcement tests (no mocks).

test_sandbox.py pins down quorum's glue against a fake nono_py; these tests
run the genuine sandbox and assert the kernel actually enforces the derived
capability set. They need nono-py installed ([nono] extra) and a supported
platform (Linux >= 5.13 with Landlock enabled, or macOS Seatbelt), so they
self-skip elsewhere. CI runs them in a dedicated job that fails if support
is missing, so a runner regression can't silently skip them.

Commands here always name binaries by absolute path. PATH lookup makes the
shell stat directories that may not be granted, so a bare `cat` can fail with
"command not found" (127) even where exec itself is permitted.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

nono_py = pytest.importorskip("nono_py")

if not nono_py.is_supported():
    pytest.skip(
        f"nono sandbox unsupported here: {nono_py.support_info().details}",
        allow_module_level=True,
    )

pytestmark = pytest.mark.nono_integration

from quorum.config import Config, LLMConfig
from quorum.projects import ProjectRegistry
from quorum.sandbox import build_capabilities, make_sandboxed_runner

CAT = shutil.which("cat") or "/bin/cat"


def sh(caps, script: str, cwd: Path | None = None):
    """Run a shell snippet in the sandbox, with stdout/stderr decoded.

    cwd is pinned inside the capability set; sandboxed_exec otherwise inherits
    the parent's directory and the shell reports getcwd() failures on stderr.
    """
    result = nono_py.sandboxed_exec(
        caps, ["/bin/sh", "-c", script], cwd=str(cwd) if cwd else None, timeout_secs=30
    )
    return result.exit_code, _text(result.stdout), _text(result.stderr)


def _text(stream) -> str:
    return stream.decode("utf-8", "replace") if isinstance(stream, bytes) else (stream or "")


def test_write_allowed_inside_home(home: Path):
    caps = build_capabilities(home, Config())
    target = home / "state" / "probe.txt"
    exit_code, _, stderr = sh(caps, f"echo enforced > {target}", cwd=home)
    assert exit_code == 0, stderr
    assert target.read_text().strip() == "enforced"


def test_system_binaries_are_executable(home: Path):
    """The capability set must carry nono's system-read baseline: without it a
    sandboxed child cannot exec at all (Linux: `nono: exec failed: Permission
    denied`; macOS: the dynamic loader cannot be opened), which silently
    disables the LLM in mode 3 and kills the supervisor in mode 2."""
    caps = build_capabilities(home, Config())
    probe = home / "state" / "echo-me.txt"
    probe.write_text("executed")
    result = nono_py.sandboxed_exec(caps, [CAT, str(probe)], cwd=str(home), timeout_secs=30)
    assert result.exit_code == 0, _text(result.stderr)
    assert _text(result.stdout).strip() == "executed"


def test_write_denied_outside_capabilities(home: Path, tmp_path: Path):
    caps = build_capabilities(home, Config())
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "denied.txt"
    exit_code, _, _ = sh(caps, f"echo escaped > {target}", cwd=home)
    assert exit_code != 0
    assert not target.exists()


def test_project_dirs_are_readonly(home: Path, tmp_path: Path):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    (pdir / "notes.txt").write_text("readable")
    ProjectRegistry(home).add(pdir, name="proj")
    caps = build_capabilities(home, Config())

    exit_code, stdout, stderr = sh(caps, f"{CAT} {pdir / 'notes.txt'}", cwd=home)
    assert exit_code == 0, stderr
    assert stdout.strip() == "readable"

    write_code, _, _ = sh(caps, f"echo tamper > {pdir / 'tampered.txt'}", cwd=home)
    assert write_code != 0
    assert not (pdir / "tampered.txt").exists()


def test_runner_stdin_roundtrip_under_real_sandbox(home: Path):
    """Mode 3 glue end-to-end: stdin prompt staged in QUORUM_HOME, redirected
    via /bin/sh, executed inside the real sandbox, staging cleaned up."""
    config = Config(llm=LLMConfig(executable=CAT))
    runner = make_sandboxed_runner(home, config)
    proc = runner([CAT], input="real sandbox prompt", timeout=30)
    assert proc.returncode == 0, proc.stderr
    # CliBackend is handed this as a subprocess.run stand-in with text=True.
    assert isinstance(proc.stdout, str) and isinstance(proc.stderr, str)
    assert proc.stdout == "real sandbox prompt"
    assert not list((home / "state" / "llm").glob("prompt-*"))


def test_llm_backend_completes_under_real_sandbox(home: Path, tmp_path: Path):
    """The whole mode 3 stack: LLMClient -> CliBackend -> sandboxed runner.
    Guards the silent-degradation path, where an exec or decode failure is
    swallowed into a None completion and agents quietly lose their LLM."""
    from quorum.config import SandboxConfig
    from quorum.llm import LLMClient

    exe = tmp_path / "fake-llm.sh"
    exe.write_text("#!/bin/sh\nread -r line\necho \"echoed: $line\"\n")
    exe.chmod(0o755)
    llm_cfg = LLMConfig(executable=str(exe))
    config = Config(llm=llm_cfg, sandbox=SandboxConfig(use_nono=True))
    client = LLMClient.from_config(
        llm_cfg, home=home, sandbox_config=config.sandbox, full_config=config
    )
    assert client.complete("hello sandbox") == "echoed: hello sandbox"


def test_self_sandbox_enforces_in_subprocess(home: Path, tmp_path: Path):
    """Mode 2: nono_py.apply() is irreversible for the calling process, so we
    exercise it in a child interpreter and check enforcement from inside.

    Also covers the lazy imports that made mode 2 unusable: builtin agents and
    APScheduler's trigger plugins are both imported *after* apply(), so the
    interpreter's own tree has to stay readable.
    """
    outside = tmp_path / "outside2"
    outside.mkdir()
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from quorum.config import Config
        from quorum.sandbox import self_sandbox

        home, outside = Path(sys.argv[1]), Path(sys.argv[2])
        self_sandbox(home, Config())

        # Imported only now, i.e. under the sandbox.
        from quorum.registry import resolve
        for name in ("tracker", "sentinel", "steward", "scribe", "scout"):
            resolve(name, home)
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(lambda: None, id="i", trigger="interval", seconds=30)
        scheduler.add_job(
            lambda: None, id="c", trigger="cron",
            minute="0", hour="8", day="*", month="*", day_of_week="*",
        )

        (home / "state" / "self-probe.txt").write_text("ok")
        try:
            (outside / "denied.txt").write_text("escaped")
        except OSError:
            sys.exit(0)
        sys.exit(3)
        """
    )
    # cwd inside the capability set: `python -c` puts the working directory on
    # sys.path, so an ungranted cwd would fail the import scan for reasons the
    # real console script (whose sys.path[0] is the granted venv bin) never hits.
    proc = subprocess.run(
        [sys.executable, "-c", script, str(home), str(outside)],
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stderr}"
    assert (home / "state" / "self-probe.txt").read_text() == "ok"
    assert not (outside / "denied.txt").exists()
