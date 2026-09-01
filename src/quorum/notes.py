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
- **Its own digest budget.** `render_section` bounds the notebook with
  `NOTES_MAX_ENTRIES` / `NOTES_MAX_BYTES`, which nothing else in the digest
  spends. Ten noisy tasks with long report tails cannot shrink it.

Writes are append-only: a note is one line, and `forget` appends a
*tombstone* rather than rewriting history, so a reader only ever appends or
reads. Nothing is compacted or summarized in Python — when the notebook
outgrows its budget the digest says how many older notes it dropped, and the
manager's prompt tells it to consolidate: one superseding note, then forget
the rest. That is policy, and policy lives in prompts.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import fsio
from .actor import notes_path

# The notebook's slot in the digest. These are *independent* of the task
# section's budget by design (that is the whole point of the reserved slot),
# and they are plain constants rather than config for the same reason the
# loop thresholds are: getting one wrong costs a chattier digest, never a
# lost note — the file keeps everything, only the rendering is bounded.
NOTES_MAX_ENTRIES = 20
NOTES_MAX_BYTES = 4000
# One note that is somehow book-length must not eat the whole slot, and must
# not push the newest note out either: lines are truncated, entries dropped.
NOTE_MAX_CHARS = 600
# The file is append-only and unbounded in principle, but a consolidated
# notebook is small; readers take a bounded tail like every other jsonl log
# here. A note older than this window is invisible (and so is its tombstone,
# which is why the pair stays consistent).
NOTES_SCAN_BYTES = 256 * 1024

SECTION_HEADER = "## Your notebook (standing notes to yourself)"
EMPTY_LINE = (
    '(empty — `quorum manager remember "<fact>"` writes a standing note that '
    "every future run of yours will read)"
)


class NotebookError(Exception):
    """A refused or unresolvable notebook operation; the CLI renders it."""


def short_id(note_id: str) -> str:
    """The handle `remember` prints and `forget` accepts: the ULID's random
    tail, for the same reason `Task.short_id` is (the head is a shared
    timestamp)."""
    return note_id[-6:].lower()


def may_write(actor_name: str, owner: str) -> bool:
    """Who may write into `owner`'s notebook: that agent itself, or a human.

    Everyone else — a task run, another agent — is refused. Tasks reach the
    manager through `task report` and the board; letting them write into its
    memory would recreate the write-side crowding the separate file exists to
    prevent. The runner already strips the actor tag from task runs, so this
    is the second, cheaper fence rather than the only one.
    """
    return actor_name == "user" or actor_name == owner


def _check_owner(owner: str) -> None:
    """A notebook is written under `state/agents/<owner>/`, so an owner name
    is a path component: hold it to the same rules as any agent name (the
    manager's historical spot is the one exemption)."""
    if owner == "manager":
        return
    from .config import ConfigError, validate_agent_name

    try:
        validate_agent_name(owner)
    except ConfigError as e:
        raise NotebookError(str(e)) from None


def _entries(home: Path, owner: str = "manager") -> list[dict]:
    return [
        e
        for e in fsio.read_jsonl_tail(notes_path(home, owner), max_bytes=NOTES_SCAN_BYTES)
        if isinstance(e, dict) and e.get("id")
    ]


def expires_at(entry: dict) -> datetime | None:
    """When this note stops being true, or None when it never does."""
    ttl = entry.get("ttl_days")
    if not ttl:
        return None
    try:
        return fsio.parse_iso(str(entry["ts"])) + timedelta(days=float(ttl))
    except (KeyError, ValueError):
        return None  # unparseable ttl: keep the note rather than lose it


def active(home: Path, owner: str = "manager", now: datetime | None = None) -> list[dict]:
    """Unretired, unexpired notes, oldest first — the notebook as it reads."""
    now = now or fsio.utc_now()
    entries = _entries(home, owner)
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


def resolve(home: Path, handle: str, owner: str = "manager") -> dict:
    """Find one note by full id, unique prefix, or unique suffix (what
    `short_id` hands out) — the same handle rules as tasks."""
    wanted = handle.strip().upper()
    matches = [
        e
        for e in active(home, owner)
        if e["id"] == wanted or e["id"].startswith(wanted) or e["id"].endswith(wanted)
    ]
    if not matches:
        raise NotebookError(f"no note matching {handle!r} — `quorum manager notes`")
    if len(matches) > 1:
        raise NotebookError(
            f"note handle {handle!r} is ambiguous: "
            + ", ".join(short_id(e["id"]) for e in matches)
        )
    return matches[0]


def remember(
    home: Path,
    text: str,
    owner: str = "manager",
    sender: str = "user",
    run_id: str = "",
    ttl_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Append a standing note to `owner`'s notebook. Raises on a refused sender."""
    _check_owner(owner)
    if not may_write(sender, owner):
        raise NotebookError(
            f"{sender} may not write to {owner}'s notebook — reach it with "
            "`quorum task report` or `quorum board post attention` instead"
        )
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
    fsio.append_jsonl(notes_path(home, owner), entry)
    return entry


def forget(
    home: Path,
    handle: str,
    owner: str = "manager",
    sender: str = "user",
    run_id: str = "",
    now: datetime | None = None,
) -> dict:
    """Retire a note by appending a tombstone; readers hide both lines."""
    _check_owner(owner)
    if not may_write(sender, owner):
        raise NotebookError(f"{sender} may not write to {owner}'s notebook")
    note = resolve(home, handle, owner)
    fsio.append_jsonl(
        notes_path(home, owner),
        {
            "id": note["id"],
            "ts": fsio.iso(now or fsio.utc_now()),
            "run_id": run_id,
            "sender": sender,
            "retired": True,
        },
    )
    return note


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
    return f"- ({short_id(entry['id'])}) [{when}{ttl}] {entry.get('sender', '?')}: {text}"


def render_section(notes: list[dict], now: datetime | None = None) -> list[str]:
    """The notebook as digest lines, under the notebook's own caps.

    Over the cap the *newest* notes are kept — a standing fact written today
    outranks one from a month ago — and the count of dropped ones is stated
    where they would have been, so the manager can see it has consolidating
    to do. Summarizing them is deliberately not attempted here: the prompt
    tells the manager to write one superseding note and forget the rest.
    """
    now = now or fsio.utc_now()
    lines = [SECTION_HEADER]
    if not notes:
        return lines + [EMPTY_LINE]
    kept = notes[-NOTES_MAX_ENTRIES:]
    rendered = [describe(e, now) for e in kept]
    # Byte cap second: entries are dropped oldest-first, but the newest note
    # always survives (truncated by `describe` if it has to be).
    while len(rendered) > 1 and sum(len(line) + 1 for line in rendered) > NOTES_MAX_BYTES:
        rendered.pop(0)
    dropped = len(notes) - len(rendered)
    if dropped:
        lines.append(
            f"({dropped} older note(s) dropped — the notebook is over its digest budget: "
            'consolidate with one superseding `quorum manager remember "…"`, then '
            "`quorum manager forget <id>` the rest)"
        )
    return lines + rendered


def digest_section(
    home: Path, owner: str = "manager", now: datetime | None = None
) -> list[str]:
    """`render_section` straight off the files — what the digest and
    `quorum manager notes` both call."""
    now = now or fsio.utc_now()
    return render_section(active(home, owner, now=now), now=now)
