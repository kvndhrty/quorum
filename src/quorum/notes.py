"""The notebook: what a future run needs to know, kept apart from everything else.

Every manager tick is a fresh harness run with no memory beyond what the
digest puts in front of it, and that memory used to be the action journal
tail — a bounded window shared with every auto-journaled `task run` and
`nudge`. A note written *for a future self* ("that PR is waiting on the
human; don't relaunch it") was pushed out of the window by the next busy
tick. The journal is a diary of one run; this is the notebook.

Two properties define it, and both are structural rather than polite:

- **Its own file.** `state/manager/notes.jsonl` (per-agent equivalent
  `state/agents/<name>/notes.jsonl`, via `actor.notes_path`) — not a board
  topic, so no task report, prompt agent or chatty babysitter can post into
  it and crowd the manager's own notes out; and not the journal, so an
  action-heavy run cannot age a standing fact out of the window.
- **Its own digest budget.** `Notebook.render_notes` bounds the notebook with
  `NOTES_MAX_ENTRIES` / `NOTES_MAX_BYTES`, which nothing else in the digest
  spends. Ten noisy tasks with long report tails cannot shrink it.

Writes are append-only: a note is one line, and `forget` appends a
*tombstone* rather than rewriting history, so a reader only ever appends or
reads. Nothing is compacted or summarized in Python — when the notebook
outgrows its budget the digest says how many older notes it dropped, and the
manager's prompt tells it to consolidate: one superseding note, then forget
the rest. That is policy, and policy lives in prompts.

A **task** has the same notebook (`tasks/<id>/notes.jsonl`, via
`task_notebook`): the same line schema, tombstones, TTL and skipped
malformed lines, rendered by the runner into every composed prompt — resume
and fresh session alike — under its own budget, and deliberately *not* into
the digest (the manager reads reports; the notebook is the task's own). The
two differ only in what `Notebook` carries: where the file is, who besides
the owner may write (the manager may write a task's), and the command names
the rendering teaches. `agent_notebook` and `task_notebook` are the only
two entry points: a caller builds the notebook it means and calls its
methods.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import fsio
from .actor import notes_path, task_actor

# The notebook's slot in the digest. These are *independent* of the task
# section's budget by design (that is the whole point of the reserved slot),
# and they are plain constants rather than config for the same reason the
# loop thresholds are: getting one wrong costs a chattier digest, never a
# lost note — the file keeps everything, only the rendering is bounded.
NOTES_MAX_ENTRIES = 20
NOTES_MAX_BYTES = 4000
# A task's notebook is rendered into its own prompt, which nothing else
# shares, so it can afford more than the manager's digest slot — but it is
# still bounded: a task that appends instead of rewriting must be told so,
# not handed an ever-longer prompt.
TASK_NOTES_MAX_ENTRIES = 30
TASK_NOTES_MAX_BYTES = 6000
# One note that is somehow book-length must not eat the whole slot, and must
# not push the newest note out either: lines are truncated, entries dropped.
NOTE_MAX_CHARS = 600
# The file is append-only and unbounded in principle, but a consolidated
# notebook is small; readers take a bounded tail like every other jsonl log
# here. A note older than this window is invisible (and so is its tombstone,
# which is why the pair stays consistent) — including a permanent one, so the
# digest says how many bytes it did not scan rather than quietly forgetting.
NOTES_SCAN_BYTES = 256 * 1024

SECTION_HEADER = "## Your notebook (standing notes to yourself)"
EMPTY_LINE = (
    '(empty — `quorum manager remember "<fact>"` writes a standing note that '
    "every future run of yours will read)"
)
TASK_SECTION_HEADER = "# Your notebook (what you kept for yourself between runs)"


class NotebookError(Exception):
    """A refused or unresolvable notebook operation; the CLI renders it."""


def _nbytes(line: str) -> int:
    """The line's cost in the rendering, in UTF-8 bytes plus its newline."""
    return len(line.encode("utf-8")) + 1


def short_id(note_id: str) -> str:
    """The handle `remember` prints and `forget` accepts: the ULID's random
    tail, for the same reason `Task.short_id` is (the head is a shared
    timestamp)."""
    return note_id[-6:].lower()


def check_owner(owner: str) -> None:
    """A notebook lives under `state/agents/<owner>/`, so an owner name is a
    path component: hold it to the same rules as any agent name (the
    manager's historical spot is the one exemption). Every entry point that
    takes an owner from the outside — reads included — goes through here."""
    if owner == "manager":
        return
    from .config import ConfigError, validate_agent_name

    try:
        validate_agent_name(owner)
    except ConfigError as e:
        raise NotebookError(str(e)) from None


