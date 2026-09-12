#!/usr/bin/env python3
"""What a QUORUM_HOME shows was actually used (issue #128, round two of #102).

Run it against a home:

    uv run python scripts/evidence.py ~/.quorum

`scripts/surfaces.py` counts what quorum *exposes*. This counts what a home
records as *used*, so a verdict per surface has something under it. One table
per surface class — CLI commands, CLI options, config keys, TUI bindings,
prompt placeholders — each row carrying its observations split by actor and a
`verdict` column left blank for a person to fill in.

**Read only.** Nothing here writes, moves or deletes anything, in the home or
anywhere else, and it runs no quorum command against the home. Files are read
fail-soft: a torn line, a truncated gzip member or a file that disappeared
mid-scan is skipped, never raised — a home being written by a live supervisor
is the normal case.

## Where the evidence comes from

- `tasks/<id>/transcript.jsonl` — a task harness's tool calls, read through
  `quorum.transcript.tool_call` so every harness's spelling is handled in the
  one place that knows them. Actor: `task`.
- `state/manager/transcript.jsonl` — the same, for the manager, which is
  append-only across every tick. Actor: `manager`.
- `state/agents/<name>/transcript.jsonl` — the same, for a prompt agent.
  Actor: `agent`.
- `state/manager/journal.jsonl`, `state/agents/<name>/journal.jsonl` — what
  the CLI guard recorded, which is ground truth rather than a model's
  self-report. Entries with `actor: user` are a person's; the rest are a
  cross-check on the transcript scan, printed in its own column so the two
  records are never added together.
- `state/<agent>/runs/*.md` — the rendered prompt each tick reasoned over:
  what a placeholder actually expanded to. Never scanned for invocations —
  a digest quotes commands at the agent, and a quoted command is not use.
- `tasks/<id>/task.json` — a field only one option can produce is evidence
  that option was used, whoever typed it.
- `messages/` (board, inboxes, `archive/*.jsonl.gz`), `logs/actions.jsonl`
  and `logs/supervisor.log` — who posted what, and when the home was awake.
- `config.toml`, `agents/*.toml` — which config keys this home sets.
- `prompts/` — which templates and overlays the home has.

## Four limits this cannot see past, all material to a verdict

1. **A person's read-only command leaves no trace.** `quorum status`, `tui`,
   `task show` and the rest of `NO_TRACE` below change nothing, so a home
   cannot tell "run hourly by a person" from "never run". Those rows print
   `no-trace`, and for them absence of evidence is not evidence of absence.
2. **A TUI key press is indistinguishable from the CLI call it makes.** The
   write affordances are thin calls into the same code (`views.py`) by
   design, so the home records the write and not the key. Every binding row
   prints `no-channel`.
3. **An invocation that named another home is not use of this one.** A
   `--home <other>` or a `QUORUM_HOME=` prefix (including one exported by an
   earlier segment of the same command line) counts in the `scratch` column:
   that is quorum's own test suite and `doctor --smoke`, which run against a
   throwaway home on purpose.
4. **An invocation through the checkout is exercise, not use.** `uv run
   quorum …` runs the CLI under development; the report protocol and every
   documented workflow use the installed `quorum`. In a home whose tasks are
   work *on quorum*, that distinction is most of the traffic, so it gets the
   `checkout` column and is excluded from use too.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import re
import shlex
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "quorum"
sys.path.insert(0, str(SRC.parent))

from quorum import surfaces, transcript  # noqa: E402  (needs the src/ path above)

#: Actor classes, in the order their columns print.
ACTORS = ("person", "manager", "agent", "task")
#: Not actors but invocation classes, and never use. See the module docstring.
CHECKOUT = "checkout"
SCRATCH = "scratch"

#: Commands a person can run without the home recording anything. Removal by
#: default (issue #128) cannot apply to these on evidence alone, so they are
#: marked rather than counted. `tests/test_evidence.py` asserts every entry
#: names a real command, so a rename cannot leave a stale exemption here.
NO_TRACE = frozenset(
    {
        "agent list",
        "agent log",
        "board read",
        "doctor",
        "integration install",
        "integration list",
        "manager journal",
        "manager notes",
        "project list",
        "prompt diff",
        "prompt list",
        "status",
        "task export",
        "task history",
        "task list",
        "task log",
        "task show",
        "tui",
        "usage",
        "notify test",
    }
)

#: Verbs #102 removed. A home that still records someone reaching for one is
#: evidence that removal cost something; silence is evidence it did not.
REMOVED_IN_102 = frozenset({"web", "hold", "release", "set-priority", "tail"})

#: Tokens that may stand in front of the `quorum` word without changing which
#: program runs. The second set marks the invocation as the checkout's CLI.
PLAIN_WRAPPERS = frozenset({"env", "time", "sudo", "nohup", "exec", "command", "stdbuf"})
CHECKOUT_WRAPPERS = frozenset({"uv", "uvx", "run", "poetry", "pipx", "python", "python3"})
WRAPPERS = PLAIN_WRAPPERS | CHECKOUT_WRAPPERS

ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
#: A token made only of shell operators ends the segment: `;`, `&&`, `|`,
#: `>`, `2>&1` and friends.
OPERATOR = re.compile(r"^[;|&<>()]+$|^\d*[<>]&?\d*$")
#: The one option that takes a value *before* the subcommand, so its value is
#: never mistaken for a verb (`quorum --home X task list`).
ROOT_VALUE_OPTIONS = frozenset({"--home"})

#: A `task.json` field that only one `task add` option can produce.
STATE_IMPLIED_OPTIONS = {
    "issue_url": "--issue",
    "depends_on": "--after",
    "perpetual": "--perpetual",
    "herdr_pane": "--herdr-pane",
}

#: Journal action -> the command whose guard writes it. The journal is a
#: cross-check column, never added to the transcript counts.
JOURNAL_ACTIONS = {
    "task.run": "task run",
    "task.add": "task add",
    "task.adopt": "task adopt",
    "task.nudge": "task nudge",
    "task.cancel": "task cancel",
    "task.stop": "task stop",
    "task.prune": "task prune",
    "task.report": "task report",
    "note": "manager note",
    "remember": "manager remember",
    "forget": "manager forget",
    "board.post": "board post",
    "agent.reload": "agent reload",
}


@dataclass
class Invocation:
    """One `quorum …` command line found in some record."""

    path: str
    options: list[str]
    actor: str
    at: str
    source: str
    raw: str
    checkout: bool = False
    scratch: bool = False

    @property
    def column(self) -> str:
        """Which column this invocation counts in: use, or one of the two
        exclusions. Scratch wins, because a scratch home is the stronger
        statement about what the call was for."""
        if self.scratch:
            return SCRATCH
        if self.checkout:
            return CHECKOUT
        return self.actor


@dataclass
class Evidence:
    """Everything one home showed."""

    invocations: list[Invocation] = field(default_factory=list)
    journal: Counter = field(default_factory=Counter)  # (action, actor) -> n
    config_keys: dict[str, str] = field(default_factory=dict)
    prompt_files: dict[str, str] = field(default_factory=dict)
    tasks: list[dict] = field(default_factory=list)
    project_notes: int = 0
    snapshots: dict[str, list[str]] = field(default_factory=dict)  # agent -> texts
    templates: str = ""  # every prompt template, whitespace-collapsed
    agent_template: dict[str, str] = field(default_factory=dict)  # agent -> template
    timestamps: list[str] = field(default_factory=list)
    by_day: Counter = field(default_factory=Counter)  # (day, channel) -> n
    sources: Counter = field(default_factory=Counter)


# --------------------------------------------------------------------------
# fail-soft readers
# --------------------------------------------------------------------------


def read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, gzip.BadGzipFile, EOFError, UnicodeError):
        return []


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in read_lines(path):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# --------------------------------------------------------------------------
# parsing a shell command for `quorum …`
# --------------------------------------------------------------------------


def split_segments(command: str) -> list[list[str]]:
    """The command string as one token list per shell segment, or [] if it
    does not lex (an unbalanced quote inside a heredoc, most often)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if OPERATOR.match(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def quorum_head(tokens: list[str]) -> tuple[list[str], dict[str, str], bool] | None:
    """(tokens after the `quorum` word, env assignments, ran-from-checkout).

    Returns None when the segment does not run quorum — which is most of
    them: `grep -rn quorum src/` stops at `grep`.
    """
    env: dict[str, str] = {}
    checkout = False
    after_flag = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if Path(token).name == "quorum" and (i > 0 or token == "quorum"):
            return tokens[i + 1 :], env, checkout or "/" in token
        assign = ENV_ASSIGN.match(token)
        if assign and not after_flag:
            env[assign.group(1)] = assign.group(2)
            i += 1
            continue
        if token in WRAPPERS:
            checkout = checkout or token in CHECKOUT_WRAPPERS
            after_flag = False
            i += 1
            continue
        if i > 0 and token.startswith("-"):
            after_flag = "=" not in token
            i += 1
            continue
        if after_flag:  # the value of a wrapper's option (`uv run --project X`)
            after_flag = False
            i += 1
            continue
        return None
    return None


def parse_invocation(tokens: list[str], command_paths: set[str], groups: set[str]):
    """(command path, option spellings, named home) for one quorum call.

    The verb path is resolved against the real command tree rather than
    guessed, so an option value that happens to look like a verb cannot
    become one. Words that match no command are reported as `(unknown)` when
    the first of them is at least a real group or a verb #102 removed —
    which is how a home would show that a removed command is still wanted —
    and dropped otherwise, because `quorum has no forge write path` in a
    tool-call description is prose, not a command.
    """
    words: list[str] = []
    options: list[str] = []
    named_home: str | None = None
    want_home = False
    for token in tokens:
        if want_home:
            named_home = token
            want_home = False
            continue
        if token.startswith("-") and token != "-":
            decl, _, value = token.partition("=")
            options.append(decl)
            if decl in ROOT_VALUE_OPTIONS:
                if value:
                    named_home = value
                else:
                    want_home = True
            continue
        words.append(token)
    for depth in (2, 1):
        candidate = " ".join(words[:depth])
        if candidate in command_paths:
            return candidate, options, named_home
    if words and words[0] in groups:
        # `quorum task` or `quorum task --help`: the group's own help, not a
        # verb, and not evidence for any command under it.
        return words[0] + " (group only)", options, named_home
    if words and words[0] in REMOVED_IN_102:
        return " ".join(words[:2]).strip() + " (removed in #102)", options, named_home
    if not words and options:
        return "(root)", options, named_home
    return None


def invocations_in(
    command: str,
    actor: str,
    at: str,
    source: str,
    home: Path,
    command_paths: set[str],
    groups: set[str],
) -> list[Invocation]:
    """Every quorum call in one shell command string.

    Env assignments carry forward between segments: a test that does
    `export QUORUM_HOME=/tmp/x; quorum init` is one command line, and the
    second half runs against `/tmp/x`.
    """
    found: list[Invocation] = []
    if "quorum" not in command:
        return found
    carried: dict[str, str] = {}
    for tokens in split_segments(command):
        head = quorum_head(tokens)
        if head is None:
            # `export QUORUM_HOME=…`, or a bare assignment segment
            for token in tokens:
                assign = ENV_ASSIGN.match(token)
                if assign:
                    carried[assign.group(1)] = assign.group(2)
            continue
        rest, env, checkout = head
        parsed = parse_invocation(rest, command_paths, groups)
        if parsed is None:
            continue
        path, options, named_home = parsed
        target = named_home or env.get("QUORUM_HOME") or carried.get("QUORUM_HOME")
        found.append(
            Invocation(
                path=path,
                options=options,
                actor=actor,
                at=at,
                source=source,
                raw=" ".join(tokens)[:160],
                checkout=checkout,
                scratch=bool(target) and not same_home(target, home),
            )
        )
    return found


def same_home(named: str, home: Path) -> bool:
    if "$" in named or "`" in named:
        return False  # `$(mktemp -d)` and friends are never this home
    try:
        return Path(named).expanduser().resolve() == home.resolve()
    except OSError:
        return False


# --------------------------------------------------------------------------
# walking a transcript
# --------------------------------------------------------------------------


def tool_calls(node: object):
    """Every tool call anywhere in one transcript event.

    `transcript.tool_call` owns the harness vocabulary; this only finds the
    dicts to hand it, wherever a harness nested them.
    """
    if isinstance(node, dict):
        call = transcript.tool_call(node)
        if call is not None:
            yield call
        for value in node.values():
            yield from tool_calls(value)
    elif isinstance(node, list):
        for value in node:
            yield from tool_calls(value)


#: An unquoted `<slot>` in a command line: the shape a *documented* command
#: has, never one a shell actually ran.
PLACEHOLDER_SLOT = re.compile(r"<[a-z][a-z0-9 _|-]*>")


def is_quoted_example(line: str, templates: str, task_id: str) -> bool:
    """Whether a printed line is a command being *shown*, not run.

    A harness that echoes its own prompt puts every command the task preamble
    teaches into the transcript, and the preamble teaches most of the CLI. Two
    marks separate those from a run: the line still carries a `<slot>`, or the
    command it quotes is a line of a prompt template with the task id filled
    in. Applied to printed lines only — a tool call is a command that ran.
    """
    start = line.find("quorum")
    if start < 0:
        return False
    tail = " ".join(line[start:].split())
    if PLACEHOLDER_SLOT.search(tail):
        return True
    if task_id:
        tail = tail.replace(task_id, "{task_id}")
    return tail in templates


def command_of(args: object) -> str:
    """The shell command a tool call carries, however the harness spelled it."""
    if isinstance(args, str):
        return args
    if isinstance(args, list):
        return " ".join(str(a) for a in args)
    if isinstance(args, dict):
        for key in ("command", "cmd", "argv", "script"):
            if key in args:
                return command_of(args[key])
    return ""


def scan_transcript(
    path: Path,
    actor: str,
    source: str,
    channel: str,
    home: Path,
    command_paths: set[str],
    groups: set[str],
    ev: Evidence,
    task_id: str = "",
) -> None:
    if not path.exists():
        return
    seen: set[str] = set()
    for entry in read_jsonl(path):
        at = str(entry.get("at") or "")
        if at:
            ev.timestamps.append(at)
            ev.by_day[(at[:10], channel)] += 1
        commands: list[str] = []
        raw_line = entry.get("line")
        if isinstance(raw_line, str) and not is_quoted_example(raw_line, ev.templates, task_id):
            commands.append(raw_line)
        event = entry.get("event")
        if isinstance(event, dict):
            for call in tool_calls(event):
                # one call, however many events a harness used to announce it
                key = call.id or f"{at}:{call.name}:{command_of(call.args)[:80]}"
                if key in seen:
                    continue
                seen.add(key)
                commands.append(command_of(call.args))
        for command in commands:
            ev.invocations.extend(
                invocations_in(command, actor, at, source, home, command_paths, groups)
            )
    ev.sources[source] += 1


# --------------------------------------------------------------------------
# the whole home
# --------------------------------------------------------------------------


def collect(home: Path, command_paths: set[str], groups: set[str]) -> Evidence:
    ev = Evidence()
    for prompt in sorted((home / "prompts").glob("*.md")):
        ev.prompt_files[prompt.name] = "\n".join(read_lines(prompt))
    ev.sources["prompts/"] += len(ev.prompt_files)
    packaged = [p.read_text(encoding="utf-8", errors="replace")
                for p in sorted((SRC / "default_prompts").glob("*.md"))]
    ev.templates = " ".join(" ".join(t.split()) for t in [*ev.prompt_files.values(), *packaged])

    live = sorted(p for p in (home / "tasks").glob("*") if p.is_dir() and p.name[0] != ".")
    archived = sorted(p for p in (home / "tasks" / ".archive").glob("*") if p.is_dir())
    if archived:
        # `prune` moves rather than deletes, so the archive both holds more
        # evidence and *is* the evidence that someone pruned.
        ev.invocations.append(
            Invocation("task prune", [], "person", "", "tasks/.archive",
                       f"{len(archived)} archived task(s)")
        )
    for task_dir in live + archived:
        record = read_json(task_dir / "task.json")
        scan_transcript(
            task_dir / "transcript.jsonl", "task",
            "archived task transcript" if task_dir.parent.name == ".archive"
            else "task transcript", "task",
            home, command_paths, groups, ev, task_id=short_id(record),
        )
        if not record:
            continue
        ev.tasks.append(record)
        for key in ("created_at", "updated_at"):
            if record.get(key):
                ev.timestamps.append(str(record[key]))
        created = str(record.get("created_at") or "")
        for field_name, option in STATE_IMPLIED_OPTIONS.items():
            if record.get(field_name):
                ev.invocations.append(
                    Invocation("task add", [option], "person", created, "task.json field",
                               f"{task_dir.name}: {field_name} is set")
                )
        if record.get("use_worktree") is False:
            ev.invocations.append(
                Invocation("task add", ["--no-worktree"], "person", created,
                           "task.json field", f"{task_dir.name}: use_worktree=false")
            )
        if (task_dir / "handoff.md").exists():
            ev.invocations.append(
                Invocation("task report", ["--handoff"], "task", created,
                           "task.json field", f"{task_dir.name}: handoff.md exists")
            )
        if record.get("attached"):
            ev.invocations.append(
                Invocation("task adopt", [], "person", created, "task.json field",
                           f"{task_dir.name}: attached=true")
            )

    agents = [("manager", home / "state" / "manager")]
    for agent_dir in sorted(p for p in (home / "state" / "agents").glob("*") if p.is_dir()):
        if agent_dir.name != "manager":  # the manager keeps its spot at state/manager
            agents.append((agent_dir.name, agent_dir))

    config = read_toml(home / "config.toml")
    for name, base in agents:
        actor = "manager" if name == "manager" else "agent"
        scan_transcript(
            base / "transcript.jsonl", actor, f"{name} transcript", actor,
            home, command_paths, groups, ev,
        )
        for entry in read_jsonl(base / "journal.jsonl"):
            action = str(entry.get("action") or "")
            who = str(entry.get("actor") or name)
            ev.journal[(action, "person" if who == "user" else actor)] += 1
            at = str(entry.get("at") or "")
            if at:
                ev.timestamps.append(at)
                ev.by_day[(at[:10], "journal")] += 1
        ev.sources[f"{name} journal"] += 1
        texts = [t for t in ("\n".join(read_lines(p)) for p in sorted((base / "runs").glob("*.md"))) if t]
        ev.snapshots[name] = texts
        ev.sources[f"{name} run snapshots"] += len(texts)
        declared = (config.get("agents", {}) or {}).get(name, {})
        agent_type = str(declared.get("type") or read_toml(home / "agents" / f"{name}.toml").get("type") or "")
        ev.agent_template[name] = "manager.md" if agent_type == "manager" else f"{name}.md"

    for entry in read_jsonl(home / "logs" / "actions.jsonl"):
        at = str(entry.get("at") or "")
        if at:
            ev.timestamps.append(at)
            ev.by_day[(at[:10], "supervisor")] += 1
    ev.sources["logs/actions.jsonl"] += 1

    # `up` and `down` leave no journal entry, but the supervisor's own log is
    # their record: the banner `up` prints, and the SIGTERM `down` sends
    # (Ctrl-C is signal 2 and is nobody's command).
    for line in read_lines(home / "logs" / "supervisor.log"):
        stamp = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", line)
        if stamp:
            ev.timestamps.append(stamp.group(1))
        for marker, path in (("quorum supervisor starting", "up"),
                             ("received signal 15", "down")):
            if marker in line:
                ev.invocations.append(
                    Invocation(path, [], "person", stamp.group(1) if stamp else "",
                               "logs/supervisor.log", line[:120])
                )
    ev.sources["logs/supervisor.log"] += 1

    messages = list((home / "messages").rglob("*.json"))
    for message_path in sorted(messages):
        message = read_json(message_path)
        if message.get("created_at"):
            ev.timestamps.append(str(message["created_at"]))
    for archive in sorted((home / "messages" / "archive").glob("*.jsonl.gz")):
        for message in read_jsonl(archive):
            if message.get("created_at"):
                ev.timestamps.append(str(message["created_at"]))
    ev.sources["messages/"] += len(messages)

    ev.config_keys = config_keys_set(home, config)
    ev.sources["config.toml + agents/*.toml"] += 1
    for project in sorted((home / "projects").glob("*.json")):
        if (read_json(project).get("notes") or "").strip():
            ev.project_notes += 1
    return ev


def short_id(record: dict) -> str:
    """The handle a prompt calls a task by: the last six of its id."""
    return str(record.get("id") or "")[-6:].lower()


def config_keys_set(home: Path, config: dict) -> dict[str, str]:
    """Dotted key -> value, for every leaf this home actually sets."""
    found: dict[str, str] = {}

    def walk(table: dict, prefix: str) -> None:
        for key, value in table.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, dotted + ".")
            else:
                found[dotted] = repr(value)

    walk(config, "")
    for agent_file in sorted((home / "agents").glob("*.toml")):
        walk(read_toml(agent_file), f"agents.{agent_file.stem}.")
    return found


