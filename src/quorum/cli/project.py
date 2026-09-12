"""`quorum project`: the registry of directories tasks run against.

The registry is `projects/<slug>.json` — one file per project, which is what
`quorum status` lists and what unregistering a project is `rm`-ing.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ._common import (
    _actor_guard,
    _fail,
    _verbatim_text,
    get_home,
    project_app,
)

# -- projects --------------------------------------------------------------


@project_app.command("add")
def project_add(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Display name (default: dir name)."),
    deadline: str | None = typer.Option(None, "--deadline", help="ISO date, e.g. 2026-09-15."),
    notes: str = typer.Option("", "--notes", help="Free-form notes shown in views."),
    marker: bool = typer.Option(False, "--marker", help="Also write a .quorum.toml into the project dir."),
    force: bool = typer.Option(False, "--force", help="Register even if the directory is not a git repository."),
) -> None:
    """Register a project directory (tasks run against registered projects).

    Example: quorum project add ~/work/my-api --deadline 2026-09-15
    """
    from ..projects import ProjectRegistry

    target = get_home()
    resolved = path.expanduser().resolve()
    if resolved.is_dir() and not (resolved / ".git").exists() and not force:
        raise _fail(
            f"{resolved} is not a git repository — tasks need one for worktrees; "
            "`git init` it, or pass --force to register anyway (tasks there will need --no-worktree)"
        )
    _actor_guard(target, "project.add", args=str(path))
    registry = ProjectRegistry(target)
    try:
        project = registry.add(
            path,
            name=name,
            deadline=deadline,
            notes=notes,
            write_marker=marker,
        )
    except ValueError as e:
        raise _fail(str(e)) from None
    typer.secho(f"registered project {project.slug} ({project.path})", fg="green")


@project_app.command("set")
def project_set(
    slug: str,
    deadline: str | None = typer.Option(None, "--deadline", help="ISO date; an empty string clears it."),
    notes: str | None = typer.Option(None, "--notes"),
    notes_file: Path | None = typer.Option(
        None,
        "--notes-file",
        help="Read the notes from a file ('-' for stdin); they fill the preamble's {project} block.",
    ),
    name: str | None = typer.Option(None, "--name"),
) -> None:
    """Update a project's metadata in the registry.

    Notes are not just a label: they fill the `{project}` block of the task
    preamble, so every task on this project starts with them. That is why
    they can come from a file — `--notes-file conventions.md`.
    """
    from ..projects import ProjectRegistry

    if notes_file is not None and notes is not None:
        raise _fail("pass --notes or --notes-file, not both")
    target = get_home()
    registry = ProjectRegistry(target)
    if notes_file is not None:
        # Read last, after everything that can be checked without it: piped
        # notes are gone the moment stdin is drained, so a typo in the slug
        # must not eat them (`task add` reads its prompt the same way round).
        if registry.get(slug) is None:
            raise _fail(f"no project {slug!r}")
        notes = _verbatim_text(notes_file, "notes")
    _actor_guard(target, "project.set", target=slug)
    try:
        project = registry.update(
            slug,
            deadline=deadline,
            notes=notes,
            name=name,
        )
    except KeyError:
        raise _fail(f"no project {slug!r}") from None
    typer.secho(f"updated {project.slug}" + (f" (due {project.deadline})" if project.deadline else ""), fg="green")