@dataclass(frozen=True)
class Notebook:
    """One notebook: where it lives, whose it is, and how it is rendered.

    `owner` is the actor name that may write it (an agent name, or a task's
    `task-<id>`); `owner_label` is how that owner is named in an error, which
    is not the same string for a task (`task-<full ulid>` is not what a
    person typed); `writers` are the others allowed besides the owner and an
    untagged human — empty for an agent, the manager for a task. The rest
    is rendering: the section header, the budget the rendering is held to,
    the line an empty notebook shows (None: nothing at all — a task's prompt
    does not carry an "(empty)" section, its preamble teaches the command),
    and the command names the drop line and the errors teach, which differ
    between `quorum manager remember` and `quorum task remember <id>`.
    """

    path: Path
    owner: str
    owner_label: str
    writers: frozenset[str]
    header: str
    max_entries: int
    max_bytes: int
    empty_line: str | None
    remember_cmd: str
    forget_cmd: str
    read_cmd: str
    budget_name: str
    refusal_hint: str

    # -- who -----------------------------------------------------------------

    def may_write(self, sender: str) -> bool:
        """The owner, an extra writer, or a human. A **convention, not a
        security boundary**: `sender` is read off `QUORUM_ACTOR`, which any
        process that can run the CLI can set. It stops honest callers from
        crowding each other's memory; the sandbox is the real fence."""
        return sender == "user" or sender == self.owner or sender in self.writers

    def refusal(self, sender: str) -> str:
        """Why `sender` was refused and what to do instead, in one place: the
        methods below raise it and the CLI prints it, so the two cannot
        drift apart."""
        return f"{sender} may not write to {self.owner_label}'s notebook — {self.refusal_hint}"

    # -- reads ---------------------------------------------------------------

    def entries(self) -> list[dict]:
        """Well-formed lines only. Anything hand-edited, half-written or
        written by a future version — a missing id, an id that is not a
        string — is skipped rather than allowed to raise out of a digest
        build or a prompt composition, because a single bad line would
        otherwise fail every run forever.

        The file itself is read the same way: a notes.jsonl that is a
        directory, or unreadable, reads as an empty notebook. This is on the
        path of every task run (`runner.compose_prompt`), so raising here
        would fail the run before the harness starts — the same reason
        `tasks.read_handoff` swallows OSError.
        """
        try:
            raw = fsio.read_jsonl_tail(self.path, max_bytes=NOTES_SCAN_BYTES)
        except OSError:
            return []
        return [
            e
            for e in raw
            if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]
        ]

    def unscanned_bytes(self) -> int:
        """How much of the notebook fell outside the `NOTES_SCAN_BYTES` tail.

        Readers take a bounded tail, so a big enough file silently hides its
        oldest notes — including permanent ones. The count is surfaced in
        the rendering (an observation, like `possible-loop`), never acted on
        here.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        return max(0, size - NOTES_SCAN_BYTES)

    def active(self, now: datetime | None = None) -> list[dict]:
        """Unretired, unexpired notes, oldest first — the notebook as it reads."""
        now = now or fsio.utc_now()
        entries = self.entries()
        retired = {e["id"] for e in entries if e.get("retired")}
        out = []
        for e in entries:
            if e.get("retired") or e["id"] in retired or not e.get("text"):
                continue
            end = expires_at(e)
            if end is not None and now >= end:
                continue
            out.append(e)
        return out

    def resolve(self, handle: str, now: datetime | None = None) -> dict:
        """Find one note by full id, unique prefix, or unique suffix (what
        `short_id` hands out) — `fsio.resolve_handle`'s grammar, the one
        tasks and board messages use, wearing the notebook's own errors.

        An empty handle is refused with its own message: `resolve_handle`
        reads it as "nothing matches", but a reader who typed nothing is
        better told what to type than told there is no such note.
        """
        if not handle.strip():
            raise NotebookError(f"a note handle is required — `{self.read_cmd}`")
        by_id = {e["id"]: e for e in self.active(now=now)}
        try:
            return by_id[fsio.resolve_handle(handle, by_id, what="note handle")]
        except KeyError:
            raise NotebookError(f"no note matching {handle!r} — `{self.read_cmd}`") from None
        except ValueError as e:
            raise NotebookError(str(e)) from None

    # -- writes --------------------------------------------------------------

    def remember(
        self,
        text: str,
        sender: str = "user",
        run_id: str = "",
        ttl_days: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Append a standing note. Raises on a refused sender."""
        if not self.may_write(sender):
            raise NotebookError(self.refusal(sender))
        text = text.strip()
        if not text:
            raise NotebookError("a note needs some text")
        if ttl_days is not None and ttl_days < 0:
            raise NotebookError("--ttl must be a number of days (0 or omitted: never expires)")
        entry: dict[str, Any] = {
            "id": fsio.ulid(now),
            "ts": fsio.iso(now or fsio.utc_now()),
            "run_id": run_id,
            "sender": sender,
            "text": text,
        }
        if ttl_days:
            entry["ttl_days"] = ttl_days
        fsio.append_jsonl(self.path, entry)
        return entry

    def forget(
        self,
        handle: str,
        sender: str = "user",
        run_id: str = "",
        now: datetime | None = None,
    ) -> dict:
        """Retire a note by appending a tombstone; readers hide both lines."""
        if not self.may_write(sender):
            raise NotebookError(self.refusal(sender))
        note = self.resolve(handle, now=now)
        fsio.append_jsonl(
            self.path,
            {
                "id": note["id"],
                "ts": fsio.iso(now or fsio.utc_now()),
                "run_id": run_id,
                "sender": sender,
                "retired": True,
            },
        )
        return note

    # -- rendering -----------------------------------------------------------

    def render_notes(
        self, notes: list[dict], now: datetime | None = None, unscanned: int = 0
    ) -> list[str]:
        """The notebook as prompt lines, under its own caps.

        Over the cap the *newest* notes are kept — a standing fact written
        today outranks one from a month ago — and the count of dropped ones
        is stated where they would have been, so the reader can see it has
        consolidating to do. Summarizing them is deliberately not attempted
        here: the prompt tells the agent to write one superseding note and
        forget the rest.

        `unscanned` is the second, quieter way notes go missing: the file
        grew past `NOTES_SCAN_BYTES` and its oldest lines — permanent notes
        included — were never read. The rendering states the count and
        nothing here acts on it.

        An empty notebook renders `empty_line` under the header, or nothing
        at all when the notebook has none (and nothing went unscanned).
        """
        now = now or fsio.utc_now()
        lines = [self.header]
        if unscanned:
            lines.append(
                f"({unscanned} bytes of older notes were not scanned — the notebook file "
                "is past its read window, so notes older than the ones below are "
                "invisible, permanent ones included; consolidate what you still need)"
            )
        if not notes:
            if self.empty_line is None:
                return lines if unscanned else []
            return lines + [self.empty_line]
        rendered = self._fit(notes, now)
        dropped = len(notes) - len(rendered)
        if dropped:
            lines.append(
                f"({dropped} older note(s) dropped — the notebook is over its "
                f"{self.budget_name} budget: consolidate with one superseding "
                f'`{self.remember_cmd} "…"`, then `{self.forget_cmd} <id>` the rest)'
            )
        return lines + rendered

    def _fit(self, notes: list[dict], now: datetime) -> list[str]:
        """The notes that survive both caps, as their rendered lines.

        Entry cap first, byte cap second: entries are dropped oldest-first,
        but the newest note always survives (truncated by `describe` if it
        has to be). Measured in UTF-8 bytes like `runner.clip_handoff`, so a
        notebook written in a non-Latin script gets the budget the name
        promises. `render_notes` prints these and `size` measures them, so
        what a reader is told it is using is what the prompt will carry.
        """
        rendered = [describe(e, now) for e in notes[-self.max_entries :]]
        while len(rendered) > 1 and sum(_nbytes(line) for line in rendered) > self.max_bytes:
            rendered.pop(0)
        return rendered

    def size(self, now: datetime | None = None) -> dict[str, int]:
        """How full the notebook is, as numbers rather than as a rendering.

        `{"notes", "shown", "dropped", "bytes", "max_bytes", "max_entries",
        "unscanned"}` — what `quorum task show self` reports so a run can
        consolidate *before* the budget starts dropping its oldest notes,
        which the rendering only says once it already has.
        """
        now = now or fsio.utc_now()
        notes = self.active(now=now)
        rendered = self._fit(notes, now)
        return {
            "notes": len(notes),
            "shown": len(rendered),
            "dropped": len(notes) - len(rendered),
            "bytes": sum(_nbytes(line) for line in rendered),
            "max_bytes": self.max_bytes,
            "max_entries": self.max_entries,
            "unscanned": self.unscanned_bytes(),
        }

    def render(self, now: datetime | None = None) -> list[str]:
        """`render_notes` straight off the file."""
        now = now or fsio.utc_now()
        return self.render_notes(self.active(now=now), now=now, unscanned=self.unscanned_bytes())


