"""User-editable prompt templates.

Templates live in QUORUM_HOME/prompts/<name>.md (seeded by `quorum init`
from the packaged defaults in quorum/default_prompts/). Editing the file
retunes the agent; deleting it restores the packaged default on next use.
Re-running `quorum init` after upgrading quorum refreshes any seed the user
never edited (recognized by hash — `home.SUPERSEDED_PROMPT_HASHES`) and
leaves edited files alone, reporting when their default has moved on.
Placeholders use str.format-style {names}; unknown braces are left intact.

Alongside each template sits an optional *overlay*: prompts/<name>.local.md,
user-owned, never seeded and never touched by `quorum init`. It is merged
into the resolved template at its `{local}` slot (the packaged manager and
task preamble carry one where home policy belongs), or prepended when the
template has no slot. An absent or empty overlay renders to nothing. The
overlay exists so that adding a few lines of home policy does not fork the
whole template — a forked `<name>.md` stops receiving packaged upgrades,
which is how a home silently falls behind. Rewriting `<name>.md` outright
still wins, exactly as before.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

LOCAL_SUFFIX = ".local.md"

# An unescaped {local} — the header comments of the packaged prompts document
# the key as `{{local}}`, which must not count as a slot.
_LOCAL_SLOT = re.compile(r"(?<!\{)\{local\}(?!\})")
# A slot sitting on its own line, plus the blank line after it: an empty
# overlay should leave the paragraphs around it touching, not a hole. An
# inline slot just substitutes "" like any other empty placeholder.
_EMPTY_LOCAL_SLOT = re.compile(r"(?m)^[ \t]*(?<!\{)\{local\}(?!\})[ \t]*(?:\n\n?|\Z)")


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


def has_slot(template: str) -> bool:
    """True when `template` carries an unescaped {local} slot (the `{{local}}`
    the packaged headers use to *document* the key does not count)."""
    return bool(_LOCAL_SLOT.search(template))


def load_local(home: Path, name: str) -> str:
    """The overlay text for `name` — "" when there is no overlay file."""
    overlay = local_path(home, name)
    if not overlay.is_file():
        return ""
    return overlay.read_text(encoding="utf-8").strip()


def render(home: Path, name: str, **placeholders: str) -> str:
    """Render a template with its overlay merged in.

    `local` is an ordinary placeholder key: pass it explicitly to override
    the overlay file (nothing in quorum does), otherwise it comes from
    prompts/<name>.local.md. A template without a `{local}` slot gets a
    non-empty overlay prepended instead, so policy still reaches a harness
    whose template predates the slot.
    """
    template = load(home, name)
    overlay = placeholders.pop("local", None)
    if overlay is None:
        overlay = load_local(home, name)
    overlay = overlay.strip()
    slot = has_slot(template)
    if not overlay:
        template = _EMPTY_LOCAL_SLOT.sub("", template)
    rendered = template.format_map(_SafeDict({**placeholders, "local": overlay}))
    if overlay and not slot:
        rendered = f"{overlay}\n\n{rendered.lstrip()}"
    return rendered
