"""The shipped harness adapters under integrations/ stay true.

Artifact tests parse the configs the READMEs tell users to copy and assert
they wire the harness's lifecycle events to the right quorum commands. The
opencode plugin — the one adapter with logic of its own — is additionally
driven for real under node with the PluginInput stubbed, against a real
quorum home (skipped when node is absent, like test_web.py without FastAPI).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quorum.messages import MessageBus
from quorum.tasks import TaskStore, attached_state, inbox_name
from test_tasks import make_repo

INTEGRATIONS = Path(__file__).parent.parent / "integrations"
DRIVER = Path(__file__).parent / "bin" / "opencode_plugin_driver.mjs"
PLUGIN = INTEGRATIONS / "opencode" / "plugin" / "quorum.js"


def hook_commands(hooks_json: Path) -> dict[str, list[str]]:
    doc = json.loads(hooks_json.read_text())
    return {
        event: [h["command"] for group in groups for h in group["hooks"] if h["type"] == "command"]
        for event, groups in doc["hooks"].items()
    }


def test_codex_hooks_wire_the_lifecycle_to_quorum():
    wired = hook_commands(INTEGRATIONS / "codex" / "hooks.json")
    assert wired == {
        "SessionStart": ["quorum task hook-session-start"],
        "Stop": ["quorum task hook-stop"],
        "SessionEnd": ["quorum task hook-session-end"],
    }


def test_claude_code_hooks_wire_the_lifecycle_to_quorum():
    wired = hook_commands(INTEGRATIONS / "claude-code" / "hooks" / "hooks.json")
    assert wired == {
        "Stop": ["quorum task hook-stop"],
        "SessionEnd": ["quorum task hook-session-end"],
    }


def test_codex_adopt_prompt_has_frontmatter_and_the_adopt_command():
    text = (INTEGRATIONS / "codex" / "prompts" / "quorum-adopt.md").read_text()
    assert text.startswith("---\n") and "description:" in text
    assert 'quorum task adopt "$ARGUMENTS" --json' in text


def test_opencode_command_matches_the_name_the_plugin_intercepts():
    # the plugin keys on command == "quorum-adopt", which opencode derives
    # from the command's filename
    assert (INTEGRATIONS / "opencode" / "commands" / "quorum-adopt.md").exists()
    assert 'input.command !== "quorum-adopt"' in PLUGIN.read_text()


# --- opencode plugin, driven for real under node ---------------------------


@pytest.fixture
def node() -> str:
    found = shutil.which("node")
    if not found:
        pytest.skip("node not installed")
    return found


@pytest.fixture
def quorum_bin(tmp_path: Path) -> Path:
    """A shim so the plugin's `quorum` shell-outs hit this checkout."""
    shim = tmp_path / "quorum-shim"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m quorum "$@"\n')
    shim.chmod(0o755)
    return shim


def drive(node: str, quorum_bin: Path, home: Path, mode: str, directory: Path,
          session: str, args: str = "") -> dict:
    r = subprocess.run(
        [node, str(DRIVER), str(PLUGIN), mode, str(directory), session, args],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "QUORUM_BIN": str(quorum_bin),
             "QUORUM_HOME": str(home)},
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_opencode_plugin_adopts_and_delivers_guidance(
    home: Path, tmp_path: Path, node: str, quorum_bin: Path
):
    repo = make_repo(tmp_path, "ocrepo")
    session = "ses_oc_driver_1"

    # /quorum-adopt: the plugin runs the adoption itself and rewrites the prompt
    out = drive(node, quorum_bin, home, "adopt", repo, session, "fix the flaky auth test")
    text = out["parts"][0]["text"]
    assert "succeeded" in text and "Report the outcome" in text
    tasks = [t for t in TaskStore(home).list() if t.attached]
    assert len(tasks) == 1
    task = tasks[0]
    assert task.session == session and task.workdir == str(repo)
    assert task.prompt == "fix the flaky auth test"

    # idle with guidance queued: hook-stop output injected as a user turn
    bus = MessageBus(home)
    bus.send("manager", inbox_name(task.id), type="guidance", text="run the tests before pushing")
    out = drive(node, quorum_bin, home, "idle", repo, session)
    assert len(out["injected"]) == 1
    call = out["injected"][0]
    assert call["path"]["id"] == session
    assert "run the tests before pushing" in call["body"]["parts"][0]["text"]
    assert not bus.pending(inbox_name(task.id))
    assert attached_state(home, task.id)["event"] == "stop"

    # idle with nothing queued: no injection (no delivery loop)
    out = drive(node, quorum_bin, home, "idle", repo, session)
    assert out["injected"] == []

    # dispose records session-end; the task stays attached
    drive(node, quorum_bin, home, "dispose", repo, session)
    assert attached_state(home, task.id)["event"] == "session-end"
    assert TaskStore(home).get(task.id).attached


def test_opencode_plugin_fails_soft_without_the_quorum_cli(
    home: Path, tmp_path: Path, node: str
):
    """A broken/missing quorum CLI must never break the session: the idle
    handler injects nothing, and adoption reports failure into the prompt."""
    missing = tmp_path / "no-such-quorum"
    repo = make_repo(tmp_path, "ocrepo2")
    out = drive(node, missing, home, "idle", repo, "ses_oc_driver_2")
    assert out["injected"] == []
    out = drive(node, missing, home, "adopt", repo, "ses_oc_driver_2")
    assert "failed" in out["parts"][0]["text"]