# --------------------------------------------------------------------------
# the tables
# --------------------------------------------------------------------------


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "|".join([" --- "] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(out)


def cell(counts: Counter, key: str) -> str:
    return str(counts[key]) if counts[key] else "·"


def trim(value: str, limit: int = 70) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(before: str, after: str) -> int:
    a, b = parse_time(before), parse_time(after)
    return 0 if a is None or b is None else (b - a).days


def window(ev: Evidence) -> tuple[str, str]:
    stamps = sorted({s for s in ev.timestamps if re.match(r"^\d{4}-\d\d-\d\d", s)})
    return (stamps[0], stamps[-1]) if stamps else ("—", "—")


def command_rows(ev: Evidence, commands: list[dict]) -> tuple[list[list[str]], Counter]:
    by_command: dict[str, Counter] = {}
    stamps: dict[str, list[str]] = {}
    for inv in ev.invocations:
        by_command.setdefault(inv.path, Counter())[inv.column] += 1
        if inv.at and inv.column in ACTORS:
            stamps.setdefault(inv.path, []).append(inv.at)
    journalled: Counter = Counter()
    for (action, _actor), n in ev.journal.items():
        path = JOURNAL_ACTIONS.get(action)
        if path:
            journalled[path] += n

    rows: list[list[str]] = []
    totals: Counter = Counter()
    for command in commands:
        if command["kind"] != "command":
            continue
        path = command["path"]
        counts = by_command.pop(path, Counter())
        used = sum(counts[a] for a in ACTORS) + journalled[path]
        seen = sorted(stamps.get(path, []))
        totals["used" if used else ("no-trace" if path in NO_TRACE else "unused")] += 1
        rows.append(
            [
                path,
                *[cell(counts, a) for a in ACTORS],
                cell(counts, CHECKOUT),
                cell(counts, SCRATCH),
                str(journalled[path]) if journalled[path] else "·",
                "no-trace" if path in NO_TRACE else "",
                f"{seen[0][:10]} … {seen[-1][:10]}" if seen else "—",
                "",
            ]
        )
    for path, counts in sorted(by_command.items()):
        rows.append(
            [
                path + " ¹",
                *[cell(counts, a) for a in ACTORS],
                cell(counts, CHECKOUT),
                cell(counts, SCRATCH),
                "·",
                "",
                "—",
                "",
            ]
        )
    return rows, totals


def option_rows(ev: Evidence, params: list[dict]) -> tuple[list[list[str]], Counter]:
    observed: dict[tuple[str, str], Counter] = {}
    for inv in ev.invocations:
        for option in inv.options:
            observed.setdefault((inv.path, option), Counter())[inv.column] += 1

    rows: list[list[str]] = []
    totals: Counter = Counter()
    for param in params:
        if param["kind"] != "option":
            continue
        command, decl = param["command"], param["decl"]
        spellings = decl.split("/")
        counts: Counter = Counter()
        for (path, spelling), bucket in observed.items():
            if spelling not in spellings:
                continue
            if command == "(root)" or path == command:
                counts.update(bucket)
        used = sum(counts[a] for a in ACTORS)
        totals["used" if used else ("no-trace" if command in NO_TRACE else "unused")] += 1
        rows.append(
            [
                command,
                decl,
                *[cell(counts, a) for a in ACTORS],
                cell(counts, CHECKOUT),
                cell(counts, SCRATCH),
                "no-trace" if command in NO_TRACE else "",
                "",
            ]
        )
    return rows, totals


def matches(actual: str, pattern: str) -> bool:
    """`agents.manager.type` matches the model's `agents.<name>.type`."""
    a, p = actual.split("."), pattern.split(".")
    return len(a) == len(p) and all(
        x == y or y.startswith("<") for x, y in zip(a, p, strict=True)
    )


def config_rows(ev: Evidence, keys: list[dict]) -> tuple[list[list[str]], Counter]:
    rows: list[list[str]] = []
    totals: Counter = Counter()
    claimed: set[str] = set()
    for key in keys:
        name = key["key"]
        if "." not in name:
            continue  # a table name, not a leaf a user writes
        hits = {k: v for k, v in ev.config_keys.items() if matches(k, name)}
        claimed |= set(hits)
        totals["used" if hits else "unused"] += 1
        rows.append(
            [
                name,
                trim(key["default"], 28),
                "yes" if hits else "·",
                trim("; ".join(f"{k}={v}" for k, v in sorted(hits.items())), 80),
                "",
            ]
        )
    for key in sorted(set(ev.config_keys) - claimed):
        rows.append([key + " ²", "—", "yes", trim(ev.config_keys[key], 80), ""])
    return rows, totals


def tui_rows() -> list[list[str]]:
    tree = ast.parse((SRC / "tui" / "app.py").read_text(encoding="utf-8"))
    rows: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if "BINDINGS" not in [t.id for t in stmt.targets if isinstance(t, ast.Name)]:
                continue
            for elt in getattr(stmt.value, "elts", []):
                try:
                    key, action, desc = ast.literal_eval(elt)
                except (ValueError, TypeError):
                    continue
                rows.append([node.name, key, action, desc, "no-channel", ""])
    return rows


def placeholder_evidence(ev: Evidence, name: str, templates: list[str]) -> str:
    """What this home shows about one prompt slot expanding to something.

    Each slot has its own observable: a rendered snapshot for the agent
    prompts, a task record field for the preamble's, an overlay file on disk
    for `{local}` and `{project}`.
    """
    run_templates = set(ev.agent_template.values())
    if name == "digest":
        texts = ev.snapshots.get("manager", [])
        hits = sum(1 for t in texts if t.startswith("# Situation digest"))
        return f"{hits}/{len(texts)} manager snapshots carry a digest"
    if name == "local":
        overlays = [t.replace(".md", ".local.md") for t in templates]
        present = [o for o in overlays if ev.prompt_files.get(o, "").strip()]
        return f"overlay present: {', '.join(present)}" if present else "no overlay file in prompts/"
    if name == "project":
        marker = "; ".join(
            filter(None, [f"{ev.project_notes} project(s) with notes" if ev.project_notes else ""])
        )
        return marker or "no registry notes; project overlay lives outside the home"
    if name == "issue":
        n = sum(1 for t in ev.tasks if t.get("issue_url"))
        return f"{n}/{len(ev.tasks)} tasks carry an issue"
    if name == "perpetual":
        n = sum(1 for t in ev.tasks if t.get("perpetual"))
        return f"{n}/{len(ev.tasks)} tasks are perpetual"
    if name in ("task_id", "project_path"):
        return f"substituted on every run ({len(ev.tasks)} tasks)"
    # a prompt-agent slot: only observable if an agent here runs that template
    live = [t for t in templates if t in run_templates]
    if not live:
        return "no agent in this home runs " + ", ".join(templates)
    return "runs: " + ", ".join(live)


def placeholder_rows(ev: Evidence) -> list[list[str]]:
    per_file: dict[str, set[str]] = {}
    for path in sorted((SRC / "default_prompts").glob("*.md")):
        stripped = re.sub(r"\{\{[^}]*\}\}", "", path.read_text(encoding="utf-8"))
        per_file[path.name] = set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", stripped))
    rows: list[list[str]] = []
    for name in sorted({n for s in per_file.values() for n in s}):
        templates = sorted(f for f, s in per_file.items() if name in s)
        in_home = sorted(f for f in templates if f in ev.prompt_files)
        rows.append(
            [
                name,
                ", ".join(templates),
                ", ".join(in_home) or "·",
                placeholder_evidence(ev, name, templates),
                "",
            ]
        )
    return rows


def day_rows(ev: Evidence) -> list[list[str]]:
    channels = ["task", "manager", "agent", "journal", "supervisor"]
    days = sorted({day for day, _ in ev.by_day})
    return [
        [day, *[str(ev.by_day[(day, c)]) if ev.by_day[(day, c)] else "·" for c in channels]]
        for day in days
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("home", type=Path, help="the QUORUM_HOME to read (never written)")
    args = parser.parse_args()
    home = args.home.expanduser()
    if not (home / "config.toml").exists() and not (home / "tasks").exists():
        parser.error(f"{home} does not look like a quorum home")

    commands, params = surfaces.cli_tree()
    keys = surfaces.config_keys()
    counts = surfaces.counts()
    command_paths = {c["path"] for c in commands if c["kind"] == "command"}
    groups = {c["path"] for c in commands if c["kind"] == "group"}

    ev = collect(home, command_paths, groups)
    first, last = window(ev)

    print(f"# Evidence — {home}\n")
    print(
        "Generated by `scripts/evidence.py`, read-only. The counts are observations "
        "of *use*; the `verdict` column is blank on purpose (issue #128)."
    )
    print(f"\nWindow: **{first}** … **{last}** ({days_between(first, last)} days).")
    print(
        "\n`checkout` counts calls through `uv run quorum` (the CLI under "
        "development), `scratch` calls that named another home. Neither is use of "
        "this home. `no-trace` marks a command a person can run without the home "
        "recording anything, and `journal` is the CLI guard's own record, printed "
        "as a cross-check rather than added to the columns left of it."
    )

    print("\n## 0. When this home was awake\n")
    print(md_table(
        ["day", "task transcript", "manager", "prompt agent", "journal", "supervisor"],
        day_rows(ev),
    ))

    rows, totals = command_rows(ev, commands)
    print(f"\n## 1. CLI commands ({counts['commands']} exposed)\n")
    print(md_table(
        ["command", *ACTORS, CHECKOUT, SCRATCH, "journal", "note", "first … last", "verdict"],
        rows,
    ))
    print(
        f"\nused by someone: {totals['used']}; no trace possible: {totals['no-trace']}; "
        f"no evidence: {totals['unused']}"
    )
    print(
        "\n¹ not a command: `quorum <group>` with no verb, a bare `quorum --help`, "
        "or a verb #102 removed. None of these is evidence for a command."
    )

    rows, totals = option_rows(ev, params)
    print(f"\n## 2. CLI options ({counts['options']} declarations)\n")
    print(md_table(
        ["command", "option", *ACTORS, CHECKOUT, SCRATCH, "note", "verdict"], rows
    ))
    print(
        f"\nused: {totals['used']}; on a no-trace command: {totals['no-trace']}; "
        f"no evidence: {totals['unused']}"
    )

    rows, totals = config_rows(ev, keys)
    print(f"\n## 3. Config keys ({counts['config_keys']} leaf settings)\n")
    print(md_table(["key", "default", "set here", "value", "verdict"], rows))
    print(f"\nset in this home: {totals['used']}; left at the default: {totals['unused']}")
    print("\n² a key this home sets that the config model does not define.")

    rows = tui_rows()
    print(f"\n## 4. TUI key bindings ({len(rows)})\n")
    print(md_table(["screen", "key", "action", "description", "note", "verdict"], rows))
    print(
        "\nEvery row is `no-channel`: a TUI write is the same call the CLI makes "
        "(`views.py`), so the home records the write and not the key press."
    )

    rows = placeholder_rows(ev)
    print(f"\n## 5. Prompt placeholders ({len(rows)} distinct)\n")
    print(md_table(
        ["placeholder", "templates", "in this home", "evidence it expanded", "verdict"], rows
    ))

    print("\n## Sources read\n")
    print(md_table(
        ["source", "files"], [[name, str(n)] for name, n in sorted(ev.sources.items())]
    ))
    print(
        f"\ninvocations parsed: {len(ev.invocations)}; "
        f"journal actions: {sum(ev.journal.values())}"
    )
    print(f"\nsurfaces: {surfaces.summary_line()}")


if __name__ == "__main__":
    main()
