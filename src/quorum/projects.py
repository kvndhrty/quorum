"""Project registry.

The canonical record for each project is `projects/<slug>.json` in
QUORUM_HOME. If the project directory itself contains a `.quorum.toml`
marker, its fields (name, deadline, tags, notes) merge over the registry
record at read time — so metadata can travel with a synced/shared repo while
quorum still only ever *reads* project directories. This function is the
single merge point; every agent and view goes through it.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from . import fsio

MARKER_NAME = ".quorum.toml"
MARKER_FIELDS = ("name", "deadline", "tags", "notes")


class Project(BaseModel):
    v: int = 1
    slug: str
    name: str
    path: str
    tags: list[str] = Field(default_factory=list)
    deadline: str | None = None  # ISO date, e.g. "2026-08-27"
    notes: str = ""
    created_at: str = Field(default_factory=lambda: fsio.iso(fsio.utc_now()))

    @field_validator("deadline")
    @classmethod
    def _check_deadline(cls, v: str | None) -> str | None:
        if v is not None:
            date.fromisoformat(v)
        return v

    @property
    def dir(self) -> Path:
        return Path(self.path).expanduser()

    def days_left(self, today: date) -> int | None:
        if self.deadline is None:
            return None
        return (date.fromisoformat(self.deadline) - today).days


class ProjectRegistry:
    def __init__(self, home: Path):
        self.dir = Path(home) / "projects"

    def _path(self, slug: str) -> Path:
        return self.dir / f"{slug}.json"

    def add(
        self,
        path: str | Path,
        name: str | None = None,
        deadline: str | None = None,
        tags: list[str] | None = None,
        notes: str = "",
        write_marker: bool = False,
    ) -> Project:
        pdir = Path(path).expanduser().resolve()
        if not pdir.is_dir():
            raise ValueError(f"{pdir} does not exist (or is not a directory)")
        name = name or pdir.name
        slug = fsio.slugify(name)
        if self._path(slug).exists():
            raise ValueError(f"project {slug!r} already registered")
        project = Project(
            slug=slug,
            name=name,
            path=str(pdir),
            deadline=deadline,
            tags=tags or [],
            notes=notes,
        )
        fsio.atomic_write_json(self._path(slug), project.model_dump())
        if write_marker:
            self.write_marker(project)
        return project

    def get(self, slug: str) -> Project | None:
        path = self._path(slug)
        if not path.exists():
            return None
        try:
            record = Project.model_validate(fsio.read_json(path))
        except (OSError, ValueError):
            return None
        return _merge_marker(record)

    def list(self) -> list[Project]:
        out = []
        for path in fsio.sorted_entries(self.dir):
            try:
                record = Project.model_validate(fsio.read_json(path))
            except (OSError, ValueError):
                continue
            out.append(_merge_marker(record))
        return sorted(out, key=lambda p: (p.deadline is None, p.deadline or "", p.slug))

    def remove(self, slug: str) -> bool:
        path = self._path(slug)
        if path.exists():
            path.unlink()
            return True
        return False

    def update(self, slug: str, **fields) -> Project:
        """Update registry fields (deadline, notes, tags, name). The registry
        file is machine-owned JSON, so rewriting it is fine — unlike config.toml."""
        path = self._path(slug)
        if not path.exists():
            raise KeyError(slug)
        record = Project.model_validate(fsio.read_json(path))
        updates = {k: v for k, v in fields.items() if v is not None}
        if updates.get("deadline") == "":  # None means "unchanged", so "" is the clear signal
            updates["deadline"] = None
        record = record.model_copy(update=updates)
        Project.model_validate(record.model_dump())  # re-check validators
        fsio.atomic_write_json(path, record.model_dump())
        return _merge_marker(record)

    def write_marker(self, project: Project) -> Path:
        """Write a .quorum.toml marker into the project dir (opt-in via --marker)."""
        lines = [f'name = "{project.name}"']
        if project.deadline:
            lines.append(f'deadline = "{project.deadline}"')
        if project.tags:
            tags = ", ".join(f'"{t}"' for t in project.tags)
            lines.append(f"tags = [{tags}]")
        if project.notes:
            lines.append(f'notes = """{project.notes}"""')
        marker = project.dir / MARKER_NAME
        marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return marker


def read_marker(project_dir: Path) -> dict:
    marker = project_dir / MARKER_NAME
    if not marker.is_file():
        return {}
    try:
        with open(marker, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    fields = {k: raw[k] for k in MARKER_FIELDS if k in raw}
    if "deadline" in fields:
        d = fields["deadline"]
        if isinstance(d, (date, datetime)):
            fields["deadline"] = d.strftime("%Y-%m-%d")
        else:
            fields["deadline"] = str(d)
        try:
            date.fromisoformat(fields["deadline"])
        except ValueError:
            del fields["deadline"]
    if "tags" in fields and not isinstance(fields["tags"], list):
        del fields["tags"]
    if "notes" in fields:
        fields["notes"] = str(fields["notes"])
    if "name" in fields:
        fields["name"] = str(fields["name"])
    return fields


def _merge_marker(record: Project) -> Project:
    fields = read_marker(record.dir)
    return record.model_copy(update=fields) if fields else record
