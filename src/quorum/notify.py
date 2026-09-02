"""The notification hook: a `[notify]` argv template fired once per new
board message on the listed topics (default: `attention`).

The `attention` topic is the one channel meant to reach a person — the
manager escalates there, the supervisor posts `agent.failing` there — and
the banners (`quorum status`, the TUI, the web header) only show it to
someone who is already looking. This module is what makes the board reach
out: a *consumer* of the board in the documented pattern (a private cursor
per topic in `state/notify.json`, the last filename processed; the board
carries no read marks), run by a small supervisor job on the control
cadence, so a message posted while the supervisor is down is delivered on
the next start, oldest first, and nothing is delivered twice.

Three rails, each the shape of an existing one:

- **The board stays the source of truth.** No queue, no retry store: the
  cursor is the whole state, and it advances — and is persisted — *before*
  the hook runs, whether or not delivery then succeeds. That makes
  delivery at-most-once on purpose: a desktop notification lost because
  the process died mid-hook is a far smaller failure than one that
  repeats every 15 seconds forever because the cursor write is what
  failed. A notification that cannot be delivered must not block the ones
  behind it.
- **No decisions in Python.** The hook fires on topic membership, never on
  content — what is escalation-worthy stays prompt policy.
- **Fails soft**, herdr's mold rather than sandbox.py's: a missing binary,
  a nonzero exit or a hang past `timeout_seconds` is one line in
  `logs/supervisor.log` and nothing else. It can never fail a tick, a
  board post, or the supervisor.

The template runs with the supervisor's environment, exactly as a harness
does — this is not a security boundary (the sandbox is).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import traceback
from pathlib import Path

from . import fsio
from .config import NotifyConfig
from .messages import Message, MessageBus

STATE_FILE = "state/notify.json"
PLACEHOLDERS = ("text", "from", "topic", "type", "id")
# Deliveries attempted per topic per tick. Each one may spend up to
# `timeout_seconds`, and the job runs every CONTROL_POLL_SECONDS — a listed
# topic that is suddenly busy (someone lists `tasks`) must not wedge the
# job's thread for an hour. The cursor advances per message, so the rest
# simply wait for the next tick.
MAX_PER_TICK = 25
# One drain at a time, whoever asks. The supervisor calls `drain` from two
# places — the startup catch-up and the interval job — and APScheduler's
# `max_instances=1` only guards the job against itself, never against the
# main thread. Two drains sharing one cursor would each read it, each
# deliver, and the second would overwrite the first's advance. Non-blocking:
# a skipped tick is a tick, and the next one is 15 seconds away.
_drain_lock = threading.Lock()

log = logging.getLogger("quorum.notify")


# -- the cursor ----------------------------------------------------------------


def state_path(home: Path) -> Path:
    return Path(home) / STATE_FILE


def load_cursors(home: Path) -> dict[str, str] | None:
    """The per-topic cursors, `{}` for a hook that has never run, or None
    when the file exists but cannot be read (the caller re-initializes it —
    a broken cursor loses at most what was pending, never every later
    tick)."""
    path = state_path(home)
    if not path.exists():
        return {}
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        return None
    cursors = data.get("cursors") if isinstance(data, dict) else None
    if not isinstance(cursors, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in cursors.items()
    ):
        return None
    return dict(cursors)


def save_cursors(home: Path, cursors: dict[str, str]) -> None:
    fsio.atomic_write_json(state_path(home), {"cursors": cursors})


# -- the template ----------------------------------------------------------------


def placeholder_values(message: Message) -> dict[str, str]:
    text = message.payload.get("text", "")
    return {
        "text": text if isinstance(text, str) else str(text),
        "from": message.sender,
        "topic": message.topic or "",
        "type": message.type,
        "id": message.id,
    }


def build_argv(command: list[str], message: Message) -> list[str]:
    """Substitute the placeholders element-wise, like a harness template.

    No shell is involved, so a text with spaces, quotes or `$` is one argv
    element and never an injection. A template with no "{text}" gets the
    text appended as the final argument, the same convention as a
    `[harness.*]` template without "{prompt}".
    """
    values = placeholder_values(message)
    argv = []
    for element in command:
        for key, value in values.items():
            element = element.replace("{" + key + "}", value)
        argv.append(element)
    if not any("{text}" in element for element in command):
        argv.append(values["text"])
    return argv


def deliver(command: list[str], message: Message, timeout: float) -> str | None:
    """Run the template once for `message`; None when it exited 0, else one
    line saying why not. Never raises."""
    argv = build_argv(command, message)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return f"{argv[0]!r} not found on PATH"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:g}s"
    except Exception as e:  # PermissionError, OSError, a non-str element...
        return f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        noise = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = f": {noise[-1].strip()}" if noise else ""
        return f"exit {proc.returncode}{tail}"
    return None


# -- the drain ------------------------------------------------------------------


def drain(home: Path, cfg: NotifyConfig, bus: MessageBus | None = None) -> int:
    """Deliver every listed-topic message posted since the cursor; returns
    how many deliveries were attempted. Never raises.

    The first drain ever — no `state/notify.json` — initializes each
    topic's cursor at its current tail *without* delivering: turning the
    hook on must not replay a month of old escalations, and the banner
    already shows the recent ones. Everything posted after that point is
    delivered, including anything that arrives while the supervisor is
    down. An unreadable state file is re-initialized the same way, with a
    log line, rather than raising every 15 seconds.

    Only one drain runs at a time; a caller that arrives while another is
    mid-batch does nothing rather than delivering the same messages again.
    """
    if not _drain_lock.acquire(blocking=False):
        log.debug("notify: a drain is already running — skipping this one")
        return 0
    try:
        return _drain(Path(home), cfg, bus or MessageBus(home))
    except Exception:
        # An unwritable state file, a board directory that vanished mid-scan:
        # one line, and the next tick tries again from the last saved cursor.
        log.error("notify: drain failed:\n%s", traceback.format_exc())
        return 0
    finally:
        _drain_lock.release()


def _drain(home: Path, cfg: NotifyConfig, bus: MessageBus) -> int:
    cursors = load_cursors(home)
    if cursors is None:
        log.warning("notify: %s is unreadable — re-initializing the cursor at the tail", STATE_FILE)
        cursors = {}
    attempted = 0
    for topic in cfg.topics:
        if topic not in cursors:
            # Only the name is wanted here: parsing the backlog to learn it
            # would be a month of escalations read to be discarded.
            tail = bus.topic_tail(topic)
            cursors[topic] = tail or ""
            save_cursors(home, cursors)
            if tail:
                log.info("notify: topic %r starts at %s — nothing older is delivered", topic, tail)
            continue
        entries = bus.entries_after_cursor(topic, cursors[topic], limit=MAX_PER_TICK)
        for filename, message in entries:
            # Advance *before* delivering, and persist it: a crash — or a
            # failed cursor write — then loses one notification instead of
            # repeating it at every tick forever. At-most-once is the right
            # trade for something whose whole job is to interrupt a person.
            cursors[topic] = filename
            save_cursors(home, cursors)
            if message is None:
                continue
            attempted += 1
            failure = deliver(cfg.command, message, cfg.timeout_seconds)
            if failure is None:
                log.info("notify: delivered %s/%s via %s", topic, filename, cfg.command[0])
            else:
                log.warning(
                    "notify: %s/%s not delivered (%s) — cursor advanced", topic, filename, failure
                )
    return attempted
