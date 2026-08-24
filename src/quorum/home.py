"""QUORUM_HOME: the single directory holding all quorum state.

Resolution order: explicit --home flag > $QUORUM_HOME > ./quorum-home (if it
exists) > ~/.quorum. Everything quorum reads or writes lives under this tree,
which is what keeps sandbox profiles small and the whole system portable —
copy the directory and the state moves with it.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_NAME = "config.toml"

DEFAULT_CONFIG = """\
# quorum configuration — human-edited; quorum never rewrites this file.

[quorum]
timezone = "local"        # display only; stored timestamps are always UTC
retention_days = 30       # board messages older than this are archived

[tasks]
worktree = true           # run each task in its own git worktree under QUORUM_HOME
stall_minutes = 15        # quiet for this long = the monitor pokes the task
max_resumes = 3           # resume attempts before a task is marked blocked
default_harness = ""      # e.g. "claude" — used when `quorum task add` has no --harness

# A harness is any coding-agent CLI that takes a prompt and works autonomously
# in the current directory. "{prompt}" and "{session}" are substituted; a
# template without "{prompt}" gets the prompt appended as the last argument.
# `resume` is optional — quorum captures a session_id from JSON output when
# the harness emits one.
#
# Runs are unattended, so the harness needs permission to act without asking —
# otherwise it stalls silently on its first denied tool call. Prefer a scoped
# allowlist covering the report protocol (Bash(quorum:*)) and the work itself
# over blanket permission bypasses. See docs/guide.md#harnesses.
#[harness.claude]
#start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose",
#          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)"]
#resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--verbose",
#          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)"]

#[harness.codex]
#start = ["codex", "exec", "{prompt}"]

#[harness.opencode]
#start = ["opencode", "run", "{prompt}"]

# Uncomment to give the monitor an LLM for smarter stall triage and nudges.
# `executable` is any CLI that takes a prompt and prints a completion.
# Everything degrades gracefully when this section is absent.
#[llm]
#backend = "cli"
#executable = "claude"
#args = ["-p"]
#input = "stdin"           # "stdin" | "argv" (use "{prompt}" placeholder in args)
#timeout_seconds = 120
#max_prompt_chars = 24000

[sandbox]
use_nono = false          # true: sandbox task runs + LLM subprocesses via nono-py
profile = ""              # optional nono profile name (used with `nono run`)
#profile_file = "~/.config/nono/profiles/quorum.json"  # your own nono-style
#                         # JSON profile (fs_read/fs_write/network), merged
#                         # into the grants quorum derives for modes 2 and 3
#task_read  = []          # extra read grants for sandboxed task runs
#task_write = ["~/.claude"]  # harness state dirs need write (claude: ~/.claude)

[agents.monitor]
type = "monitor"
schedule = "every 2m"

# Plugin agents: drop a .py file into QUORUM_HOME/plugins/ and point a stanza
# at it (see docs/guide.md; examples/steward.py in the quorum repo is a
# complete worked example):
#[agents.steward]
#type = "steward:Steward"
#schedule = "every 1h"
"""

SUBDIRS = [
    "projects",
    "prompts",
    "messages/board",
    "messages/inbox",
    "messages/archive",
    "state/agents",
    "tasks",
    "worktrees",
    "logs",
    "plugins",
]


def resolve_home(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("QUORUM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    local = Path.cwd() / "quorum-home"
    if local.is_dir():
        return local.resolve()
    return Path.home() / ".quorum"


def scaffold(home: Path) -> bool:
    """Create the QUORUM_HOME tree. Returns True if the config was newly written."""
    home.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    config = home / CONFIG_NAME
    fresh = not config.exists()
    if fresh:
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    _seed_prompts(home)
    return fresh


def _seed_prompts(home: Path) -> None:
    """Copy packaged default prompt templates into prompts/ (never overwrites)."""
    from importlib import resources

    target = home / "prompts"
    try:
        defaults = resources.files("quorum") / "default_prompts"
        for entry in defaults.iterdir():  # type: ignore[attr-defined]
            if entry.name.endswith(".md"):
                dest = target / entry.name
                if not dest.exists():
                    dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass
