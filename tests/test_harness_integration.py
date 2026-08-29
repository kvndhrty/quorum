"""Real-harness adoption tests: the shipped adapters against the actual
codex / opencode binaries, spending real model tokens.

Opt-in twice over: each test self-skips when its binary is missing, and all
of them skip unless QUORUM_HARNESS_TESTS=1 — a plain `uv run pytest` must
never silently spend money. Run them with:

    QUORUM_HARNESS_TESTS=1 uv run pytest -m "codex_integration or opencode_integration" -v

They assert on quorum's file state (liveness written, session id learned,
guidance consumed = the hook delivered it), not on what the model said —
model compliance is its own business.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum.cli import app
from quorum.messages import MessageBus
from quorum.tasks import TaskStore, attached_state, inbox_name
from test_tasks import make_repo

INTEGRATIONS = Path(__file__).parent.parent / "integrations"
LIVE_EVENTS = {"session-start", "stop", "session-end"}

runner = CliRunner()


def require(binary: str) -> str:
    if not os.environ.get("QUORUM_HARNESS_TESTS"):
        pytest.skip("set QUORUM_HARNESS_TESTS=1 to run tests that spend real harness tokens")
    found = shutil.which(binary)
    if not found:
        pytest.skip(f"{binary} not on PATH")
    return found


def adopt_and_queue_guidance(home: Path, repo: Path):
    r = runner.invoke(
        app, ["task", "adopt", "harness integration test", "--dir", str(repo), "--json"]
    )
    assert r.exit_code == 0, r.output
    task = TaskStore(home).get(json.loads(r.output.strip().splitlines()[-1])["id"])
    MessageBus(home).send(
        "manager", inbox_name(task.id), type="guidance",
        text="Acknowledge this guidance with the single word QUORUM-ACK.",
    )
    return task


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


@pytest.mark.codex_integration
def test_codex_hooks_adopt_report_and_deliver(home: Path, tmp_path: Path):
    codex = require("codex")
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.exists():
        pytest.skip("codex is not authenticated (~/.codex/auth.json missing)")
    repo = make_repo(tmp_path, "codexrepo")
    # An isolated CODEX_HOME with the shipped hooks.json: home-level is the
    # one hook scope every codex version discovers (project .codex/ hooks
    # need a newer codex than 0.149), and it shields the test from the
    # user's config.toml (which may pin a model this auth can't use).
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").symlink_to(auth)
    shutil.copy(INTEGRATIONS / "codex" / "hooks.json", codex_home / "hooks.json")
    task = adopt_and_queue_guidance(home, repo)

    r = subprocess.run(
        [
            codex, "exec",
            "--dangerously-bypass-hook-trust",
            "--sandbox", "read-only",
            "--cd", str(repo),
            "Reply with a one-word acknowledgement.",
        ],
        capture_output=True, text=True, timeout=600,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "QUORUM_HOME": str(home), "CODEX_HOME": str(codex_home)},
    )
    assert r.returncode == 0, f"codex exec failed:\n{r.stderr[-2000:]}"

    st = attached_state(home, task.id)
    assert st and st["event"] in LIVE_EVENTS, f"no hook fired; attached state: {st}"
    fresh = TaskStore(home).get(task.id)
    assert fresh.session, "SessionStart/Stop hook should have learned the session id"
    assert not MessageBus(home).pending(inbox_name(task.id)), "guidance was not delivered"
    assert fresh.attached


@pytest.mark.opencode_integration
def test_opencode_plugin_adopt_report_and_deliver(home: Path, tmp_path: Path):
    opencode = require("opencode")
    repo = make_repo(tmp_path, "openrepo")
    plugins = repo / ".opencode" / "plugins"
    plugins.mkdir(parents=True)
    shutil.copy(INTEGRATIONS / "opencode" / "plugin" / "quorum.js", plugins / "quorum.js")
    task = adopt_and_queue_guidance(home, repo)

    # a model must be pinned (a fresh project has none configured and
    # `opencode run` hangs on selection); the default is opencode's built-in
    # free-tier model, overridable when it inevitably rotates
    model = os.environ.get("QUORUM_OPENCODE_MODEL", "opencode/big-pickle")
    r = subprocess.run(
        [opencode, "run", "--model", model, "Reply with a one-word acknowledgement."],
        capture_output=True, text=True, timeout=600, cwd=repo,
        stdin=subprocess.DEVNULL,
        # PWD must agree with cwd: opencode resolves its project from $PWD
        env={**os.environ, "QUORUM_HOME": str(home), "PWD": str(repo)},
    )
    assert r.returncode == 0, f"opencode run failed:\n{r.stderr[-2000:]}"

    # the plugin's idle handler races `opencode run`'s exit; give it a moment
    bus = MessageBus(home)
    assert wait_for(lambda: not bus.pending(inbox_name(task.id))), "guidance was not delivered"
    st = attached_state(home, task.id)
    assert st and st["event"] in LIVE_EVENTS, f"attached state never advanced: {st}"
    fresh = TaskStore(home).get(task.id)
    assert fresh.session and fresh.session.startswith("ses"), (
        "idle hook should have learned the opencode session id"
    )
    assert fresh.attached
