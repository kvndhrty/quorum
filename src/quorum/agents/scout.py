"""Scout: discovers unregistered projects in configured workspace roots.

Scans [projects].workspace_roots (bounded depth) for directories that look
like projects — a .git directory or a .quorum.toml marker. Candidates are
posted to the `projects` topic as proposals and recorded in
state/scout/candidates.json; nothing is ever registered implicitly. The user
confirms with `quorum project adopt <slug>` (or the web dashboard), or
suppresses with `quorum project decline <slug>`.
"""

from __future__ import annotations

from pathlib import Path

from .. import fsio
from ..agent import Agent
from ..projects import MARKER_NAME, read_marker

MAX_DEPTH = 3
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".cache", ".quorum"}

CANDIDATES_FILE = ("state", "scout", "candidates.json")
DECLINED_FILE = ("state", "scout", "declined.json")


def candidates_path(home: Path) -> Path:
    return Path(home).joinpath(*CANDIDATES_FILE)


def declined_path(home: Path) -> Path:
    return Path(home).joinpath(*DECLINED_FILE)


def load_candidates(home: Path) -> dict[str, str]:
    try:
        return fsio.read_json(candidates_path(home))
    except (OSError, ValueError):
        return {}


def load_declined(home: Path) -> list[str]:
    try:
        return fsio.read_json(declined_path(home))
    except (OSError, ValueError):
        return []


def decline(home: Path, slug: str) -> None:
    declined = set(load_declined(home))
    declined.add(slug)
    fsio.atomic_write_json(declined_path(home), sorted(declined))
    candidates = load_candidates(home)
    if candidates.pop(slug, None) is not None:
        fsio.atomic_write_json(candidates_path(home), candidates)


class Scout(Agent):
    default_schedule = "cron 15 9 * * *"

    def tick(self) -> None:
        roots = []
        if self.ctx.config is not None:
            roots = [Path(r).expanduser() for r in self.ctx.config.projects.workspace_roots]
        max_depth = int(self.ctx.settings.get("max_depth", MAX_DEPTH))

        registered = {Path(p.path).resolve() for p in self.ctx.projects.list()}
        declined = set(load_declined(self.ctx.home))
        candidates = load_candidates(self.ctx.home)
        state = self.ctx.load_state()
        announced = set(state.setdefault("announced", []))

        for root in roots:
            if not root.is_dir():
                continue
            for pdir in self._find_projects(root, max_depth):
                resolved = pdir.resolve()
                if resolved in registered:
                    continue
                marker = read_marker(pdir)
                name = marker.get("name", pdir.name)
                slug = fsio.slugify(name)
                if slug in declined or self.ctx.projects.get(slug) is not None:
                    continue
                candidates[slug] = str(resolved)
                if slug not in announced:
                    source = "marker" if (pdir / MARKER_NAME).is_file() else "git"
                    text = (
                        f"found unregistered project '{name}' at {resolved} "
                        f"— `quorum project adopt {slug}` to track it"
                    )
                    self.ctx.bus.post(
                        self.name, "projects", "project.candidate", text=text,
                        payload={"slug": slug, "path": str(resolved), "source": source},
                    )
                    self.ctx.log_action("project.candidate", text, slug=slug)
                    announced.add(slug)

        # drop candidates that were adopted or declined since the last scan
        candidates = {
            s: p
            for s, p in candidates.items()
            if s not in declined and Path(p).resolve() not in registered
        }
        fsio.atomic_write_json(candidates_path(self.ctx.home), candidates)
        state["announced"] = sorted(announced)
        self.ctx.save_state(state)

    def _find_projects(self, root: Path, max_depth: int) -> list[Path]:
        found: list[Path] = []
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                children = [c for c in current.iterdir() if c.is_dir()]
            except OSError:
                continue
            for child in children:
                if child.name in SKIP_DIRS or child.name.startswith("."):
                    continue
                if (child / ".git").exists() or (child / MARKER_NAME).is_file():
                    found.append(child)  # a project root; don't descend further
                elif depth + 1 < max_depth:
                    stack.append((child, depth + 1))
        return sorted(found)
