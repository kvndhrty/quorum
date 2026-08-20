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

[projects]
# Directories the scout agent scans for unregistered projects (git repos or
# dirs containing a .quorum.toml marker). Proposals appear on the board;
# confirm with `quorum project adopt <slug>`.
workspace_roots = []

# Uncomment to give agents an LLM. `executable` is any CLI that takes a prompt
# and prints a completion: `claude` with args=["-p"], `codex` with args=["exec"],
# `llm`, `ollama` with args=["run", "llama3.2"], ...
# All agents degrade gracefully when this section is absent.
#[llm]
#backend = "cli"
#executable = "claude"
#args = ["-p"]
#input = "stdin"           # "stdin" | "argv" (use "{prompt}" placeholder in args)
#timeout_seconds = 120
#max_prompt_chars = 24000

[sandbox]
use_nono = false          # true: run LLM subprocesses under nono-py sandboxed_exec
profile = ""              # optional nono profile name, used by docs/tooling

[agents.tracker]
type = "tracker"
schedule = "every 30m"

[agents.sentinel]
type = "sentinel"
schedule = "cron 0 8 * * *"
[agents.sentinel.settings]
tiers = [14, 7, 3, 1, 0]

[agents.steward]
type = "steward"
schedule = "every 1h"
[agents.steward.settings]
watch = []                # e.g. ["~/Downloads"]
apply = false             # false = propose moves on the board; true = perform them
rules = []                # e.g. [{ match = "*.pdf", dest = "~/papers/inbox" }]

[agents.scribe]
type = "scribe"
schedule = "cron 30 17 * * *"

[agents.scout]
type = "scout"
schedule = "cron 15 9 * * *"
"""

SUBDIRS = [
    "projects",
    "prompts",
    "messages/board",
    "messages/inbox",
    "messages/archive",
    "state/agents",
    "state/projects",
    "state/steward",
    "briefs",
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
