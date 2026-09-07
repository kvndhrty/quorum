"""`quorum notify`: send one message through the [notify] template."""

from __future__ import annotations

import json

import typer

from ._common import (
    _fail,
    _load_config,
    get_home,
    notify_app,
)

# -- notify ----------------------------------------------------------------


@notify_app.command("test")
def notify_test(text: str) -> None:
    """Send one message through the [notify] template, right now.

    Proves the wiring without waiting for an escalation. It goes straight
    to the template — nothing is posted to the board and the hook's cursor
    is untouched — and unlike the supervisor's fail-soft delivery it is
    loud: a template that could not be run exits 1 and says why.
    """
    from .. import notify as notify_mod
    from ..messages import Message

    target = get_home()
    config = _load_config(target)
    if config.notify is None:
        raise _fail(
            "no [notify] table in config.toml — add one (docs/guide.md#getting-notified) "
            "to be told when the manager needs you"
        )
    message = Message.model_validate(
        {
            "from": "user",
            "topic": config.notify.topics[0],
            "type": "notify.test",
            "payload": {"text": text},
        }
    )
    argv = notify_mod.build_argv(config.notify.command, message)
    typer.echo("running: " + " ".join(json.dumps(element) for element in argv))
    failure = notify_mod.deliver(config.notify.command, message, config.notify.timeout_seconds)
    if failure is not None:
        raise _fail(f"not delivered: {failure}")
    typer.secho("delivered", fg="green")
