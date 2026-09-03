"""Reading a transcript: one narrative renderer, and the event vocabulary.

`transcript.jsonl` is what a harness said, line by line, as `stream_transcript`
captured it: `{"at": ..., "line": <raw stdout>}` for text a harness printed,
`{"at": ..., "event": <parsed JSON>}` for a structured stream. That file is
the record, and it is unreadable — a claude run is a few hundred events of
nested `tool_use` payloads and echoed tool output, and the questions a person
asks of it ("what did it try, what came back, why did it stop") take `jq`.

This module answers them without changing the record. It is a **pure reader**:
nothing here writes, and the rendering is never cached — `task tail`, `task
log`, `manager log`, the TUI's transcript pane and the web dashboard all call
`render()` on entries they read themselves, so the three surfaces agree by
construction rather than by convention.

Three properties it is built around, the same shape `usage.py` has:

- **One place for harness shapes.** Every "which harness spells it how"
  decision lives here — `tool_call`, `session_id`, `normalize`. `usage.py`
  already owns that seam for the result events, so result rendering reads
  its `usage_from_event` rather than re-deriving cost, and `manager.py`'s
  loop signal reads `tool_call` rather than its own copy of the vocabulary.
  No harness-specific code anywhere else.
- **Fail-soft, always.** An event this module does not recognize renders as
  its raw JSON line, and a malformed entry renders as its `repr` — never a
  raise. This runs inside a dashboard's refresh loop and inside `-f` tails,
  where an exception on an event shape a harness added last week would take
  the whole surface down.
- **The raw output survives.** `raw_entry` is the line `task tail` printed
  before this module existed, byte for byte, and `--raw` still prints it —
  anything grepping a transcript keeps working.

What "narrative" means, concretely: assistant text in full (it is the run's
reasoning, and the whole point of reading a transcript), each tool call as one
line with its first argument trimmed, each tool result collapsed to a size or
exit code, and the events that carry no story — a `system` init's tool list, a
progress ping, an allowed rate-limit notice — folded away. `-v` unfolds all of
it: thinking blocks, full arguments, full results, the noise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import actor, fsio, usage

# --- the event vocabulary ----------------------------------------------------
# Loose on purpose, and matched against an event's "type"/"item_type": a
# harness quorum has never seen still gets its tool calls read, and one that
# spells them some third way degrades to raw lines rather than to nothing.

TOOL_CALL_KINDS = frozenset(
    {"tool_use", "tool_call", "function_call", "command_execution", "local_shell_call"}
)
TOOL_NAME_KEYS = ("name", "tool_name", "tool")
TOOL_ARG_KEYS = ("input", "arguments", "argv", "args", "parameters", "params", "command", "cmd")
# A harness may emit more than one event per call (codex pairs item.started
# with item.completed, both carrying the full call); the call id is how one
# call is counted once, whatever the event multiplicity.
CALL_ID_KEYS = ("id", "call_id", "tool_use_id")

# What a harness calls the identifier of the conversation it is continuing:
# claude's `session_id`, codex's `thread_id`.
SESSION_ID_KEYS = ("session_id", "sessionId", "thread_id", "threadId")

# The events that announce a run's start — the first thing a reader wants and
# the only place the session id is guaranteed to appear.
START_EVENT_TYPES = frozenset({"thread.started", "session.created", "run.started"})

# The result events worth a line of their own. `usage.py` owns the wider set
# (it must catch every event that reports spend, including codex's repeated
# mid-run `token_count`); a *narrative* wants only the ones that end a turn,
# so the incremental ones stay folded as noise rather than printing a
# "result" every few seconds.
INCREMENTAL_RESULT_TYPES = frozenset({"token_count", "usage"})
RESULT_EVENT_TYPES = usage.RESULT_EVENT_TYPES - INCREMENTAL_RESULT_TYPES

# Structured events that say nothing a person reading a run needs: progress
# pings, token estimates, the init banner's tool list, stream deltas.
NOISE_EVENT_TYPES = frozenset(
    {
        "system",
        "stream_event",
        "tool_progress",
        "item.updated",
        "turn.started",
        "thread.updated",
    }
)

# How much of a session id identifies it on the start line: a uuid by its
# head, anything shorter than two of those in full.
SESSION_ID_CHARS = 8

# The prefix `runner.note_transcript` writes on quorum's own transcript lines
# (worktree prepared, run stopped, auto-commit, stall watchdog).
NOTE_PREFIX = "quorum: "

# --- rendering budget --------------------------------------------------------
# Truncation lengths, not limits on anything: every one of them is lifted by
# `-v`, and the underlying file is never touched.
TOOL_SUMMARY_CHARS = 100
RESULT_PREVIEW_CHARS = 120
DETAIL_MAX_CHARS = 4000

KIND_ICONS = {
    "start": "▶",
    "text": "💬",
    "thinking": "🤔",
    "tool": "🔧",
    "tool_result": "  ↳",
    "note": "•",
    "status": "⏸",
    "error": "⚠",
    "result": "■",
    "raw": "?",
    "noise": "·",
}
# Rendered only under -v: reasoning, and the events that carry no story.
VERBOSE_KINDS = frozenset({"thinking", "noise"})


@dataclass(frozen=True)
class Line:
    """One rendered beat of a run: what it was, and what it said."""

    kind: str
    text: str
    at: str = ""
    detail: str = ""  # the full payload, printed only under -v

    @property
    def icon(self) -> str:
        return KIND_ICONS.get(self.kind, KIND_ICONS["raw"])


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation as some harness spelled it."""

    name: str
    args: Any = None
    id: str | None = None


