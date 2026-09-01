"""The [notify] hook: the board reaching a person.

A fake notifier (tests/bin/fake_notifier.py) plays terminal-notifier / ntfy /
curl. The hook's contract is herdr's, not sandbox.py's: a missing binary, a
nonzero exit or a hang must each be one supervisor.log line and an advanced
cursor — never a failed tick, never a lost later message, never a duplicate.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import fsio, notify
from quorum.cli import app
from quorum.config import Config, ConfigError, NotifyConfig, load_config
from quorum.messages import Message, MessageBus
from quorum.supervisor import Supervisor

FAKE_NOTIFIER = Path(__file__).parent / "bin" / "fake_notifier.py"
runner = CliRunner()


def notifier_argv(*extra: str) -> list[str]:
    return [sys.executable, str(FAKE_NOTIFIER), *extra]


@pytest.fixture
def delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Everything the fake notifier was invoked with, as a list of argv lists."""
    log = tmp_path / "notify.log"
    monkeypatch.setenv("FAKE_NOTIFY_LOG", str(log))
    monkeypatch.delenv("FAKE_NOTIFY_MODE", raising=False)

    def read() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    return read


def notify_config(*extra: str, topics: list[str] | None = None, timeout: float = 10.0):
    return NotifyConfig(
        command=notifier_argv("-m", "{text}", *extra),
        topics=topics or ["attention"],
        timeout_seconds=timeout,
    )


def configure(home: Path, *, command: list[str] | None = None, topics: str = '["attention"]',
              timeout: float = 10.0) -> Config:
    argv = json.dumps(command or notifier_argv("-m", "{text}"))
    (home / "config.toml").write_text(
        f"[notify]\ncommand = {argv}\ntopics = {topics}\ntimeout_seconds = {timeout}\n"
    )
    return load_config(home)


def cursor(home: Path, topic: str = "attention") -> str | None:
    return fsio.read_json(home / "state" / "notify.json")["cursors"].get(topic)


# -- config ----------------------------------------------------------------------


def test_notify_table_parses_with_defaults(home: Path):
    (home / "config.toml").write_text('[notify]\ncommand = ["ntfy", "pub", "q", "{text}"]\n')
    cfg = load_config(home).notify
    assert cfg is not None
    assert cfg.topics == ["attention"]
    assert cfg.timeout_seconds == 10.0


def test_notify_table_is_absent_by_default(home: Path):
    assert load_config(home).notify is None
    assert Config().notify is None


@pytest.mark.parametrize(
    "body",
    [
        '[notify]\ncommand = []\n',
        '[notify]\ncommand = [""]\n',
        '[notify]\ncommand = ["x"]\ntopics = []\n',
        '[notify]\ncommand = ["x"]\ntopics = [" "]\n',
        '[notify]\ncommand = ["x"]\ntimeout_seconds = 0\n',
        '[notify]\ntopics = ["attention"]\n',
    ],
)
def test_notify_table_rejects_a_hook_that_could_never_fire(home: Path, body: str):
    (home / "config.toml").write_text(body)
    with pytest.raises(ConfigError):
        load_config(home)


# -- the template ------------------------------------------------------------------


def message(text: str = "hello", **fields) -> Message:
    return Message.model_validate(
        {"from": "manager", "topic": "attention", "type": "escalation", "id": "01ID",
         "payload": {"text": text}, **fields}
    )


def test_placeholders_substitute_per_argv_element():
    argv = notify.build_argv(
        ["notify", "-t", "quorum {topic}/{type}", "-m", "{text}", "--from={from}", "{id}"],
        message("needs a $human; 'quoted' \"too\""),
    )
    assert argv == [
        "notify", "-t", "quorum attention/escalation", "-m",
        "needs a $human; 'quoted' \"too\"", "--from=manager", "01ID",
    ]


def test_a_template_without_text_gets_it_appended():
    """The harness-template convention: no {prompt} → appended last."""
    assert notify.build_argv(["say"], message("hi")) == ["say", "hi"]
    assert notify.build_argv(["say", "{text}"], message("hi")) == ["say", "hi"]  # not twice


def test_a_non_string_text_is_stringified_not_raised():
    assert notify.build_argv(["n", "{text}"], message(text=42)) == ["n", "42"]


def test_deliver_reports_success_and_each_failure_in_one_line(delivered, monkeypatch, tmp_path):
    assert notify.deliver(notifier_argv("{text}"), message("ok"), 10) is None
    assert delivered() == [["ok"]]

    monkeypatch.setenv("FAKE_NOTIFY_MODE", "fail")
    failure = notify.deliver(notifier_argv("{text}"), message(), 10)
    assert failure is not None and failure.startswith("exit 3") and "no display" in failure

    monkeypatch.setenv("FAKE_NOTIFY_MODE", "hang")
    failure = notify.deliver(notifier_argv("{text}"), message(), 0.3)
    assert failure is not None and "timed out" in failure

    failure = notify.deliver([str(tmp_path / "no-such-notifier"), "{text}"], message(), 10)
    assert failure is not None and "not found" in failure