def agent_notebook(home: Path, owner: str = "manager") -> Notebook:
    """The manager's notebook, or another agent's under `state/agents/<owner>/`.

    Rendered into the digest under `NOTES_MAX_ENTRIES` / `NOTES_MAX_BYTES`;
    only the owner and a human write it (`writers` is empty), which is what
    keeps the manager's memory free of task chatter.
    """
    return Notebook(
        path=notes_path(home, owner),
        owner=owner,
        owner_label=owner,
        writers=frozenset(),
        header=SECTION_HEADER,
        max_entries=NOTES_MAX_ENTRIES,
        max_bytes=NOTES_MAX_BYTES,
        empty_line=EMPTY_LINE,
        remember_cmd="quorum manager remember",
        forget_cmd="quorum manager forget",
        read_cmd="quorum manager notes",
        budget_name="digest",
        refusal_hint="reach it with `quorum task report` or `quorum board post attention` instead",
    )


def manager_writers(home: Path) -> frozenset[str]:
    """The agent names that count as "the manager" for a task's notebook.

    A task's notebook admits the manager as an extra writer, and the manager
    is an agent named in config: `[agents.boss] type = "manager"` runs the
    builtin under the name `boss`, and its harness is tagged
    `QUORUM_ACTOR=boss`, so matching the literal string "manager" would
    refuse a renamed manager every time. Config is read through
    `try_load_config`, which returns None for a file quorum cannot parse;
    that case falls back to the literal name rather than raising, because
    this is read on the path of every task run. The literal name is always
    included: it is the manager's default name and its historical state
    directory.
    """
    from .config import try_load_config

    names = {"manager"}
    config = try_load_config(home)
    if config is not None:
        names |= {n for n, agent in config.agents.items() if agent.type == "manager"}
    return frozenset(names)