def session_id(event: object) -> str | None:
    """The harness session/thread id an event carries, or None."""
    if not isinstance(event, dict):
        return None
    for key in SESSION_ID_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def tool_call(node: object) -> ToolCall | None:
    """The tool call one dict announces, or None.

    Recognizes a dict tagged with a `TOOL_CALL_KINDS` kind or carrying a
    string `tool_name`, whatever harness emitted it. Arguments are taken from
    the first `TOOL_ARG_KEYS` hit and returned raw — callers decide whether to
    hash them (`manager.loop_signal`) or trim them for a line (`normalize`).
    """
    if not isinstance(node, dict):
        return None
    kinds = {str(node.get(k)) for k in ("type", "item_type") if node.get(k) is not None}
    matched = sorted(kinds & TOOL_CALL_KINDS)
    if not matched and not isinstance(node.get("tool_name"), str):
        return None
    name = next(
        (str(node[k]) for k in TOOL_NAME_KEYS if isinstance(node.get(k), str)),
        matched[0] if matched else "tool",
    )
    args = next((node[k] for k in TOOL_ARG_KEYS if k in node), None)
    call_id = next((str(node[k]) for k in CALL_ID_KEYS if node.get(k) is not None), None)
    return ToolCall(name=name, args=args, id=call_id)


# --- rendering helpers -------------------------------------------------------


def _clock(at: object) -> str:
    """`HH:MM:SS` from an entry's timestamp; `--:--:--` when it has none."""
    text = str(at or "")
    try:
        return fsio.parse_iso(text).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        pass
    # A hand-written or foreign stamp still usually has a clock in it.
    if "T" in text and len(text) >= 19:
        return text[11:19]
    return "--:--:--"


def _flatten(value: object) -> str:
    """One line of text for `value`, whatever shape it arrived in."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = " ".join(_flatten(v) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return " ".join(text.split())


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _detail(value: object) -> str:
    """The full payload for `-v`: pretty when it is JSON, bounded either way."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(value)
    if len(text) > DETAIL_MAX_CHARS:
        return text[:DETAIL_MAX_CHARS] + f"\n… ({len(text) - DETAIL_MAX_CHARS} more characters)"
    return text


def _bytes(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} MB"
    if count >= 1_000:
        return f"{count / 1_000:.1f} kB"
    return f"{count} B"


def _size_note(text: str) -> str:
    lines = text.count("\n") + 1 if text else 0
    plural = "" if lines == 1 else "s"
    return f"{lines} line{plural} · {_bytes(len(text.encode('utf-8', 'replace')))}"


