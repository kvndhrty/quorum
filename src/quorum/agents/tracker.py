"""Project tracker: keeps a per-project activity picture.

For each registered project it asks git (read-only `git log`) when available,
falling back to a bounded file-mtime scan. Results land in
state/projects/<slug>/status.json for the dashboards, and staleness
transitions are announced on the `projects` topic (deduped via agent state).
Purely deterministic.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import fsio
from ..agent import Agent

DEFAULT_STALE_DAYS = 10
MAX_SCAN_FILES = 5000
MAX_SCAN_DEPTH = 4
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".cache"}


class Tracker(Agent):
    default_schedule = "every 30m"

    def tick(self) -> None:
        stale_days = int(self.ctx.settings.get("stale_days", DEFAULT_STALE_DAYS))
        now = self.ctx.now()
        state = self.ctx.load_state()
        flags = state.setdefault("stale_flags", {})

        for project in self.ctx.projects.list():
            pdir = project.dir
            if not pdir.is_dir():
                continue
            status = self._scan(project.slug, pdir, now)
            fsio.atomic_write_json(
                self.ctx.home / "state" / "projects" / project.slug / "status.json", status
            )

            last = status.get("last_activity_at")
            is_stale = True
            if last:
                age = now - fsio.parse_iso(last)
                is_stale = age > timedelta(days=stale_days)
            was_stale = flags.get(project.slug, False)
            if is_stale and not was_stale:
                text = f"{project.name} has had no activity for over {stale_days} days"
                self.ctx.bus.post(
                    self.name, "projects", "project.stale", text=text,
                    payload={"project": project.slug, "last_activity_at": last},
                )
                self.ctx.log_action("project.stale", text, project=project.slug)
            elif was_stale and not is_stale:
                text = f"{project.name} is active again"
                self.ctx.bus.post(
                    self.name, "projects", "project.active", text=text,
                    payload={"project": project.slug, "last_activity_at": last},
                )
                self.ctx.log_action("project.active", text, project=project.slug)
            flags[project.slug] = is_stale

        self.ctx.save_state(state)

    def _scan(self, slug: str, pdir: Path, now: datetime) -> dict:
        if (pdir / ".git").exists():
            git = self._git_scan(pdir, now)
            if git is not None:
                return git
        return self._mtime_scan(pdir, now)

    def _git_scan(self, pdir: Path, now: datetime) -> dict | None:
        try:
            last = _git(pdir, "log", "-1", "--format=%cI")
            if last is None:
                return None
            since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            subjects = _git(pdir, "log", f"--since={since}", "--format=%s") or ""
            recent = [s for s in subjects.splitlines() if s.strip()]
            last_iso = fsio.iso(fsio.parse_iso(last.strip())) if last.strip() else None
            summary = (
                f"{len(recent)} commit(s) in the last 7 days" if recent else "no recent commits"
            )
            return {
                "source": "git",
                "scanned_at": fsio.iso(now),
                "last_activity_at": last_iso,
                "commits_last_7d": len(recent),
                "recent_subjects": recent[:5],
                "summary": summary,
            }
        except (OSError, subprocess.SubprocessError):
            return None

    def _mtime_scan(self, pdir: Path, now: datetime) -> dict:
        newest: float | None = None
        changed_24h = 0
        seen = 0
        cutoff_24h = (now - timedelta(hours=24)).timestamp()
        stack: list[tuple[Path, int]] = [(pdir, 0)]
        while stack and seen < MAX_SCAN_FILES:
            current, depth = stack.pop()
            try:
                for entry in current.iterdir():
                    name = entry.name
                    if name.startswith(".") and name != ".quorum.toml":
                        continue
                    if entry.is_dir():
                        if depth < MAX_SCAN_DEPTH and name not in SKIP_DIRS:
                            stack.append((entry, depth + 1))
                        continue
                    seen += 1
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    newest = mtime if newest is None else max(newest, mtime)
                    if mtime >= cutoff_24h:
                        changed_24h += 1
                    if seen >= MAX_SCAN_FILES:
                        break
            except OSError:
                continue
        last_iso = (
            fsio.iso(datetime.fromtimestamp(newest, tz=timezone.utc)) if newest else None
        )
        return {
            "source": "mtime",
            "scanned_at": fsio.iso(now),
            "last_activity_at": last_iso,
            "files_changed_24h": changed_24h,
            "summary": f"{changed_24h} file(s) touched in the last 24h",
        }


def _git(pdir: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(pdir), *args], capture_output=True, text=True, timeout=30
    )
    return proc.stdout if proc.returncode == 0 else None
