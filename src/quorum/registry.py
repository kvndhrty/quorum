"""Resolve agent `type` strings from config into Agent classes.

Builtins resolve by short name ("sentinel"); anything else is a
"module:Class" dotted path. QUORUM_HOME/plugins is prepended to sys.path, so
a researcher adds a custom agent by dropping a .py file there and writing

    [agents.myagent]
    type = "myagent_module:MyAgent"

in config.toml — no packaging required.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .agent import Agent


class AgentResolutionError(RuntimeError):
    pass


def builtin_names() -> list[str]:
    from .agents import BUILTIN_NAMES

    return sorted(BUILTIN_NAMES)


def resolve(type_name: str, home: Path) -> type[Agent]:
    from .agents import BUILTIN_NAMES, get_builtin

    if type_name in BUILTIN_NAMES:
        return get_builtin(type_name)
    if ":" not in type_name:
        raise AgentResolutionError(
            f"unknown agent type {type_name!r}: not a builtin "
            f"({', '.join(builtin_names())}) and not a 'module:Class' path"
        )
    plugins = str(Path(home) / "plugins")
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
    module_name, _, class_name = type_name.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise AgentResolutionError(f"cannot import module {module_name!r}: {e}") from e
    cls = getattr(module, class_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, Agent)):
        raise AgentResolutionError(
            f"{type_name!r} does not name an Agent subclass in {module_name!r}"
        )
    return cls
