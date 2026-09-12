"""`quorum prompt`: how a home's copy of a template differs from the packaged
default.

Which templates exist, which are edited, and what overlays them is `quorum
doctor`'s prompt lines — the one classifier (`home.classify_prompt`) reports
there, and this is the diff behind an "edited" line."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import prompts as prompts_mod
from ._common import (
    _fail,
    get_home,
    prompt_app,
)

# -- prompts ---------------------------------------------------------------


def _read_prompt_file(target: Path) -> str | None:
    """A prompt file's text, or None when it cannot be read or decoded."""
    try:
        return target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


@prompt_app.command("diff")
def prompt_diff(
    name: str = typer.Argument(help="Template name, e.g. manager (no .md)."),
) -> None:
    """Diff this home's copy of a prompt against the packaged default.

    What `quorum init` will not do for an edited prompt: show you what the
    upgrade would have brought. Move your own lines into prompts/<name>.local.md
    and delete prompts/<name>.md to start receiving them again.
    """
    import difflib

    target = get_home()
    name = name[:-3] if name.endswith(".md") else name
    default = prompts_mod.packaged(name)
    if default is None:
        raise _fail(
            f"quorum packages no default prompt named {name!r} — `quorum doctor` lists them"
        )
    home_copy = prompts_mod.path(target, name)
    if not home_copy.is_file():
        typer.echo(f"no prompts/{name}.md — this home uses the packaged default unchanged")
        return
    text = _read_prompt_file(home_copy)
    if text is None:
        raise _fail(
            f"prompts/{name}.md cannot be read (not UTF-8, or no permission) — "
            f"nothing to diff; fix or delete it to fall back to the packaged default"
        )
    if text == default:
        typer.echo(f"prompts/{name}.md is identical to the packaged default")
        return
    diff = difflib.unified_diff(
        default.splitlines(keepends=True),
        text.splitlines(keepends=True),
        fromfile=f"packaged default ({name}.md)",
        tofile=f"prompts/{name}.md",
    )
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("+"):
            typer.secho(line, fg="green")
        elif line.startswith("-"):
            typer.secho(line, fg="red")
        elif line.startswith("@@"):
            typer.secho(line, fg="cyan")
        else:
            typer.echo(line)
    overlay = prompts_mod.local_path(target, name)
    typer.echo("")
    typer.echo(
        f"prompts/{name}.md is yours, so `quorum init` never upgrades it. To take the "
        f"packaged default again, keep your own lines in prompts/{name}.local.md "
        + ("(which already exists) " if overlay.is_file() else "")
        + f"and delete prompts/{name}.md."
    )
