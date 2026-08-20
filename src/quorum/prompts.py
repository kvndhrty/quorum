"""User-editable prompt templates.

Templates live in QUORUM_HOME/prompts/<name>.md (seeded by `quorum init`
from the packaged defaults in quorum/default_prompts/). Editing the file
retunes the agent; deleting it restores the packaged default on next use.
Placeholders use str.format-style {names}; unknown braces are left intact.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load(home: Path, name: str) -> str:
    user_file = Path(home) / "prompts" / f"{name}.md"
    if user_file.is_file():
        return user_file.read_text(encoding="utf-8")
    packaged = resources.files("quorum") / "default_prompts" / f"{name}.md"
    try:
        return packaged.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyError(f"no prompt template {name!r} (looked in {user_file} and packaged defaults)")


def render(home: Path, name: str, **placeholders: str) -> str:
    return load(home, name).format_map(_SafeDict(placeholders))
