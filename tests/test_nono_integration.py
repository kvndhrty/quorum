"""Real nono-py enforcement tests (no mocks).

test_sandbox.py pins down quorum's glue against a fake nono_py; these tests
run the genuine sandbox and assert the kernel actually enforces the derived
capability set. They need nono-py installed ([nono] extra) and a supported
platform (Linux >= 5.13 with Landlock enabled, or macOS Seatbelt), so they
self-skip elsewhere. CI runs them in a dedicated job that fails if support
is missing, so a runner regression can't silently skip them.
"""

from __future__ import annotations

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


def sh(caps, script: str):
    return nono_py.sandboxed_exec(caps, ["/bin/sh", "-c", script], timeout_secs=30)


def test_write_allowed_inside_home(home: Path):
    caps = build_capabilities(home, Config())
    target = home / "state" / "probe.txt"
    result = sh(caps, f"echo enforced > {target}")
    assert result.exit_code == 0, result.stderr
    assert target.read_text().strip() == "enforced"


def test_write_denied_outside_capabilities(home: Path, tmp_path: Path):
    caps = build_capabilities(home, Config())
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "denied.txt"
    result = sh(caps, f"echo escaped > {target}")
    assert result.exit_code != 0
    assert not target.exists()


def test_project_dirs_are_readonly(home: Path, tmp_path: Path):
    pdir = tmp_path / "proj"
    pdir.mkdir()
    (pdir / "notes.txt").write_text("readable")
    ProjectRegistry(home).add(pdir, name="proj")
    caps = build_capabilities(home, Config())

    read = sh(caps, f"cat {pdir / 'notes.txt'}")
    assert read.exit_code == 0 and read.stdout.strip() == "readable"

    write = sh(caps, f"echo tamper > {pdir / 'tampered.txt'}")
    assert write.exit_code != 0
    assert not (pdir / "tampered.txt").exists()


def test_runner_stdin_roundtrip_under_real_sandbox(home: Path):
    """Mode 3 glue end-to-end: stdin prompt staged in QUORUM_HOME, redirected
    via /bin/sh, executed inside the real sandbox, staging cleaned up."""
    config = Config(llm=LLMConfig(executable="/bin/cat"))
    runner = make_sandboxed_runner(home, config)
    proc = runner(["/bin/cat"], input="real sandbox prompt", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "real sandbox prompt"
    assert not list((home / "state" / "llm").glob("prompt-*"))


def test_self_sandbox_enforces_in_subprocess(home: Path, tmp_path: Path):
    """Mode 2: nono_py.apply() is irreversible for the calling process, so we
    exercise it in a child interpreter and check enforcement from inside."""
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
        (home / "state" / "self-probe.txt").write_text("ok")
        try:
            (outside / "denied.txt").write_text("escaped")
        except OSError:
            sys.exit(0)
        sys.exit(3)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(home), str(outside)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stderr}"
    assert (home / "state" / "self-probe.txt").read_text() == "ok"
    assert not (outside / "denied.txt").exists()
