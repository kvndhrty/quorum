"""QUORUM_HOME: the single directory holding all quorum state.

Resolution order: explicit --home flag > $QUORUM_HOME > ./quorum-home (if it
exists) > ~/.quorum. Everything quorum reads or writes lives under this tree,
which is what keeps sandbox profiles small and the whole system portable —
copy the directory and the state moves with it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import fsio

CONFIG_NAME = "config.toml"

DEFAULT_CONFIG = """\
# quorum config — yours to edit; quorum never writes this file.
# To start: uncomment ONE [harness.*] block below and set default_harness.
# Everything else can wait. All options: docs/guide.md

[tasks]
default_harness = ""      # e.g. "claude"
worktree = true           # each task runs in its own git worktree

# A harness is any coding-agent CLI; {prompt} and {session} are substituted.
# Runs are unattended — the flags below let the harness act without asking,
# otherwise it stalls silently at its first permission prompt.
#[harness.claude]
#start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
#          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)"]
#resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
#          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)"]
#inject = "stream-json"   # delivers the prompt + nudges over stdin (stream-json CLIs ignore an argv prompt)

#[harness.codex]
#start  = ["codex", "exec", "--json", "{prompt}"]
#resume = ["codex", "exec", "resume", "{session}", "--json", "{prompt}"]

#[harness.opencode]
#start = ["opencode", "run", "{prompt}"]

# Any agentic CLI works as a harness — point start at your own binary:
#[harness.custom]
#start = ["/path/to/your-agent", "--prompt", "{prompt}"]

# Optional small-completion LLM for plugin agents (ctx.llm.complete()).
# Any binary that takes a prompt and prints text; the manager does not
# use this — it runs a full harness (above).
#[llm]
#backend = "cli"
#executable = "claude"
#args = ["-p"]
#input = "stdin"          # "stdin" | "argv" ({prompt} placeholder in args)
#timeout_seconds = 120

# The manager checks on everything every 5 minutes, driven by the same
# harness. Its policy is a prompt you can edit: prompts/manager.md
[agents.manager]
type = "manager"
schedule = "every 5m"
auto_pause = false        # keep trying while the LLM service is down

[quorum]
retention_days = 30       # board messages older than this are archived

[sandbox]
use_nono = false          # sandbox task runs via nono-py (docs/guide.md#sandboxing)
"""

SUBDIRS = [
    "agents",
    "projects",
    "prompts",
    "messages/board",
    "messages/inbox",
    "messages/archive",
    "state/agents",
    "state/manager",
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


# sha256 of every *previously* shipped version of each default prompt. A
# prompts/ file whose content hashes into this set is a pristine seed from an
# older quorum, so `quorum init` upgrades it to the current default; any other
# content is a user edit and is never touched. When you change a file in
# default_prompts/, append the hash of the version you are replacing here
# (`shasum -a 256 src/quorum/default_prompts/<name>` before editing, or
# `git show HEAD:src/quorum/default_prompts/<name> | shasum -a 256` after).
SUPERSEDED_PROMPT_HASHES: dict[str, set[str]] = {
    "task-preamble.md": {
        "28f1079b09bfad2841dca8ebbeae8131969c81b97a0b6a7611deb4035f2048be",
    },
    "manager.md": {
        "ac136ce1d1da20740f949c88be16cef2e7fe83c5031b48c7434ebbe784227acb",
        "04ccc56d28eb382b859d7073616a71dd2af5a6156b5b281e36e8fe8521ea2a55",
    },
}


def scaffold(home: Path) -> tuple[bool, dict[str, str]]:
    """Create the QUORUM_HOME tree.

    Returns (config newly written, prompt seeding outcomes — see
    `_seed_prompts`). Safe to re-run: an existing config is never rewritten,
    and re-running is how an upgraded quorum refreshes unedited prompts.
    """
    home.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    config = home / CONFIG_NAME
    fresh = not config.exists()
    if fresh:
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return fresh, _seed_prompts(home)


def _seed_prompts(home: Path) -> dict[str, str]:
    """Seed packaged prompt templates into prompts/ and upgrade stale seeds.

    A missing file is seeded. An existing file is replaced only when its
    content matches a previously shipped default (`SUPERSEDED_PROMPT_HASHES`)
    — i.e. the user never edited it. An edited file is never touched; when
    the packaged default has moved on it is reported as "edited" so the CLI
    can tell the user. Returns {filename: "seeded" | "upgraded" | "edited"}
    covering only files that changed or need attention.
    """
    from importlib import resources

    target = home / "prompts"
    outcomes: dict[str, str] = {}
    try:
        defaults = resources.files("quorum") / "default_prompts"
        entries = [e for e in defaults.iterdir() if e.name.endswith(".md")]  # type: ignore[attr-defined]
    except (FileNotFoundError, ModuleNotFoundError):
        return outcomes
    for entry in entries:
        current = entry.read_text(encoding="utf-8")
        dest = target / entry.name
        if not dest.is_file():
            dest.write_text(current, encoding="utf-8")
            outcomes[entry.name] = "seeded"
            continue
        existing = dest.read_text(encoding="utf-8")
        if existing == current:
            continue
        digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
        if digest in SUPERSEDED_PROMPT_HASHES.get(entry.name, set()):
            fsio.atomic_write_text(dest, current)
            outcomes[entry.name] = "upgraded"
        else:
            outcomes[entry.name] = "edited"
    return outcomes
