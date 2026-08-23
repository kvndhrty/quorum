"""Shared read-model for `quorum status`, the web dashboard, and the TUI.

Everything here is assembled purely from files under QUORUM_HOME, so every
view works whether or not the supervisor is running.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import fsio
from .config import Config, ConfigError, load_config
from .messages import MessageBus
from .projects import ProjectRegistry
from .supervisor import LOCK_TOUCH_SECONDS

SUPERVISOR_STALE_AFTER = LOCK_TOUCH_SECONDS * 3


def supervisor_status(home: Path) -> dict[str, Any]:
    lock = home / "supervisor.lock"
    if not lock.exists():
        return {"alive": False}
    try:
        meta = fsio.read_json(lock)
        age = time.time() - lock.stat().st_mtime
    except (OSError, ValueError):
        return {"alive": False}
    pid = meta.get("pid")
    # The mtime heartbeat is only touched once a minute, so on its own it
    # reports a supervisor that crashed seconds ago (leaving its lock behind)
    # as running. Ask the OS whether the recorded pid is still there too.
    alive = age < SUPERVISOR_STALE_AFTER and isinstance(pid, int) and fsio.pid_alive(pid)
    return {
        "alive": alive,
        "pid": pid,
        "started_at": meta.get("started_at"),
        "lock_age_seconds": int(age),
    }


def agent_rows(home: Path, config: Config | None = None) -> list[dict[str, Any]]:
    if config is None:
        try:
            config = load_config(home)
        except ConfigError:
            config = Config()
    rows = []
    for name, acfg in sorted(config.agents.items()):
        hb_path = home / "state" / "agents" / name / "heartbeat.json"
        hb: dict[str, Any] = {}
        try:
            hb = fsio.read_json(hb_path)
        except (OSError, ValueError):
            pass
        rows.append(
            {
                "name": name,
                "type": acfg.type,
                "schedule": acfg.schedule,
                "enabled": acfg.enabled,
                "status": hb.get("status", "never-ran"),
                "last_start": hb.get("last_start"),
                "last_end": hb.get("last_end"),
                "duration_ms": hb.get("duration_ms"),
                "next_run": hb.get("next_run"),
                "error": hb.get("error"),
            }
        )
    return rows


def project_rows(home: Path) -> list[dict[str, Any]]:
    registry = ProjectRegistry(home)
    today = fsio.utc_now().date()
    rows = []
    for p in registry.list():
        status: dict[str, Any] = {}
        try:
            status = fsio.read_json(home / "state" / "projects" / p.slug / "status.json")
        except (OSError, ValueError):
            pass
        rows.append(
            {
                "slug": p.slug,
                "name": p.name,
                "path": p.path,
                "tags": p.tags,
                "deadline": p.deadline,
                "days_left": p.days_left(today),
                "notes": p.notes,
                "last_activity": status.get("last_activity_at"),
                "activity_summary": status.get("summary"),
            }
        )
    return rows


def board_tail(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    bus = MessageBus(home)
    msgs = []
    for topic in bus.topics():
        for m in bus.read_topic(topic, limit=limit):
            msgs.append(
                {
                    "at": m.created_at,
                    "topic": topic,
                    "from": m.sender,
                    "type": m.type,
                    "text": m.payload.get("text", ""),
                }
            )
    msgs.sort(key=lambda m: m["at"])
    return msgs[-limit:]


def recent_actions(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    return fsio.read_jsonl(home / "logs" / "actions.jsonl")[-limit:]


def overview(home: Path) -> dict[str, Any]:
    try:
        config = load_config(home)
    except ConfigError:
        config = None
    return {
        "home": str(home),
        "supervisor": supervisor_status(home),
        "agents": agent_rows(home, config),
        "projects": project_rows(home),
        "board": board_tail(home),
        "actions": recent_actions(home),
    }
