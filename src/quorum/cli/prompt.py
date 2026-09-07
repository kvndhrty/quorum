"""`quorum prompt`: what each template resolves to, and how it differs from
the packaged default."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import fsio
from .. import prompts as prompts_mod

if TYPE_CHECKING:
    pass

from ._common import (
    _fail,
    get_home,
    prompt_app,
)

# -- prompts ---------------------------------------------------------------


def _prompt_names(home: Path) -> list[str]:
    """Every template name that resolves here: packaged defaults plus
    anything the user wrote into prompts/ (overlays are not templates)."""
    from importlib import resources

    names = set()
    try:
        defaults = resources.files("quorum") / "default_prompts"
        names |= {e.name[:-3] for e in defaults.iterdir() if e.name.endswith(".md")}
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    for entry in fsio.sorted_entries(home / "prompts", suffix=".md"):
        if entry.name.endswith(prompts_mod.LOCAL_SUFFIX):
            continue
        names.add(entry.name[:-3])
    return sorted(names)


def _read_prompt_file(target: Path) -> str | None:
    """A prompt file's text, or None when it cannot be read or decoded.

    One unreadable file must not take the whole listing down with it — see
    `prompt_list`, which marks it `?` and carries on."""
    try:
        return target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


@prompt_app.command("list")
def prompt_list() -> None:
    """Show each prompt template: home copy vs packaged default, and overlay."""
    target = get_home()
    names = _prompt_names(target)
    for name in names:
        default = prompts_mod.packaged(name)
        home_copy = prompts_mod.path(target, name)
        text = default
        if not home_copy.is_file():
            state = "packaged default (no home copy)"
        else:
            text = _read_prompt_file(home_copy)
            if text is None:
                state = "? unreadable (not UTF-8, or no permission) — every render of it fails"
            elif default is None:
                state = "yours (quorum packages no default)"
            elif text == default:
                state = "seeded, matches the packaged default"
            else:
                state = f"edited — `quorum prompt diff {name}` vs the packaged default"
        overlay = prompts_mod.local_path(target, name)
        if overlay.is_file():
            if _read_prompt_file(overlay) is None:
                # render() ignores an overlay it cannot decode; say so here,
                # because silently dead policy is the failure that hurts.
                note = "? unreadable — ignored when rendering"
            elif text is None:
                note = "merged where the template says, once it is readable"
            else:
                note = "{local} slot" if prompts_mod.has_slot(text) else "prepended"
            state += f" + {overlay.name} ({note})"
        typer.echo(f"  {name:<16} {state}")
    # an overlay for a template that does not exist is silently dead policy
    for entry in fsio.sorted_entries(target / "prompts", suffix=prompts_mod.LOCAL_SUFFIX):
        stem = entry.name[: -len(prompts_mod.LOCAL_SUFFIX)]
        if stem not in names:
            typer.secho(
                f"  {entry.name}: no prompt named {stem!r} — this overlay is never rendered",
                fg="yellow",
            )
    _print_project_blocks(target)


PREAMBLE = "task-preamble"


def _print_project_blocks(target: Path) -> None:
    """The fourth prompt layer: what each project puts in the preamble's
    `{project}` slot (its registry notes, its own .quorum file, or both).

    Only projects that actually contribute a block are listed — the point is
    to make per-project prompt text findable, not to re-list the registry.
    """
    from ..projects import ProjectRegistry

    rows: list[tuple[str, str]] = []
    for project in ProjectRegistry(target).list():
        sources = []
        if project.notes.strip():
            sources.append("notes (registry)")
        block = prompts_mod.project_local_path(project.dir, PREAMBLE)
        if block.is_file():
            shown = f"{prompts_mod.PROJECT_DIR_NAME}/{block.name}"
            # render() ignores a block it cannot decode, exactly as it does
            # an overlay; silently dead policy is the failure that hurts.
            unreadable = _read_prompt_file(block) is None
            sources.append(f"? {shown} unreadable — ignored when rendering" if unreadable else shown)
        if sources:
            rows.append((project.slug, " + ".join(sources)))
    if not rows:
        return
    typer.echo(f"  per-project {{project}} block in {PREAMBLE}:")
    for slug, sources in rows:
        typer.echo(f"    {slug:<14} {sources}")
    try:
        template = prompts_mod.load(target, PREAMBLE)
    except (KeyError, OSError, UnicodeDecodeError):
        return  # already reported above as missing or unreadable
    if not prompts_mod.has_slot(template, "project"):
        typer.secho(
            f"    prompts/{PREAMBLE}.md has no {{project}} slot — these blocks are never rendered",
            fg="yellow",
        )


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
        raise _fail(f"quorum packages no default prompt named {name!r} — `quorum prompt list`")
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
