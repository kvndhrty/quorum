"""The fail-soft herdr adapter: observation and the nudge doorbell.

A tiny in-test unix-socket server plays herdr (newline-delimited JSON,
request/response), speaking the shapes the real 0.8 server speaks. The
adapter must degrade to None/False — silently — when the socket is absent
or the server misbehaves.
"""

from __future__ import annotations

import json
import shutil
import socket
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from quorum import fsio, herdr
from quorum.agents.manager import build_digest
from quorum.messages import MessageBus
from quorum.tasks import TaskStore, inbox_name, nudge, write_attached_state


@pytest.fixture
def sock_dir():
    """AF_UNIX paths are capped (~104 bytes on macOS); pytest's tmp_path is
    routinely deeper, so sockets get their own short-lived short directory."""
    d = Path(tempfile.mkdtemp(prefix="qh-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeHerdr(socketserver.ThreadingUnixStreamServer):
    """Answers pane.get / agent.prompt / pane.send_text; records requests."""

    def __init__(self, sock_path: Path, agent_status: str = "working", agent_pane: bool = True):
        self.requests_seen: list[dict] = []
        self.agent_status = agent_status
        self.agent_pane = agent_pane  # False: agent.prompt errors, send_text works
        super().__init__(str(sock_path), _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            req = json.loads(raw)
            self.server.requests_seen.append(req)
            method = req["method"]
            if method == "pane.get":
                result = {"type": "pane_info", "pane": {
                    "pane_id": req["params"]["pane_id"],
                    "agent_status": self.server.agent_status,
                }}
                resp = {"id": req["id"], "result": result}
            elif method == "agent.prompt":
                if self.server.agent_pane:
                    resp = {"id": req["id"], "result": {"type": "ok"}}
                else:
                    resp = {"id": req["id"], "error": {
                        "code": "agent_not_found", "message": "no agent in pane"}}
            elif method == "pane.send_text":
                resp = {"id": req["id"], "result": {"type": "ok"}}
            else:
                resp = {"id": req["id"], "error": {"code": "invalid_request", "message": "?"}}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


@pytest.fixture
def fake_herdr(home: Path, sock_dir: Path):
    sock = sock_dir / "fake-herdr.sock"
    (home / "config.toml").write_text(f'[herdr]\nsocket = "{sock}"\n')
    server = FakeHerdr(sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def adopt_task(home: Path, tmp_path: Path, pane: str | None = "w1:p3"):
    repo = tmp_path / "live"
    repo.mkdir(exist_ok=True)
    task = TaskStore(home).add(
        "liveproj", "adopted work", "", use_worktree=False,
        workdir=str(repo), attached=True, status="attached",
    )
    if pane:
        task = TaskStore(home).update(task.id, herdr_pane=pane)
    write_attached_state(home, task.id, "adopt")
    return task


def test_agent_state_reads_the_pane_status(home: Path, fake_herdr: FakeHerdr):
    assert herdr.agent_state(home, "w1:p3") == "working"
    fake_herdr.agent_status = "blocked"
    assert herdr.agent_state(home, "w1:p3") == "blocked"


def test_digest_enriched_with_pane_state(home: Path, tmp_path: Path, fake_herdr: FakeHerdr):
    task = adopt_task(home, tmp_path)
    digest = build_digest(home, TaskStore(home).list(), fsio.utc_now(), [])
    assert f"- [attached] {task.short_id}" in digest
    assert "herdr: state=working" in digest


def test_nudge_rings_the_doorbell_but_payload_stays_in_the_inbox(
    home: Path, tmp_path: Path, fake_herdr: FakeHerdr
):
    task = adopt_task(home, tmp_path)
    nudge(home, task, "check the failing test first")

    prompts = [r for r in fake_herdr.requests_seen if r["method"] == "agent.prompt"]
    assert len(prompts) == 1
    assert task.short_id in prompts[0]["params"]["text"]
    assert "check the failing test first" not in prompts[0]["params"]["text"]  # doorbell, not payload
    msgs = list(MessageBus(home).claim(inbox_name(task.id)))
    assert len(msgs) == 1
    assert msgs[0].message.payload["text"] == "check the failing test first"


def test_doorbell_falls_back_to_send_text_without_newline(
    home: Path, tmp_path: Path, fake_herdr: FakeHerdr
):
    fake_herdr.agent_pane = False
    assert herdr.ring_doorbell(home, "w1:p3", "quorum guidance waiting")
    sent = [r for r in fake_herdr.requests_seen if r["method"] == "pane.send_text"]
    assert len(sent) == 1
    assert not sent[0]["params"]["text"].endswith("\n")  # never execute in a foreign pane


def test_everything_degrades_silently_without_a_socket(home: Path, tmp_path: Path):
    (home / "config.toml").write_text(f'[herdr]\nsocket = "{tmp_path}/nope.sock"\n')
    assert herdr.available(home) is False
    assert herdr.agent_state(home, "w1:p1") is None
    assert herdr.ring_doorbell(home, "w1:p1", "hello") is False
    # a nudge to a pane-bearing task still lands in the inbox
    task = adopt_task(home, tmp_path)
    nudge(home, task, "still works")
    assert MessageBus(home).pending(inbox_name(task.id))


def test_disabled_config_wins_even_with_a_live_socket(
    home: Path, tmp_path: Path, fake_herdr: FakeHerdr
):
    sock = herdr.socket_path(home)
    (home / "config.toml").write_text(f'[herdr]\nsocket = "{sock}"\nenabled = false\n')
    assert herdr.available(home) is False
    assert herdr.agent_state(home, "w1:p3") is None


def test_misbehaving_server_degrades_to_none(home: Path, sock_dir: Path):
    sock = sock_dir / "garbage.sock"
    (home / "config.toml").write_text(f'[herdr]\nsocket = "{sock}"\n')

    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(sock))
    srv.listen(1)

    def garbage():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"this is not json\n")
        conn.close()

    t = threading.Thread(target=garbage, daemon=True)
    t.start()
    assert herdr.agent_state(home, "w1:p1") is None
    srv.close()
