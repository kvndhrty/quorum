"""The `quorum` command-line interface.

One module per command group — `task`, `agent`, `manager`, `board`,
`project`, `prompt`, `integration`, `notify`, and `root` for the commands
that belong to no group — over `_common`, which holds the typer apps
themselves and everything more than one group needs. Importing this package
imports all of them, which is what registers every command on `app`.

`quorum.cli.app` is still the entry point named in pyproject.toml and in
`python -m quorum`, and the helpers tests reach for are re-exported here,
so `from quorum.cli import _task_table` keeps working.
"""

from __future__ import annotations

# The module aliases the CLI reads through, reachable where they have
# always been: `quorum.cli.prune_mod` names the same module object
# `quorum.cli.task` calls, so patching one attribute reaches both.
from .. import doctor as doctor_mod  # noqa: F401
from .. import prompts as prompts_mod  # noqa: F401
from .. import prune as prune_mod  # noqa: F401
from .. import transcript as transcript_mod  # noqa: F401
from . import agent, board, integration, manager, notify, project, prompt, root, task  # noqa: F401

# The helpers and shared option objects, reachable where callers and tests
# have always reached them.
from ._common import (  # noqa: F401
    ID_MIN_WIDTH,
    PLAIN_TABLE_WIDTH,
    REPORT_MAX_CHARS,
    _actor_guard,
    _agent_table,
    _build_table,
    _confirm,
    _fail,
    _journal_task,
    _load_config,
    _notebook_write,
    _one_line,
    _parse_before,
    _parse_window,
    _pr_ref,
    _print_table,
    _project_table,
    _resolve_task,
    _stdin_prompt,
    _task_action,
    _task_table,
    _verbatim_text,
    agent_app,
    app,
    board_app,
    fsio,
    get_home,
    home_mod,
    integration_app,
    manager_app,
    notify_app,
    project_app,
    prompt_app,
    task_app,
)
from .root import STATUS_LEGEND  # noqa: F401

__all__ = ["app"]
