"""User-editable prompt templates.

Templates live in QUORUM_HOME/prompts/<name>.md (seeded by `quorum init`
from the packaged defaults in quorum/default_prompts/). Editing the file
retunes the agent; deleting it restores the packaged default on next use.
Re-running `quorum init` after upgrading quorum refreshes any seed the user
never edited (recognized by the seed record `prompts/.seeded.json`) and
leaves edited files alone, reporting when their default has moved on.
Placeholders use str.format-style {names}; unknown braces are left intact.

Alongside each template sits an optional *overlay*: prompts/<name>.local.md,
user-owned, never seeded and never touched by `quorum init`. It is merged
into the resolved template at its `{local}` slot (the packaged manager,
task preamble and perpetual block each carry one where home policy
belongs), or prepended when the template has no slot. An absent, empty or
unreadable overlay renders to nothing. The
overlay exists so that adding a few lines of home policy does not fork the
whole template — a forked `<name>.md` stops receiving packaged upgrades,
which is how a home silently falls behind. Rewriting `<name>.md` outright
still wins, exactly as before.

The overlay is home-wide, which is the wrong scope for a home holding
several projects. `{project}` is the fourth layer (after the packaged
default, the home copy and the home overlay): a second block, filled per
project from the registry `notes` and from `.quorum/<name>.local.md`
*inside the project directory* (`project_block`) — read-only, like every
other project-dir read. It follows the `{local}` rules exactly: same
fail-soft read, and an empty block takes its slot's line with it. It has no
prepend fallback — a template that never mentions `{project}` simply has no
place for it, and `quorum prompt list` says so rather than guessing.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

LOCAL_SUFFIX = ".local.md"
# The directory a project keeps its own prompt blocks in, e.g.
# <project>/.quorum/task-preamble.local.md. Sibling of the .quorum.toml
# marker projects.py reads, and just as read-only.
PROJECT_DIR_NAME = ".quorum"


def _slot_re(key: str) -> re.Pattern[str]:
    """An unescaped {key} — the header comments of the packaged prompts
    document their keys as `{{key}}`, which must not count as a slot."""
    return re.compile(r"(?<!\{)\{" + key + r"\}(?!\})")


def _empty_slot_re(key: str) -> re.Pattern[str]:
    """A slot sitting on its own line, plus the blank line after it: an empty
    block should leave the paragraphs around it touching, not a hole. An
    inline slot just substitutes "" like any other empty placeholder."""
    return re.compile(r"(?m)^[ \t]*(?<!\{)\{" + key + r"\}(?!\})[ \t]*(?:\n\n?|\Z)")


_LOCAL_SLOT = _slot_re("local")
_EMPTY_LOCAL_SLOT = _empty_slot_re("local")
_PROJECT_SLOT = _slot_re("project")
_EMPTY_PROJECT_SLOT = _empty_slot_re("project")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def path(home: Path, name: str) -> Path:
    """The home copy of a template (may not exist — see `load`)."""
    return Path(home) / "prompts" / f"{name}.md"


def local_path(home: Path, name: str) -> Path:
    """The overlay file for a template (may not exist — see `load_local`)."""
    return Path(home) / "prompts" / f"{name}{LOCAL_SUFFIX}"


def packaged(name: str) -> str | None:
    """The default quorum ships for `name`, or None if it packages none."""
    entry = resources.files("quorum") / "default_prompts" / f"{name}.md"
    try:
        return entry.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def load(home: Path, name: str) -> str:
    user_file = path(home, name)
    if user_file.is_file():
        return user_file.read_text(encoding="utf-8")
    text = packaged(name)
    if text is None:
        raise KeyError(
            f"no prompt template {name!r} (looked in {user_file} and packaged defaults)"
        )
    return text


def has_slot(template: str, key: str = "local") -> bool:
    """True when `template` carries an unescaped {local} (or {project}) slot
    — the `{{local}}` the packaged headers use to *document* the key does
    not count."""
    pattern = _PROJECT_SLOT if key == "project" else _LOCAL_SLOT
    return bool(pattern.search(template))


def project_local_path(project_dir: Path, name: str) -> Path:
    """The project's own block for `name` (may not exist — see
    `load_project_local`). Quorum only ever reads it."""
    return Path(project_dir) / PROJECT_DIR_NAME / f"{name}{LOCAL_SUFFIX}"


def load_local(home: Path, name: str) -> str:
    """The overlay text for `name` — "" when there is no usable overlay file.

    Fail-soft, unlike `load`: an overlay that cannot be read or decoded
    renders as no overlay at all. `render` sits on the manager tick and on
    every task run, so one stray non-UTF-8 byte in a user-owned file must
    not fail every tick forever. `quorum prompt list` is where an unreadable
    overlay is reported.
    """
    overlay = local_path(home, name)
    try:
        if not overlay.is_file():
            return ""
        return overlay.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, OSError):
        return ""


def load_project_local(project_dir: Path, name: str) -> str:
    """`load_local`, aimed at a project directory instead of the home.

    Same fail-soft contract, and for the same reason: this read is on every
    task run, and the file belongs to whoever owns the repo — an unreadable
    one renders as no block rather than failing the run. `quorum prompt
    list` reports it.
    """
    block = project_local_path(project_dir, name)
    try:
        if not block.is_file():
            return ""
        return block.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, OSError):
        return ""


def project_block(project_dir: Path | None, notes: str = "", name: str = "task-preamble") -> str:
    """The `{project}` block: the project's registry `notes` (already merged
    with its `.quorum.toml` marker), then its own `.quorum/<name>.local.md`.

    Both sources, in that order, so a project can keep short metadata in the
    registry and longer conventions in the repo. "" when neither has
    anything, which is what makes the slot's line disappear.
    """
    parts = [(notes or "").strip()]
    if project_dir is not None:
        parts.append(load_project_local(project_dir, name))
    return "\n\n".join(p for p in parts if p)


def render(home: Path, name: str, **placeholders: str) -> str:
    """Render a template with its overlay merged in.

    `local` is an ordinary placeholder key: pass it explicitly to override
    the overlay file (nothing in quorum does), otherwise it comes from
    prompts/<name>.local.md. A template without a `{local}` slot gets a
    non-empty overlay prepended instead, so policy still reaches a harness
    whose template predates the slot.

    `project` is the per-project block (`project_block`); callers that have
    no project pass nothing. It gets the same empty-slot treatment as
    `local` but no prepend fallback — see the module docstring.
    """
    template = load(home, name)
    overlay = placeholders.pop("local", None)
    if overlay is None:
        overlay = load_local(home, name)
    overlay = overlay.strip()
    project = (placeholders.pop("project", None) or "").strip()
    slot = has_slot(template)
    if not overlay:
        template = _EMPTY_LOCAL_SLOT.sub("", template)
    if not project:
        template = _EMPTY_PROJECT_SLOT.sub("", template)
    rendered = template.format_map(
        _SafeDict({**placeholders, "local": overlay, "project": project})
    )
    if overlay and not slot:
        rendered = f"{overlay}\n\n{rendered.lstrip()}"
    return rendered