def _text_of(block: dict) -> str:
    """The human text a content block carries, whatever key holds it."""
    for key in ("text", "thinking", "content", "message", "summary"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            joined = "\n".join(
                b.get("text", "") for b in value if isinstance(b, dict) and b.get("text")
            )
            if joined.strip():
                return joined
    return ""


# --- normalization -----------------------------------------------------------


# The argument a tool call is *about*, in the order a reader wants it: what
# was run, then what was read or written, then what was asked. Anything with
# no hit falls back to the first string it carries, so an unknown tool still
# gets a line worth reading rather than a JSON blob.
TOOL_ARG_PRIORITY = (
    "command",
    "cmd",
    "argv",
    "file_path",
    "path",
    "notebook_path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)


def _arg_summary(args: object) -> str:
    """The one argument worth putting on a tool call's line."""
    if not isinstance(args, dict):
        return _flatten(args)
    for key in TOOL_ARG_PRIORITY:
        if args.get(key):
            return _flatten(args[key])
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _flatten(value)
    return _flatten(args)


def _edit_delta(args: object) -> str:
    """`(+3 −1)` for a call that replaces one block of text with another."""
    if not isinstance(args, dict):
        return ""
    before, after = args.get("old_string"), args.get("new_string")
    if not isinstance(before, str) or not isinstance(after, str):
        return ""
    return f"  (+{len(after.splitlines())} −{len(before.splitlines())})"


def _tool_line(call: ToolCall, at: str) -> Line:
    summary = _trim(_arg_summary(call.args), TOOL_SUMMARY_CHARS)
    text = f"{call.name}  {summary}" if summary else call.name
    return Line("tool", text + _edit_delta(call.args), at, detail=_detail(call.args))


def _tool_result_line(block: dict, at: str) -> Line:
    body = block.get("content")
    if body is None:
        body = block.get("output", block.get("aggregated_output", ""))
    text = body if isinstance(body, str) else _text_of({"content": body}) or _flatten(body)
    exit_code = block.get("exit_code", block.get("exitCode"))
    parts = []
    if block.get("is_error") or block.get("isError"):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        parts.append(f"error: {_trim(first, RESULT_PREVIEW_CHARS)}" if first else "error")
    if isinstance(exit_code, int):
        parts.append(f"exit {exit_code}")
    parts.append(_size_note(text))
    return Line("tool_result", " · ".join(parts), at, detail=_detail(text))


def _result_line(event: dict, at: str) -> Line:
    parts = []
    subtype = event.get("subtype") or event.get("status") or event.get("stop_reason")
    if event.get("is_error"):
        parts.append("FAILED")
    if isinstance(subtype, str) and subtype:
        parts.append(subtype)
    turns = event.get("num_turns")
    if isinstance(turns, int):
        parts.append(f"{turns} turns")
    duration_ms = event.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        parts.append(usage.format_duration(duration_ms / 1000))
    spend = usage.describe(usage.usage_from_event(event))
    if spend:
        parts.append(spend)
    said = _flatten(event.get("result"))
    detail = _detail(event.get("result")) if said else ""
    return Line("result", "result: " + (" · ".join(parts) or "(no detail)"), at, detail=detail)


def _rate_limit_line(event: dict, at: str) -> Line | None:
    """A rate-limit notice, or None when it says the run may continue.

    The common case is a status the harness handled by itself; printing one
    per event would bury the story. Anything not marked allowed is the run
    being throttled, which is exactly what a person reading it wants to see.
    """
    info = event.get("rate_limit_info")
    info = info if isinstance(info, dict) else event
    status = str(info.get("overageStatus") or info.get("status") or "")
    kind = str(info.get("rateLimitType") or info.get("type") or "rate limit")
    windows = info.get("unifiedWindows")
    share = ""
    if isinstance(windows, dict):
        window = windows.get(kind)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            share = f" {round(window['utilization'] * 100)}%"
    if status == "allowed":
        return None
    return Line("status", f"rate limit: {kind}{share} ({status or 'throttled'})", at,
                detail=_detail(event))


def normalize(entry: object) -> list[Line]:
    """One transcript entry as the beats it contains. Never raises.

    An entry usually renders to one line; a claude `assistant` event carrying
    text and two tool calls renders to three, in the order the harness sent
    them. An entry this module cannot read at all renders to a `raw` line —
    the JSON, unchanged, which is what the reader would have seen anyway.
    """
    try:
        return _normalize(entry)
    except Exception:  # every caller is a surface a raise would take down
        return [Line("raw", _flatten(entry))]


def _normalize(entry: object) -> list[Line]:
    if not isinstance(entry, dict):
        return [Line("raw", _flatten(entry))]
    at = str(entry.get("at", ""))
    if "event" not in entry:
        line = str(entry.get("line", ""))
        if line.startswith(NOTE_PREFIX):
            return [Line("note", line[len(NOTE_PREFIX):], at)]
        return [Line("raw", line, at)] if line else []
    event = entry.get("event")
    if not isinstance(event, dict):
        return [Line("raw", _flatten(event), at)]

    kinds = {str(event.get(k)) for k in ("type", "item_type") if event.get(k) is not None}
    subtype = str(event.get("subtype") or "")
    raw = Line("raw", _flatten(event), at, detail=_detail(event))

    # Run start: the session id, and the harness's own idea of where it is.
    # claude's banner is `system`/`init`; a harness that just announces its
    # session on a bare `system` event (the shipped fake, and anything else
    # keeping it simple) means the same thing. A *subtyped* system event is
    # something else — claude tags every one of them with the session id too,
    # and a token-estimate ping is not the start of a run.
    session = session_id(event)
    if (kinds & START_EVENT_TYPES) or (
        "system" in kinds and (subtype == "init" or (not subtype and session))
    ):
        where = event.get("cwd") or event.get("workdir") or ""
        # a uuid is recognizable by its head; a short id has to stay whole
        short = session[:SESSION_ID_CHARS] if len(session or "") > 2 * SESSION_ID_CHARS else session
        parts = [p for p in (f"session {short}" if session else "", str(where)) if p]
        return [Line("start", "run started" + (f" ({' · '.join(parts)})" if parts else ""), at,
                     detail=_detail(event))]

    if kinds & RESULT_EVENT_TYPES:
        return [_result_line(event, at)]

    if "rate_limit_event" in kinds or "rate_limit" in kinds:
        line = _rate_limit_line(event, at)
        return [line] if line else [Line("noise", _trim(raw.text, RESULT_PREVIEW_CHARS), at,
                                        detail=raw.detail)]

    if any("error" in k for k in kinds) or event.get("is_error") is True:
        return [Line("error", _trim(_flatten(event), RESULT_PREVIEW_CHARS), at,
                     detail=_detail(event))]

    # Folded before the blocks are read: a `system` event carries fields that
    # look like text (a subagent notification's `summary`), and a progress
    # ping is not part of the story whatever it happens to contain.
    if kinds & NOISE_EVENT_TYPES:
        return [Line("noise", _trim(raw.text, RESULT_PREVIEW_CHARS), at, detail=raw.detail)]

    blocks = _content_blocks(event)
    lines = [line for block in blocks for line in _block_lines(block, at)]
    if lines:
        return lines
    # Blocks that render to nothing are silence, not an unknown shape: an
    # assistant turn whose only content is a redacted (empty) thinking block
    # must not dump its whole signature into the narrative.
    if blocks:
        return [Line("noise", _trim(raw.text, RESULT_PREVIEW_CHARS), at, detail=raw.detail)]
    return [raw]


def _content_blocks(event: dict) -> list[dict]:
    """The payload dicts an event carries: claude's message content blocks,
    codex's `item`, or the event itself when it is its own block."""
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return [b for b in content if isinstance(b, dict)]
        if isinstance(content, str) and content.strip():
            return [{"type": str(event.get("type") or "text"), "text": content}]
    item = event.get("item")
    if isinstance(item, dict):
        return [{**item, "_event_type": str(event.get("type") or "")}]
    if tool_call(event) is not None or _text_of(event):
        return [event]
    return []


def _block_lines(block: dict, at: str) -> list[Line]:
    kinds = {str(block.get(k)) for k in ("type", "item_type") if block.get(k) is not None}
    if kinds & {"tool_result", "function_call_output", "local_shell_call_output"}:
        return [_tool_result_line(block, at)]
    call = tool_call(block)
    if call is not None:
        # codex announces a command twice — `item.started` carries the call,
        # `item.completed` the same call plus its output. Render the second as
        # the result it is, so one command is one call and one outcome.
        if str(block.get("_event_type", "")).endswith(".completed") and (
            "exit_code" in block or "aggregated_output" in block or "output" in block
        ):
            return [_tool_line(call, at), _tool_result_line(block, at)]
        return [_tool_line(call, at)]
    if "thinking" in kinds or "reasoning" in kinds:
        text = _text_of(block)
        return [Line("thinking", text, at)] if text else []
    text = _text_of(block)
    if text:
        return [Line("text", text, at)]
    return []


# --- rendering ---------------------------------------------------------------


def raw_entry(entry: dict) -> str:
    """One transcript entry the way `task tail` printed it before this module
    existed. `--raw` is this, byte for byte."""
    at = str(entry.get("at", "")).replace("T", " ").rstrip("Z")
    if "line" in entry:
        return f"[{at}] {entry['line']}"
    return f"[{at}] {json.dumps(entry.get('event'), ensure_ascii=False)}"


def render_line(line: Line, *, verbose: bool = False) -> list[str]:
    """One normalized beat as terminal lines (its text may be a paragraph)."""
    stamp = f"[{_clock(line.at)}] " if line.at else ""
    indent = " " * (len(stamp) + 2)
    body = line.text.splitlines() or [""]
    out = [f"{stamp}{line.icon} {body[0]}"] + [f"{indent}{ln}" for ln in body[1:]]
    if verbose and line.detail and line.detail.strip() != line.text.strip():
        out += [f"{indent}| {ln}" for ln in line.detail.splitlines()]
    return out


def render(entries: list, *, verbose: bool = False, raw: bool = False) -> list[str]:
    """A transcript as a story. `raw` prints the file's own lines instead.

    Pure: `entries` are the parsed JSONL a caller already read (a tail, a
    whole file, one run's slice), and nothing here reaches for more.
    """
    if raw:
        return [raw_entry(e) if isinstance(e, dict) else _flatten(e) for e in entries]
    out = []
    for entry in entries:
        for line in normalize(entry):
            if line.kind in VERBOSE_KINDS and not verbose:
                continue
            out.extend(render_line(line, verbose=verbose))
    return out


# --- one agent run, end to end ----------------------------------------------
# The manager's tick is four files: the digest snapshot it was rendered from,
# its transcript, the actions the CLI journaled for it, and the ledger line
# saying how it ended. Reading them together is what makes "why did it launch
# that / not launch that" answerable after the fact.

# How much of a tick's digest snapshot prints without -v. The snapshot is
# already bounded on disk; this keeps the narrative on screen.
SNAPSHOT_PREVIEW_LINES = 30
# How many runs back the ledger and transcript are scanned for run ids.
RUN_SCAN_ENTRIES = 400


def read_snapshot(home: Path, name: str, run_id: str) -> str:
    """The digest (or prompt) a run was given, or "" when none was kept."""
    try:
        return actor.run_snapshot_path(home, name, run_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def run_ids(home: Path, name: str, limit: int = 20) -> list[str]:
    """The agent's most recent run ids, oldest last.

    Read from the usage ledger (one line per finished run, failures included)
    and the transcript (which carries the id of a run still in flight), so a
    tick you are watching right now is listed like any other.
    """
    seen: dict[str, None] = {}
    for entry in fsio.read_jsonl_tail(actor.usage_path(home, name), limit=RUN_SCAN_ENTRIES):
        if isinstance(entry, dict) and isinstance(entry.get("run"), str) and entry["run"]:
            seen[entry["run"]] = None
    for entry in fsio.read_jsonl_tail(actor.transcript_path(home, name), limit=RUN_SCAN_ENTRIES):
        if isinstance(entry, dict) and isinstance(entry.get("run"), str) and entry["run"]:
            seen.setdefault(entry["run"], None)
    ids = sorted(seen)  # run ids are ULIDs: lexicographic order is chronological
    return ids[-limit:] if limit > 0 else ids


def run_entries(home: Path, name: str, run_id: str, limit: int = 2000) -> list[dict]:
    """The transcript entries one run wrote."""
    entries = fsio.read_jsonl_tail(
        actor.transcript_path(home, name), limit=limit, max_bytes=4 * 1024 * 1024
    )
    return [e for e in entries if isinstance(e, dict) and e.get("run") == run_id]


def run_actions(home: Path, name: str, run_id: str, limit: int = 400) -> list[dict]:
    """The journal entries the CLI wrote for one run."""
    entries = fsio.read_jsonl_tail(actor.journal_path(home, name), limit=limit)
    return [e for e in entries if isinstance(e, dict) and e.get("run") == run_id]


def run_ledger(home: Path, name: str, run_id: str) -> dict | None:
    """The usage-ledger line for one run, or None while it is still running."""
    entries = fsio.read_jsonl_tail(actor.usage_path(home, name), limit=RUN_SCAN_ENTRIES)
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get("run") == run_id:
            return entry
    return None


def _action_lines(home: Path, entries: list[dict]) -> list[str]:
    """Journaled actions with their then-vs-now outcome — the same reading the
    next digest gives the manager, so `manager log` and the digest agree."""
    from .tasks import TaskStore

    try:
        by_short = {t.short_id: t for t in TaskStore(home).list()}
    except Exception:
        by_short = {}
    out = []
    for e in entries:
        target = str(e.get("target") or "")
        line = f"[{_clock(e.get('at'))}] {e.get('action', '')}"
        if target:
            line += f" -> {target}"
        if e.get("args"):
            line += f"  {_trim(_flatten(e['args']), TOOL_SUMMARY_CHARS)}"
        then = e.get("target_status")
        if target and then:
            now = by_short[target].status if target in by_short else "-"
            changed = "unchanged" if then == now else f"{then} -> {now}"
            line += f"  [{changed}]"
        out.append(line)
    return out or ["(no actions journaled)"]


def render_run(
    home: Path, name: str, run_id: str, *, verbose: bool = False, raw: bool = False
) -> list[str]:
    """One agent tick end to end: what it saw, what it said, what it did.

    Every section degrades to a note rather than an error: a run whose
    snapshot has aged out of the bounded directory, or that died before
    writing a ledger line, still renders everything else.
    """
    entries = run_entries(home, name, run_id)
    if raw:
        return render(entries, raw=True)
    ledger = run_ledger(home, name, run_id)
    when = (ledger or {}).get("at") or next(
        (str(e.get("at", "")) for e in entries if isinstance(e, dict)), ""
    )
    out = [f"=== {name} run {run_id}" + (f" — {when}" if when else "")]

    snapshot = read_snapshot(home, name, run_id)
    out.append("")
    out.append(f"--- what it saw ({actor.run_snapshot_path(home, name, run_id).name})")
    if not snapshot.strip():
        out.append("(no snapshot kept for this run)")
    else:
        lines = snapshot.splitlines()
        shown = lines if verbose else lines[:SNAPSHOT_PREVIEW_LINES]
        out.extend(shown)
        if len(lines) > len(shown):
            out.append(f"… ({len(lines) - len(shown)} more lines; -v for all)")

    out += ["", "--- what it said"]
    said = render(entries, verbose=verbose)
    out.extend(said or ["(no transcript for this run)"])

    out += ["", "--- what it did"]
    out.extend(_action_lines(home, run_actions(home, name, run_id)))

    out += ["", "--- how it ended"]
    out.append(describe_ledger(ledger))
    return out


def describe_ledger(ledger: dict | None) -> str:
    """`ok · 2m10s · $0.42 · 120k tok`, or why there is nothing to say."""
    if not ledger:
        return "(still running, or the run ended before it could record)"
    parts = [str(ledger.get("outcome") or "?")]
    seconds = ledger.get("duration_seconds")
    if isinstance(seconds, (int, float)):
        parts.append(usage.format_duration(seconds))
    spent = usage.describe(ledger.get("usage") if isinstance(ledger.get("usage"), dict) else None)
    if spent:
        parts.append(spent)
    return " · ".join(parts)
