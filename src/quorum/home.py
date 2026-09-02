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
#auto_commit = true       # safety net: commit whatever a run leaves uncommitted
                          # in its worktree, so a crashed harness loses nothing
                          # (skipped under [sandbox].use_nono — git is blocked there)
#max_cost_per_run = 5.0   # budget observation (0 = off): a run that reports
#max_tokens_per_run = 0   # more spend than this is flagged in the digest and
                          # in `quorum status`. Nothing is killed or refused
                          # for cost — the manager judges what to do.

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

[ci]
enabled = true            # let the manager digest observe PR/check state via `gh`
                          # (silently does nothing without gh; one network call
                          # per digested task per tick)
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
        # replaced by the {perpetual} placeholder revision (#12)
        "ac344c667083649b04166a4801c8646240a67f37a078b90c13db922005079a6d",
        # replaced by the {local} overlay-slot revision (#37)
        "f564262f9fa66c42301e211c64e77433e981003f0df138964aa78174898845f0",
        # the overlay-slot revision, replaced by the rebase-before-push
        # delivery step (#58)
        "fa56cbd13104899d52ba4169ed1829c4b4b29ebe2389d3f9e7f3ae390949f677",
    },
    "task-perpetual.md": {
        # the original (#12) block, replaced by the {local} overlay-slot
        # revision (#37)
        "5023261d8d55f456570cba3df7c1212de7f7fd4c7fedd11a8c86f77330743934",
    },
    "manager.md": {
        "ac136ce1d1da20740f949c88be16cef2e7fe83c5031b48c7434ebbe784227acb",
        "04ccc56d28eb382b859d7073616a71dd2af5a6156b5b281e36e8fe8521ea2a55",
        "5f2838b51db8ac07f830a668cae0d6c4d9afe181d7598d51a7dbd1afe5b89b15",
        "02e3715e80b0bdb167b1c35bc354ba756efc00774772e1f6e0e9193c93a974f2",
        # the #19 (usage/budget) revision, replaced by the merge with the
        # ci-probe revision
        "e9d9c79d5822a4f34b8237306ab87dc2c9dd4bcb6997f21dd3276e2e3665cca1",
        # the ci-probe revision, replaced by the perpetual-task + self-cost
        # revision (#12, #32)
        "454fb62b2718f5303a7bb6623ed959aa8289d82613b47c2c006125e84960e96e",
        # replaced by the {local} overlay-slot revision (#37)
        "8afe70f5f190c23c22d18c5d6b90b8db68da37436d8ed3efba5a948af994116f",
        # replaced by the notebook revision (#35): note vs remember vs forget
        "634f2d9516e58f47d560374b26fa14089910d48c3ab1bd073ae81e33b5a3e950",
        # the notebook revision, replaced by the #31 task-dependency rule
        "3ea524332199f2330240e647847e2dcf9165ce33985d6e61544bbc731e0455e9",
        # the task-dependency revision, replaced by the overlaps= rule (#58)
        "27622474013b1a239267e9e3deacfd488077e952c17e4b5c0b28e1133c743ed2",
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


def packaged_prompts() -> dict[str, str]:
    """The packaged default prompt templates, {filename: text}.

    Empty when the package's default_prompts/ cannot be read at all — every
    caller treats that as "nothing to say", never as "everything is stale".
    """
    from importlib import resources

    try:
        defaults = resources.files("quorum") / "default_prompts"
        entries = [e for e in defaults.iterdir() if e.name.endswith(".md")]  # type: ignore[attr-defined]
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    return {e.name: e.read_text(encoding="utf-8") for e in entries}


def classify_prompt(existing: str | None, current: str, filename: str) -> str:
    """One home prompt copy against the packaged default.

    "missing" (nothing seeded), "default" (identical to what ships now),
    "upgradable" (a pristine seed from an older quorum — recognized by hash,
    so `quorum init` may safely replace it) or "edited" (anything else: the
    user's own words, over a default that has since moved on).
    """
    if existing is None:
        return "missing"
    if existing == current:
        return "default"
    digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
    if digest in SUPERSEDED_PROMPT_HASHES.get(filename, set()):
        return "upgradable"
    return "edited"


def classify_prompts(home: Path) -> dict[str, str]:
    """`classify_prompt` for every packaged default — the read-only view of
    prompt staleness `quorum doctor` reports and `_seed_prompts` acts on."""
    target = Path(home) / "prompts"
    states = {}
    for filename, current in packaged_prompts().items():
        dest = target / filename
        try:
            existing = dest.read_text(encoding="utf-8") if dest.is_file() else None
        except OSError:
            existing = None
        states[filename] = classify_prompt(existing, current, filename)
    return states


def _seed_prompts(home: Path) -> dict[str, str]:
    """Seed packaged prompt templates into prompts/ and upgrade stale seeds.

    A missing file is seeded. An existing file is replaced only when it is a
    pristine seed from an older quorum (`classify_prompts` → "upgradable").
    An edited file is never touched; when the packaged default has moved on
    it is reported as "edited" so the CLI can tell the user. Returns
    {filename: "seeded" | "upgraded" | "edited"} covering only files that
    changed or need attention.
    """
    target = home / "prompts"
    outcomes: dict[str, str] = {}
    states = classify_prompts(home)
    for filename, current in packaged_prompts().items():
        state = states.get(filename)
        dest = target / filename
        if state == "missing":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(current, encoding="utf-8")
            outcomes[filename] = "seeded"
        elif state == "upgradable":
            fsio.atomic_write_text(dest, current)
            outcomes[filename] = "upgraded"
        elif state == "edited":
            outcomes[filename] = "edited"
    return outcomes
