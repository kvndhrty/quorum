"""One archive of a task, for sharing a run or attaching it to a bug report.

Everything about a task sits under `tasks/<id>/`, its guidance under
`messages/inbox/task-<id>/` (and, once delivered, in the compacted
`messages/archive/`), and its code in a worktree — three places that all
have to be known to tar by hand. `quorum task export <id>` collects them
into one `.tar.gz`:

    quorum-task-<short-id>/
      export.json            what this archive is (task id, when, options)
      task.json              the record, exactly as on disk
      reports.jsonl          `quorum task report` entries
      transcript.jsonl       the harness's stdout (rewritten under --redact)
      runner.log             detached-run bootstrap output, when present
      attached.json          adopted-session liveness, when present
      <any subdirectory>/    a notebook or artifacts directory a future
                             feature adds next to the record is carried
                             along unchanged — the walk is over the whole
                             task directory, not a list of known names
      inbox/new/*.json       guidance still waiting to be claimed
      inbox/cur/*.json       guidance claimed by a run that has not acked it
      inbox/delivered.jsonl  guidance already delivered: every archived
                             message addressed to this task, oldest first
      worktree.diff          --with-worktree-diff: the worktree against the
                             branch it forked from

A **pure reader**: the only write is the output file, which defaults to the
current directory and is refused inside the home (an archive under
`tasks/<id>/` would be swept into the next export of the same task, and the
home's layout is documented state this command adds nothing to). Nothing
from the project directory is ever read — the diff comes from the task's
own worktree, and a task that ran in the user's checkout (`--no-worktree`,
attached) is refused the diff rather than exporting someone's checkout.

`runner.lock` is left out on purpose: it is a pid on this machine, not a
fact about the task, and an unpacked archive must not look like it holds a
live run.

`--redact` is for transcripts that contain what the tools *returned* — file
contents, command output, secrets read off disk. It keeps the assistant's
own text, its thinking, and every tool *call* (name and arguments, which is
how a reader follows what the run did) and replaces each tool *result* with
a marker. The shapes are the ones quorum already reads for its loop signal
(claude `tool_result` blocks and the `tool_use_result` field beside them,
codex `*_output` items and the output fields on a `command_execution`
item); a plain-text harness's `line` entries carry no structure to redact
and are kept verbatim, which the command says out loud.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsio, installed_version
from .tasks import Task, _worktree_base, inbox_name, task_dir

ARCHIVE_SUFFIX = ".tar.gz"
# Everything under the archive root is named from the short id: the full
# ULID is in export.json and task.json for anyone who needs it.
ROOT_PREFIX = "quorum-task-"

# The one file in a task directory that is not part of its record.
EXCLUDED_FILES = frozenset({"runner.lock"})

REDACTED = "[redacted by quorum task export --redact]"

# A dict tagged with one of these *is* a tool result: its output-bearing
# fields go, its identity (tool_use_id, is_error, call_id) stays so the
# result still lines up with the call it answers.
RESULT_KINDS = frozenset(
    {"tool_result", "function_call_output", "tool_call_output", "custom_tool_call_output"}
)
RESULT_KEYS = ("content", "output", "result", "stdout", "stderr", "aggregated_output")
# A dict tagged with one of these is a tool *call* that may carry its own
# result alongside (codex's `command_execution` item does): only the output
# fields are dropped, never the command or its arguments.
CALL_KINDS = frozenset(
    {"tool_use", "tool_call", "function_call", "command_execution", "local_shell_call"}
)
CALL_OUTPUT_KEYS = ("output", "aggregated_output", "stdout", "stderr", "result")
# Keys that hold a raw tool result wherever they appear (claude puts the
# structured `tool_use_result` on the user event, beside the message).
RESULT_KEYS_ANYWHERE = frozenset({"tool_use_result"})
# Past this depth a node is replaced rather than walked: the failure
# direction of a redaction has to be "dropped", never "kept".
REDACT_DEPTH = 32


class ExportError(RuntimeError):
    """A refusal named for the person running the command."""


@dataclass
class Redaction:
    """What `--redact` did to the transcript: counts a reader can check."""

    results: int = 0
    lines_kept: int = 0
    entries: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ExportEntry:
    """One archive member: its name in the tar and where it comes from —
    a file on disk (`source`) or bytes composed for the archive (`data`)."""

    arcname: str
    source: Path | None = None
    data: bytes | None = None


def default_output(task: Task, cwd: Path | None = None) -> Path:
    """Where the archive goes unless `--out` says otherwise: the current
    directory, named after the short id."""
    return Path(cwd or Path.cwd()) / f"{ROOT_PREFIX}{task.short_id}{ARCHIVE_SUFFIX}"


def output_refusal(out: Path, home: Path) -> str | None:
    """Why this output path must not be written, or None."""
    resolved = out.resolve()
    home_resolved = Path(home).resolve()
    if resolved == home_resolved or home_resolved in resolved.parents:
        return f"{out} is inside the quorum home {home} — write the archive somewhere else"
    if resolved.exists():
        return f"{out} already exists — pick another --out, or move it first"
    if not resolved.parent.is_dir():
        return f"{resolved.parent} is not a directory"
    return None


# -- what goes in ---------------------------------------------------------


def task_entries(home: Path, task: Task) -> list[ExportEntry]:
    """Every file under `tasks/<id>/`, recursively, in a stable order —
    minus the lock and tmp files a crashed atomic write may have left."""
    root = task_dir(home, task.id)
    if not root.is_dir():
        raise ExportError(f"no task directory at {root}")
    out: list[ExportEntry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not fsio.is_tmp(d))
        rel_dir = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            if fsio.is_tmp(name) or (rel_dir == Path(".") and name in EXCLUDED_FILES):
                continue
            path = Path(dirpath) / name
            if not path.is_file():
                continue  # a socket, a fifo: not a record
            out.append(ExportEntry(arcname=str(rel_dir / name), source=path))
    return out


def inbox_entries(home: Path, task: Task) -> list[ExportEntry]:
    """The task's live inbox: what is waiting (`new/`) and what a run has
    claimed but not acked (`cur/`)."""
    inbox = Path(home) / "messages" / "inbox" / inbox_name(task.id)
    out: list[ExportEntry] = []
    for sub in ("new", "cur"):
        for path in fsio.sorted_entries(inbox / sub):
            out.append(ExportEntry(arcname=f"inbox/{sub}/{path.name}", source=path))
    return out


def delivered_guidance(home: Path, task: Task) -> list[dict]:
    """Guidance this task already received, read back out of the compacted
    message archive: every record addressed to its inbox, oldest first.

    An acked inbox message is appended to `messages/archive/<YYYY-MM>.jsonl.gz`
    for the month it was acked in, which can only be the task's creation
    month or later — so only those files are opened, and a home with years
    of history is not decompressed whole for one task. A malformed line is
    skipped: this is a share, not a proof, and one torn line must not stop
    the archive.
    """
    archive = Path(home) / "messages" / "archive"
    if not archive.is_dir():
        return []
    try:
        floor = f"{fsio.parse_iso(task.created_at):%Y-%m}"
    except ValueError:
        floor = ""
    wanted = inbox_name(task.id)
    found: list[dict] = []
    for path in sorted(archive.glob("*.jsonl.gz")):
        if path.name[: len(floor)] < floor:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and record.get("to") == wanted:
                        found.append(record)
        except (OSError, EOFError):
            continue  # a truncated month is a skipped month, not a failed export
    found.sort(key=lambda r: (str(r.get("created_at", "")), str(r.get("id", ""))))
    return found


def worktree_diff(task: Task) -> str:
    """`git diff` of the task's worktree against the branch it forked from,
    untracked files included, as one patch. Read-only git; no fetch.

    Raises ExportError, naming why, when there is nothing to diff: the
    person asked for the diff, so silently omitting it would be the wrong
    kind of quiet. Refused outright for a task that did not run in a
    worktree quorum made — that workdir is the user's own checkout, and
    nothing from a project directory belongs in an export.
    """
    if task.attached or not task.use_worktree:
        raise ExportError(
            "the task ran in the project checkout, not a worktree quorum made — "
            "nothing from a project directory is exported"
        )
    if not task.workdir:
        raise ExportError("the task has no worktree yet (it has never run)")
    workdir = Path(task.workdir)
    if not workdir.is_dir():
        raise ExportError(f"the worktree {workdir} is gone (pruned?)")

    def git(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside is None:
        raise ExportError("git could not be run")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ExportError(f"{workdir} is not a git worktree")
    base = _worktree_base(git)
    if base is None:
        raise ExportError("no base branch to diff against (no remote, no upstream)")
    fork = git("merge-base", base, "HEAD")
    if fork is None or fork.returncode != 0 or not fork.stdout.strip():
        raise ExportError(f"no merge base between {base} and HEAD")
    fork_sha = fork.stdout.strip()
    diff = git("diff", "--no-color", "--no-ext-diff", fork_sha)
    if diff is None or diff.returncode != 0:
        raise ExportError("git diff failed: " + _trim(diff.stderr if diff else ""))
    parts = [
        f"# quorum task {task.short_id}: worktree {workdir}\n"
        f"# against {base} (merge base {fork_sha})\n",
        diff.stdout,
    ]
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    if untracked is not None and untracked.returncode == 0:
        for rel in sorted(p for p in untracked.stdout.split("\0") if p):
            # `--no-index` exits 1 when the files differ, which they always
            # do against /dev/null; only a missing patch is a failure.
            one = git("diff", "--no-color", "--no-ext-diff", "--no-index", "--", os.devnull, rel)
            if one is not None and one.stdout:
                parts.append(one.stdout)
    return "".join(parts)


# -- redaction ------------------------------------------------------------


def redact_transcript(entries: list[dict]) -> Redaction:
    """The transcript with every tool result replaced by a marker. Pure."""
    redaction = Redaction()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "event" in entry:
            copy = dict(entry)
            copy["event"] = _redact_node(entry["event"], redaction, 0)
            redaction.entries.append(copy)
        else:
            if "line" in entry:
                redaction.lines_kept += 1
            redaction.entries.append(entry)
    return redaction


def _redact_node(node: Any, redaction: Redaction, depth: int) -> Any:
    if depth > REDACT_DEPTH:
        redaction.results += 1
        return REDACTED
    if isinstance(node, list):
        return [_redact_node(item, redaction, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    kinds = {str(node.get(k)) for k in ("type", "item_type") if node.get(k) is not None}
    if kinds & RESULT_KINDS:
        out = dict(node)
        hit = False
        for key in RESULT_KEYS:
            if key in out:
                out[key] = REDACTED
                hit = True
        if hit:
            redaction.results += 1
        return out
    out = {}
    call = bool(kinds & CALL_KINDS)
    for key, value in node.items():
        if key in RESULT_KEYS_ANYWHERE or (call and key in CALL_OUTPUT_KEYS):
            out[key] = REDACTED
            redaction.results += 1
        else:
            out[key] = _redact_node(value, redaction, depth + 1)
    return out


# -- the archive ----------------------------------------------------------


def plan(
    home: Path,
    task: Task,
    with_worktree_diff: bool = False,
    redact: bool = False,
    now: Any = None,
) -> tuple[list[ExportEntry], Redaction | None]:
    """Every member the archive will hold, in order, plus what redaction
    did (None when not asked). Reads everything; writes nothing."""
    entries = task_entries(home, task)
    redaction: Redaction | None = None
    if redact:
        transcript = next((e for e in entries if e.arcname == "transcript.jsonl"), None)
        if transcript is not None and transcript.source is not None:
            redaction = redact_transcript(fsio.read_jsonl(transcript.source))
            body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in redaction.entries)
            entries[entries.index(transcript)] = ExportEntry(
                arcname="transcript.jsonl", data=body.encode("utf-8")
            )
        else:
            redaction = Redaction()
    entries.extend(inbox_entries(home, task))
    delivered = delivered_guidance(home, task)
    if delivered:
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in delivered)
        entries.append(ExportEntry(arcname="inbox/delivered.jsonl", data=body.encode("utf-8")))
    if with_worktree_diff:
        entries.append(ExportEntry(arcname="worktree.diff", data=worktree_diff(task).encode()))
    manifest = {
        "task": task.id,
        "short_id": task.short_id,
        "project": task.project,
        "exported_at": fsio.iso(now or fsio.utc_now()),
        "quorum": installed_version(),
        "redacted": redact,
        "worktree_diff": with_worktree_diff,
        "entries": [e.arcname for e in entries],
    }
    entries.insert(
        0,
        ExportEntry(
            arcname="export.json",
            data=(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        ),
    )
    return entries, redaction


def write_archive(out: Path, task: Task, entries: list[ExportEntry]) -> list[str]:
    """Write the tar.gz: built beside its final name and renamed into place,
    so a failure mid-way leaves no half-archive. Returns the member names."""
    out = Path(out)
    root = f"{ROOT_PREFIX}{task.short_id}"
    names: list[str] = []
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for entry in entries:
                arcname = f"{root}/{entry.arcname}"
                if entry.data is not None:
                    info = tarfile.TarInfo(arcname)
                    info.size = len(entry.data)
                    info.mtime = int(fsio.utc_now().timestamp())
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(entry.data))
                else:
                    assert entry.source is not None
                    tar.add(entry.source, arcname=arcname, recursive=False, filter=_anonymous)
                names.append(arcname)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return names


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip the local user and group from a member: an archive meant to be
    handed to someone else should not name this machine's account."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _trim(text: str) -> str:
    return " ".join(text.split())[:200]
