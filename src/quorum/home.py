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
#run_stall_timeout_seconds = 1800   # stall watchdog (0 = off): end a run whose
                          # harness has printed nothing for this long. It counts
                          # silence, not progress — set it above your longest
                          # quiet step (a full test suite, a cold build).

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

# Reach a person: an argv template run once per new message on the listed
# board topics ({text}, {from}, {topic}, {type}, {id} substituted per element,
# no shell). Fails soft — a missing binary or a bad exit is one line in
# logs/supervisor.log. Try it with `quorum notify test "hello"`.
#[notify]
#command = ["terminal-notifier", "-title", "quorum", "-message", "{text}"]
#topics = ["attention"]   # the manager's ask-a-human channel
#timeout_seconds = 10
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


# The seed record: prompts/.seeded.json maps each packaged prompt filename to
# the sha256 of the text `quorum init` last wrote there (or last found
# identical to the packaged default). A home copy whose hash still equals
# its record is a pristine seed, so init may replace it with the current
# default; anything else is a user edit and is never touched. The record is
# local to the home, so changing a file in default_prompts/ needs no
# bookkeeping here. Dot-prefixed so `sorted_entries` skips it.
SEEDED_RECORD = ".seeded.json"


def seeded_record_path(home: Path) -> Path:
    return Path(home) / "prompts" / SEEDED_RECORD


def read_seeded_record(home: Path) -> dict[str, str]:
    """{filename: sha256} of what init last seeded. Fail-soft: a missing,
    unreadable or malformed record reads as empty, which classifies every
    differing copy as "edited" — the direction that never overwrites."""
    try:
        data = fsio.read_json(seeded_record_path(home))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def classify_prompt(existing: str | None, current: str, seeded: str | None) -> str:
    """One home prompt copy against the packaged default.

    "missing" (nothing seeded), "default" (identical to what ships now),
    "upgradable" (still byte-for-byte what init last seeded — `seeded` is
    that hash from the seed record — so `quorum init` may safely replace it)
    or "edited" (anything else: the user's own words, over a default that
    has since moved on; also any differing copy with no record, since a
    lost record must never turn into an overwrite).
    """
    if existing is None:
        return "missing"
    if existing == current:
        return "default"
    if seeded is not None and _sha256(existing) == seeded:
        return "upgradable"
    return "edited"


def classify_prompts(home: Path) -> dict[str, str]:
    """`classify_prompt` for every packaged default — the read-only view of
    prompt staleness `quorum doctor` reports and `_seed_prompts` acts on."""
    target = Path(home) / "prompts"
    record = read_seeded_record(home)
    states = {}
    for filename, current in packaged_prompts().items():
        dest = target / filename
        try:
            existing = dest.read_text(encoding="utf-8") if dest.is_file() else None
        except OSError:
            existing = None
        states[filename] = classify_prompt(existing, current, record.get(filename))
    return states


def _seed_prompts(home: Path) -> dict[str, str]:
    """Seed packaged prompt templates into prompts/ and upgrade stale seeds.

    A missing file is seeded. An existing file is replaced only when it is
    still the seed init wrote (`classify_prompts` → "upgradable", by the
    seed record). An edited file is never touched; when the packaged default
    has moved on it is reported as "edited" so the CLI can tell the user.
    Every file seeded, upgraded or found identical to the current default
    is (re)recorded in prompts/.seeded.json — so a home that predates the
    record picks one up as long as its copies are pristine. Returns
    {filename: "seeded" | "upgraded" | "edited"} covering only files that
    changed or need attention.
    """
    target = home / "prompts"
    outcomes: dict[str, str] = {}
    states = classify_prompts(home)
    record = read_seeded_record(home)
    recorded = dict(record)
    for filename, current in packaged_prompts().items():
        state = states.get(filename)
        dest = target / filename
        if state == "missing":
            dest.parent.mkdir(parents=True, exist_ok=True)
            fsio.atomic_write_text(dest, current)
            outcomes[filename] = "seeded"
        elif state == "upgradable":
            fsio.atomic_write_text(dest, current)
            outcomes[filename] = "upgraded"
        elif state == "edited":
            outcomes[filename] = "edited"
        if state != "edited":
            recorded[filename] = _sha256(current)
    if recorded != record:
        fsio.atomic_write_json(seeded_record_path(home), recorded)
    return outcomes
