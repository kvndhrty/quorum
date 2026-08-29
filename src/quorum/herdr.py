"""Optional herdr adapter: the only module that talks to a herdr server.

herdr (https://herdr.dev) is a terminal multiplexer with first-class
awareness of interactive coding agents; it exposes a local socket API
(newline-delimited JSON over a unix socket). Quorum uses it two narrow ways
for *attached* tasks that live in a herdr pane:

- observation: the pane's agent status (idle/working/blocked/done) enriches
  the manager digest — a busy interactive session fires no hooks, so this
  beats mtime probes;
- the doorbell: `task nudge` pokes the pane so the session notices guidance
  is waiting. The payload stays in the task's maildir inbox — herdr is a
  doorbell, never a second transport, so delivery stays exactly-once no
  matter how many delivery points exist.

Unlike sandbox.py (fail-closed: no nono, no run), this module **fails
soft**: herdr being absent, stopped, or speaking an unexpected shape must
never break a digest or a nudge — every failure degrades to None/False.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

DEFAULT_SOCKET = "~/.config/herdr/herdr.sock"
TIMEOUT_SECONDS = 0.5


def _config(home: Path):
    """The optional [herdr] table, or None; never raises."""
    try:
        from .config import load_config

        return load_config(Path(home)).herdr
    except Exception:
        return None


def socket_path(home: Path) -> Path:
    cfg = _config(home)
    override = cfg.socket if cfg is not None else ""
    return Path(override or DEFAULT_SOCKET).expanduser()


def available(home: Path) -> bool:
    cfg = _config(home)
    if cfg is not None and not cfg.enabled:
        return False
    try:
        return socket_path(home).exists()
    except OSError:
        return False


def _call(home: Path, method: str, params: dict) -> dict | None:
    """One request/response over the socket; any failure → None."""
    if not available(home):
        return None
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(TIMEOUT_SECONDS)
            s.connect(str(socket_path(home)))
            req = {"id": "quorum", "method": method, "params": params}
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            with s.makefile("r", encoding="utf-8") as f:
                resp = json.loads(f.readline())
        result = resp.get("result") if isinstance(resp, dict) else None
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def agent_state(home: Path, pane_id: str) -> str | None:
    """The pane's detected agent status (idle/working/blocked/done/unknown),
    or None when herdr can't say."""
    result = _call(home, "pane.get", {"pane_id": pane_id})
    if not result:
        return None
    status = (result.get("pane") or {}).get("agent_status")
    return status if isinstance(status, str) and status else None


def ring_doorbell(home: Path, pane_id: str, text: str) -> bool:
    """Poke the pane: `agent.prompt` submits to a herdr-recognized agent; the
    fallback types the text into the pane *without* a newline (never execute
    anything in a pane we don't understand — a human presses enter)."""
    if _call(home, "agent.prompt", {"target": pane_id, "text": text}) is not None:
        return True
    return _call(home, "pane.send_text", {"pane_id": pane_id, "text": text}) is not None