def task_notebook(home: Path, task_id: str) -> Notebook:
    """A task's notebook: `tasks/<id>/notes.jsonl`, owned by the task's actor
    identity (`task-<id>`), which the runner sets on the task's harness.

    The manager may write it too — a standing instruction for a task's next
    run is the natural complement to a one-shot `task nudge` — and so may a
    human; any *other* task, and any prompt agent, is refused. Which names
    count as the manager comes from `manager_writers`, by configured type
    rather than by the literal name. It is
    rendered into the task's own prompt on every run under
    `TASK_NOTES_MAX_ENTRIES` / `TASK_NOTES_MAX_BYTES` and printed by
    `quorum task show`; the digest never carries it.
    """
    from .tasks import short_handle, task_dir

    handle = short_handle(task_id)
    return Notebook(
        path=task_dir(home, task_id) / "notes.jsonl",
        owner=task_actor(task_id),
        owner_label=f"task {handle}",
        writers=manager_writers(home),
        header=TASK_SECTION_HEADER,
        max_entries=TASK_NOTES_MAX_ENTRIES,
        max_bytes=TASK_NOTES_MAX_BYTES,
        empty_line=None,
        remember_cmd=f"quorum task remember {handle}",
        forget_cmd=f"quorum task forget {handle}",
        read_cmd=f"quorum task show {handle}",
        budget_name="prompt",
        refusal_hint=f"guide it with `quorum task nudge {handle}` instead",
    )


def expires_at(entry: dict) -> datetime | None:
    """When this note stops being true, or None when it never does."""
    ttl = entry.get("ttl_days")
    if not ttl:
        return None
    try:
        return fsio.parse_iso(str(entry["ts"])) + timedelta(days=float(ttl))
    except (KeyError, ValueError):
        return None  # unparseable ttl: keep the note rather than lose it


def describe(entry: dict, now: datetime | None = None) -> str:
    """One note as a digest line: handle, date, who wrote it, the text."""
    now = now or fsio.utc_now()
    text = entry.get("text", "")
    if len(text) > NOTE_MAX_CHARS:
        text = text[: NOTE_MAX_CHARS - 1].rstrip() + "…"
    when = str(entry.get("ts", ""))[:10]
    end = expires_at(entry)
    ttl = ""
    if end is not None:
        # rounded up: a note written a minute ago with --ttl 2 has two days
        # left, not one, and reading "expires in 0d" on a live note is worse
        # than a day of imprecision
        days = max(0, math.ceil((end - now).total_seconds() / 86400))
        ttl = f", expires in {days}d"
    note_id = entry.get("id")
    handle = short_id(note_id) if isinstance(note_id, str) else "??????"
    return f"- ({handle}) [{when}{ttl}] {entry.get('sender', '?')}: {text}"
