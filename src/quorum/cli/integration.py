"""`quorum integration`: install a harness adapter so a live session can be
adopted."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from ._common import (
    _fail,
    integration_app,
)

# -- integrations ----------------------------------------------------------


def _integrations_root() -> Path:
    """The bundled harness adapters: inside the package in a wheel install,
    at the repo root in a checkout."""
    package = Path(__file__).resolve().parents[1]  # quorum/
    packaged = package / "integrations"
    if packaged.is_dir():
        return packaged
    checkout = package.parents[1] / "integrations"  # the repo root, from src/quorum
    if checkout.is_dir():
        return checkout
    raise _fail("no bundled integrations found — reinstall quorum-orchestrator")


def _adapter_files(root: Path, name: str) -> list[tuple[Path, Path]]:
    """(source, destination) pairs for a copy-installed adapter."""
    if name == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return [
            (root / "codex" / "hooks.json", codex_home / "hooks.json"),
            (
                root / "codex" / "prompts" / "quorum-adopt.md",
                codex_home / "prompts" / "quorum-adopt.md",
            ),
        ]
    if name == "opencode":
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
        return [
            (root / "opencode" / "plugin" / "quorum.js", cfg / "plugins" / "quorum.js"),
            (
                root / "opencode" / "commands" / "quorum-adopt.md",
                cfg / "commands" / "quorum-adopt.md",
            ),
        ]
    return []


_ADAPTER_NOTES = {
    "claude-code": "adopt with /quorum:adopt inside a session",
    "codex": "Codex asks you to trust the new hooks once; adopt with /prompts:quorum-adopt",
    "opencode": "adopt with /quorum-adopt inside a session",
}


def _print_adapters(root: Path) -> None:
    """The bundled adapters and whether each is installed (`install --list`)."""
    for name in ("claude-code", "codex", "opencode"):
        if name == "claude-code":
            state = "plugin-managed"
            detail = f"`claude plugin install {root / name}`"
        else:
            files = _adapter_files(root, name)
            installed = sum(1 for _, dest in files if dest.exists())
            state = (
                "installed" if installed == len(files)
                else "partial" if installed
                else "not installed"
            )
            detail = ", ".join(str(dest) for _, dest in files)
        typer.echo(f"{name:<12} {state:<14} {detail}")
    typer.echo("\ninstall one: `quorum integration install <name>`")


@integration_app.command("install")
def integration_install(
    name: str = typer.Argument("", help="Adapter: claude-code, codex, or opencode."),
    list_only: bool = typer.Option(
        False, "--list", help="Show the bundled adapters and whether they are installed."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing destination files."),
) -> None:
    """Install a harness adapter so live sessions can be adopted (`quorum task adopt`).

    Copies the adapter's hook config or plugin to the harness's user-wide
    config location; per-project installs are described in the adapter's
    README (integrations/<name>/README.md in the repo).

    `quorum integration install --list` names the bundled adapters and says
    which of them this machine already has.
    """
    root = _integrations_root()
    if list_only:
        if name:
            raise _fail("--list shows every adapter; drop the name")
        _print_adapters(root)
        return
    if not name:
        raise _fail("name an adapter to install, or --list to see them")
    if name == "claude-code":
        typer.echo("Claude Code adapters install through its plugin manager — run:")
        typer.echo(f"  claude plugin install {root / 'claude-code'}")
        typer.echo("then adopt a session with /quorum:adopt (manual, plugin-less install: "
                   "see the README in that directory)")
        return
    files = _adapter_files(root, name)
    if not files:
        raise _fail(f"no adapter {name!r} (available: claude-code, codex, opencode)")
    for src, dest in files:
        if dest.exists() and not force:
            if dest.read_bytes() == src.read_bytes():
                typer.echo(f"{dest} already installed (identical)")
                continue
            raise _fail(
                f"{dest} already exists with different content — merge the entries from "
                f"{src} by hand (see {root / name / 'README.md'}), or re-run with --force to overwrite"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        typer.secho(f"installed {dest}", fg="green")
    note = _ADAPTER_NOTES.get(name)
    if note:
        typer.echo(note)