# -- the drain --------------------------------------------------------------------


def test_first_drain_starts_at_the_tail_without_replaying_history(home: Path, delivered):
    """Turning the hook on must not deliver a month of old escalations."""
    bus = MessageBus(home)
    old = bus.post("manager", "attention", text="long ago")
    assert notify.drain(home, notify_config(), bus) == 0
    assert delivered() == []
    assert cursor(home) == old.filename()

    bus.post("manager", "attention", text="now")
    assert notify.drain(home, notify_config(), bus) == 1
    assert delivered() == [["-m", "now"]]


def test_first_drain_on_an_empty_topic_still_arms_the_cursor(home: Path, delivered):
    """An empty topic is not "never initialized": the first escalation ever
    posted after enabling must go out, not be swallowed as history."""
    bus = MessageBus(home)
    notify.drain(home, notify_config(), bus)
    assert cursor(home) == ""
    bus.post("manager", "attention", text="first ever")
    notify.drain(home, notify_config(), bus)
    assert delivered() == [["-m", "first ever"]]


def test_post_is_delivered_once_and_never_again(home: Path, delivered):
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()  # arms the cursor
    MessageBus(home).post("manager", "attention", "escalation", text="PR #44 has conflicts")
    sup._notify()
    assert delivered() == [["-m", "PR #44 has conflicts"]]
    sup._notify()
    sup._notify()
    assert delivered() == [["-m", "PR #44 has conflicts"]]


def test_restart_does_not_redeliver(home: Path, delivered):
    config = configure(home)
    Supervisor(home, config)._notify()
    MessageBus(home).post("manager", "attention", text="once")
    Supervisor(home, config)._notify()
    assert delivered() == [["-m", "once"]]
    # a fresh supervisor reads the same state/notify.json
    Supervisor(home, config)._notify()
    assert delivered() == [["-m", "once"]]


def test_posts_while_down_are_delivered_in_order_on_restart(home: Path, delivered):
    config = configure(home)
    Supervisor(home, config)._notify()
    bus = MessageBus(home)
    bus.post("manager", "attention", text="first")
    bus.post("supervisor", "attention", "agent.failing", text="second")
    # ...supervisor was down for both; it comes back:
    Supervisor(home, config)._notify()
    assert delivered() == [["-m", "first"], ["-m", "second"]]


def test_only_listed_topics_fire(home: Path, delivered):
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()
    bus = MessageBus(home)
    bus.post("supervisor", "system", "agent.error", text="tick failed")
    bus.post("runner", "tasks", "task.started", text="started")
    sup._notify()
    assert delivered() == []
    bus.post("manager", "attention", text="me")
    sup._notify()
    assert delivered() == [["-m", "me"]]


def test_several_topics_each_keep_their_own_cursor(home: Path, delivered):
    config = configure(home, topics='["attention", "system"]')
    sup = Supervisor(home, config)
    sup._notify()
    bus = MessageBus(home)
    bus.post("supervisor", "system", text="sys")
    bus.post("manager", "attention", text="att")
    sup._notify()
    assert sorted(delivered()) == [["-m", "att"], ["-m", "sys"]]
    sup._notify()
    assert len(delivered()) == 2


def test_no_table_means_no_job_work_and_no_state(home: Path, delivered):
    sup = Supervisor(home, load_config(home))
    MessageBus(home).post("manager", "attention", text="unheard")
    sup._notify()
    assert delivered() == []
    assert not (home / "state" / "notify.json").exists()


# -- fail-soft: the three disappointments ---------------------------------------------


def test_missing_binary_is_one_log_line_and_the_cursor_advances(
    home: Path, delivered, caplog, tmp_path: Path
):
    caplog.set_level(logging.INFO, logger="quorum")
    config = configure(home, command=[str(tmp_path / "no-such-notifier"), "{text}"])
    sup = Supervisor(home, config)
    sup._notify()
    posted = MessageBus(home).post("manager", "attention", text="lost")
    sup._notify()  # must not raise
    assert cursor(home) == posted.filename()
    lines = [r.getMessage() for r in caplog.records if "not delivered" in r.getMessage()]
    assert len(lines) == 1 and "not found" in lines[0] and "cursor advanced" in lines[0]
    sup._notify()
    assert len([r for r in caplog.records if "not delivered" in r.getMessage()]) == 1


