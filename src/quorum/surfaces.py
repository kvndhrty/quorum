"""What quorum exposes, counted from the code.

Issue #102 asks that the size of the exposed surface stay visible: every
command, option and config key costs a reader something whether or not they
use it. This module is the one place that counts them, so `quorum doctor`'s
`surfaces` line and `scripts/surfaces.py` can never report different numbers.

The three classes here are the ones that can be counted from an installed
package alone: typer's command tree, the option and argument declarations on
it, and the fields of the pydantic config model. The inventory script adds the
classes that need the repo (the home layout, TUI bindings, prompt
placeholders, doctor checks, digest markers, the guide's headings).

Reading only: nothing here writes, and nothing here decides anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


def _first_line(text: str | None) -> str:
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _param_kind(param: Any) -> str:
    kind = getattr(param, "param_type_name", "")
    if kind in ("option", "argument"):
        return kind
    return "option" if type(param).__name__.endswith("Option") else "argument"


def cli_tree() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Walk the real typer/click tree: (commands, params).

    `commands` has one row per group and per command; `params` one row per
    option or positional argument declaration, so an option declared on
    sixty-one commands counts sixty-one times — that is the surface a reader
    meets in `--help`.
    """
    import typer.main

    from . import cli as qcli

    root = typer.main.get_command(qcli.app)
    commands: list[dict[str, str]] = []
    params: list[dict[str, str]] = []

    def is_group(cmd: Any) -> bool:
        return isinstance(getattr(cmd, "commands", None), dict)

    def walk(cmd: Any, path: list[str]) -> None:
        if is_group(cmd):
            if path:
                commands.append(
                    {
                        "path": " ".join(path),
                        "kind": "group",
                        "params": "",
                        "opts": "",
                        "args": "",
                        "panel": "",
                        "help": _first_line(cmd.help),
                    }
                )
            for name in sorted(cmd.commands):
                walk(cmd.commands[name], path + [name])
            return
        opts = [p for p in cmd.params if _param_kind(p) == "option"]
        args = [p for p in cmd.params if _param_kind(p) == "argument"]
        commands.append(
            {
                "path": " ".join(path),
                "kind": "command",
                "params": str(len(cmd.params)),
                "opts": str(len(opts)),
                "args": str(len(args)),
                "panel": getattr(cmd, "rich_help_panel", "") or "",
                "help": _first_line(cmd.help),
            }
        )
        for p in cmd.params:
            decl = "/".join(p.opts + p.secondary_opts) if p.opts else p.name
            try:
                type_name = p.type.name
            except Exception:  # pragma: no cover - click types all have .name
                type_name = str(p.type)
            default = p.default
            default = "" if default is None else {True: "true", False: "false"}.get(
                default, default
            )
            params.append(
                {
                    "command": " ".join(path) or "(root)",
                    "kind": _param_kind(p),
                    "decl": decl,
                    "type": str(type_name),
                    "required": "yes" if p.required else "",
                    "default": str(default),
                    "help": _first_line(getattr(p, "help", "") or ""),
                }
            )

    walk(root, [])
    return commands, params


def config_keys() -> list[dict[str, str]]:
    """Every field of every config model, nested, plus the `settings` keys the
    code reads out of the free-form per-agent dict the model cannot enumerate.

    A row whose key contains a dot is a leaf setting a user can write; the
    dotless rows are the table names those settings live under.
    """
    from pydantic import BaseModel

    from . import config as qconfig

    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def annot(a: Any) -> str:
        text = re.sub(r"\b(typing|quorum\.config)\.", "", str(a))
        return text.replace("<class '", "").replace("'>", "")

    def nested(a: Any) -> list[type[BaseModel]]:
        out = []
        if isinstance(a, type) and issubclass(a, BaseModel):
            out.append(a)
        for x in getattr(a, "__args__", ()):
            if isinstance(x, type) and issubclass(x, BaseModel):
                out.append(x)
        return out

    def is_dict_of_model(a: Any) -> bool:
        args = getattr(a, "__args__", ())
        return "dict" in str(a).lower() and any(
            isinstance(x, type) and issubclass(x, BaseModel) for x in args
        )

    def walk(model: type[BaseModel], prefix: str, depth: int = 0) -> None:
        if depth > 4:
            return
        for name, field in model.model_fields.items():
            key = f"{prefix}{name}" if prefix else name
            if key in seen:
                continue
            seen.add(key)
            default = field.get_default(call_default_factory=True)
            rows.append(
                {
                    "key": key,
                    "type": annot(field.annotation),
                    "default": "(required)" if field.is_required() else repr(default),
                    "required": "yes" if field.is_required() else "",
                    "description": (field.description or "").split("\n")[0],
                }
            )
            label = f"{key}.<name>" if is_dict_of_model(field.annotation) else key
            for sub in nested(field.annotation):
                walk(sub, label + ".", depth + 1)

    walk(qconfig.Config, "")

    settings_keys: set[str] = set()
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        settings_keys |= set(re.findall(r'settings\.get\(\s*"([a-z_]+)"', text))
    try:
        from . import dials

        numeric = dials.NUMERIC_AGENT_SETTINGS
    except Exception:  # pragma: no cover - dials is always importable
        numeric = {}
    for key in sorted(settings_keys):
        rows.append(
            {
                "key": f"agents.<name>.settings.{key}",
                "type": "int|float" if key in numeric else "str/any",
                "default": repr(numeric[key]) if key in numeric else "(agent-defined)",
                "required": "",
                "description": "read via settings.get() — not in the pydantic model",
            }
        )
    return rows


def counts() -> dict[str, int]:
    """The three numbers `quorum doctor` reports."""
    commands, params = cli_tree()
    keys = config_keys()
    return {
        "commands": sum(1 for c in commands if c["kind"] == "command"),
        "groups": sum(1 for c in commands if c["kind"] == "group"),
        "options": sum(1 for p in params if p["kind"] == "option"),
        "arguments": sum(1 for p in params if p["kind"] == "argument"),
        "config_keys": sum(1 for k in keys if "." in k["key"]),
        "config_tables": sum(1 for k in keys if "." not in k["key"]),
    }


def summary_line() -> str:
    """`53 commands, 170 options, 43 config keys` — doctor's informational line."""
    c = counts()
    return (
        f"{c['commands']} commands, {c['options']} options, {c['config_keys']} config keys"
    )