def test_nonzero_exit_advances_the_cursor_and_the_next_message_still_goes(
    home: Path, delivered, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="quorum")
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()
    bus = MessageBus(home)
    monkeypatch.setenv("FAKE_NOTIFY_MODE", "fail")
    bus.post("manager", "attention", text="undeliverable")
    sup._notify()
    assert delivered() == [["-m", "undeliverable"]]  # invoked, exit 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "exit 3" in warnings[0].getMessage()

    monkeypatch.setenv("FAKE_NOTIFY_MODE", "ok")
    bus.post("manager", "attention", text="after it")
    sup._notify()
    assert delivered() == [["-m", "undeliverable"], ["-m", "after it"]]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_hang_is_killed_at_timeout_and_the_cursor_advances(
    home: Path, delivered, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="quorum")
    config = configure(home, timeout=0.3)
    sup = Supervisor(home, config)
    sup._notify()
    bus = MessageBus(home)
    monkeypatch.setenv("FAKE_NOTIFY_MODE", "hang")
    posted = bus.post("manager", "attention", text="slow")
    sup._notify()
    assert cursor(home) == posted.filename()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "timed out" in warnings[0]
    monkeypatch.setenv("FAKE_NOTIFY_MODE", "ok")
    bus.post("manager", "attention", text="next")
    sup._notify()
    assert delivered()[-1] == ["-m", "next"]


def test_an_unreadable_cursor_file_is_reinitialized_not_fatal(home: Path, delivered, caplog):
    caplog.set_level(logging.INFO, logger="quorum")
    config = configure(home)
    sup = Supervisor(home, config)
    (home / "state" / "notify.json").write_text("{not json")
    MessageBus(home).post("manager", "attention", text="during the breakage")
    sup._notify()  # must not raise
    assert delivered() == []
    assert cursor(home)  # re-armed at the tail
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    MessageBus(home).post("manager", "attention", text="after")
    sup._notify()
    assert delivered() == [["-m", "after"]]


def test_an_unreadable_board_file_is_stepped_over(home: Path, delivered):
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()
    board = home / "messages" / "board" / "attention"
    board.mkdir(parents=True, exist_ok=True)
    (board / "20990101T000000Z-ZZZZ.json").write_text("garbage")
    sup._notify()
    assert cursor(home) == "20990101T000000Z-ZZZZ.json"
    assert delivered() == []
    sup._notify()  # not re-read forever


def test_a_drain_that_cannot_write_its_cursor_does_not_raise(home: Path, delivered, caplog):
    caplog.set_level(logging.INFO, logger="quorum")
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()
    (home / "state" / "notify.json").unlink()
    (home / "state" / "notify.json").mkdir()  # a directory where the file goes
    MessageBus(home).post("manager", "attention", text="x")
    sup._notify()  # must not raise
    assert any("drain failed" in r.getMessage() for r in caplog.records)


def test_per_tick_cap_leaves_the_rest_for_the_next_tick(home: Path, delivered, monkeypatch):
    monkeypatch.setattr(notify, "MAX_PER_TICK", 2)
    config = configure(home)
    sup = Supervisor(home, config)
    sup._notify()
    bus = MessageBus(home)
    for i in range(3):
        bus.post("manager", "attention", text=f"m{i}")
    sup._notify()
    assert delivered() == [["-m", "m0"], ["-m", "m1"]]
    sup._notify()
    assert delivered() == [["-m", "m0"], ["-m", "m1"], ["-m", "m2"]]


# -- quorum notify test -----------------------------------------------------------------


def test_notify_test_sends_through_the_template_and_touches_nothing(home: Path, delivered):
    configure(home)
    result = runner.invoke(app, ["notify", "test", "hello there", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "delivered" in result.output
    assert delivered() == [["-m", "hello there"]]
    assert not (home / "state" / "notify.json").exists()
    assert MessageBus(home).read_topic("attention") == []


def test_notify_test_is_loud_about_a_template_that_cannot_run(home: Path, tmp_path: Path):
    configure(home, command=[str(tmp_path / "no-such-notifier"), "{text}"])
    result = runner.invoke(app, ["notify", "test", "hello", "--home", str(home)])
    assert result.exit_code == 1
    assert "not delivered" in result.output and "not found" in result.output


def test_notify_test_is_loud_about_a_nonzero_exit(home: Path, delivered, monkeypatch):
    configure(home)
    monkeypatch.setenv("FAKE_NOTIFY_MODE", "fail")
    result = runner.invoke(app, ["notify", "test", "hello", "--home", str(home)])
    assert result.exit_code == 1
    assert "exit 3" in result.output


def test_notify_test_without_a_table_says_how_to_add_one(home: Path):
    result = runner.invoke(app, ["notify", "test", "hello", "--home", str(home)])
    assert result.exit_code == 1
    assert "no [notify] table" in result.output
