from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from quorum import fsio, tasks
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore
from quorum.web.app import create_app


@pytest.fixture
def client(home: Path) -> TestClient:
    return TestClient(create_app(home))


def test_index_and_overview(client: TestClient, home: Path, tmp_path: Path):
    ProjectRegistry(home).add(tmp_path, name="Web Proj", deadline="2026-09-01")
    MessageBus(home).post("monitor", "tasks", text="heads up")
    TaskStore(home).add("web-proj", "fix the tests", "fake")
    assert "<title>quorum</title>" in client.get("/").text
    data = client.get("/api/overview").json()
    assert data["supervisor"]["alive"] is False
    assert data["projects"][0]["name"] == "Web Proj"
    assert any(m["text"] == "heads up" for m in data["board"])
    assert data["tasks"][0]["status"] == "queued"


def test_task_detail_and_nudge(client: TestClient, home: Path):
    task = TaskStore(home).add("proj", "do it", "fake")
    fsio.append_jsonl(tasks.transcript_path(home, task.id), {"at": "t", "line": "hello"})
    tasks.report(home, task.id, status="executing", text="working")

    detail = client.get(f"/api/tasks/{task.id}").json()
    assert detail["status"] == "executing"
    assert detail["transcript"][0]["line"] == "hello"
    assert detail["reports"][0]["text"] == "working"
    assert client.get("/api/tasks/ghost").status_code == 404

    r = client.post(f"/api/tasks/{task.id}/nudge", json={"text": "steer left"})
    assert r.status_code == 200 and r.json()["id"]
    inbox = MessageBus(home).inbox_dir / tasks.inbox_name(task.id) / "new"
    entries = fsio.sorted_entries(inbox)
    assert len(entries) == 1
    assert fsio.read_json(entries[0])["payload"]["text"] == "steer left"
    assert client.post("/api/tasks/ghost/nudge", json={"text": "x"}).status_code == 404


def test_post_note_and_read_topic(client: TestClient):
    r = client.post("/api/board/notes", json={"text": "from the web"})
    assert r.status_code == 200 and r.json()["id"]
    msgs = client.get("/api/board/notes").json()
    assert msgs[0]["payload"]["text"] == "from the web"
    assert msgs[0]["from"] == "user@web"


def test_patch_project(client: TestClient, home: Path, tmp_path: Path):
    ProjectRegistry(home).add(tmp_path, name="Patchable")
    r = client.patch("/api/projects/patchable", json={"deadline": "2026-10-01", "notes": "n"})
    assert r.status_code == 200 and r.json()["deadline"] == "2026-10-01"
    assert client.patch("/api/projects/ghost", json={"deadline": "2026-10-01"}).status_code == 404
    assert client.patch("/api/projects/patchable", json={"deadline": "nope"}).status_code == 422


def test_agent_create_detail_and_control(client: TestClient, home: Path):
    r = client.post("/api/agents", json={
        "name": "standup", "schedule": "every 30m", "prompt_text": "post a note",
    })
    assert r.status_code == 200, r.text
    assert (home / "agents" / "standup.toml").exists()
    assert (home / "prompts" / "standup.md").read_text() == "post a note"

    detail = client.get("/api/agents/standup").json()
    assert detail["schedule"] == "every 30m"
    assert detail["journal"] == [] and detail["actions"] == []
    assert detail["notes"] == []  # an empty notebook is still an answer
    assert client.get("/api/agents/ghost").status_code == 404

    # the notebook rides along with the detail, read straight off its file
    from quorum import notes

    notes.remember(home, "the standup skips weekends", owner="standup")
    detail = client.get("/api/agents/standup").json()
    assert [n["text"] for n in detail["notes"]] == ["the standup skips weekends"]
    assert "skips weekends" in detail["notes_text"]

    # duplicate creation is refused, loudly
    r = client.post("/api/agents", json={"name": "standup", "prompt_text": "again"})
    assert r.status_code == 422

    assert client.post("/api/agents/standup/pause").status_code == 200
    assert client.post("/api/agents/standup/explode").status_code == 422
    assert client.post("/api/agents/ghost/pause").status_code == 404

    inbox = MessageBus(home).inbox_dir / "supervisor" / "new"
    types = [fsio.read_json(p)["type"] for p in fsio.sorted_entries(inbox)]
    assert types == ["agent.reload", "agent.pause"]


def test_overview_carries_attention(client: TestClient, home: Path):
    empty = client.get("/api/overview").json()["attention"]
    assert empty["count"] == 0
    MessageBus(home).post("manager", "attention", text="stuck: need credentials")
    attn = client.get("/api/overview").json()["attention"]
    assert attn["count"] == 1
    assert attn["recent"][0]["text"] == "stuck: need credentials"


def test_patch_deadline_empty_clears_it(client: TestClient, home: Path, tmp_path: Path):
    ProjectRegistry(home).add(tmp_path, name="Clearable", deadline="2026-09-01")
    r = client.patch("/api/projects/clearable", json={"deadline": ""})
    assert r.status_code == 200
    assert r.json()["deadline"] is None
    r = client.patch("/api/projects/clearable", json={"notes": "kept"})
    assert r.json()["deadline"] is None and r.json()["notes"] == "kept"


def test_task_rows_expose_dependencies(client: TestClient, home: Path):
    """The browser gets `waiting_on` from the same read model the CLI and the
    TUI use — nothing dependency-shaped is materialized to disk (#31)."""
    store = TaskStore(home)
    upstream = store.add("web-proj", "build it", "fake")
    dependent = store.add("web-proj", "review it", "fake", depends_on=[upstream.id])

    rows = {r["id"]: r for r in client.get("/api/tasks").json()}
    assert rows[dependent.id]["waiting_on"] == [upstream.short_id]
    assert rows[dependent.id]["dep_failed"] == []

    tasks.report(home, upstream.id, status="done", text="shipped")
    rows = {r["id"]: r for r in client.get("/api/tasks").json()}
    assert rows[dependent.id]["waiting_on"] == []


def test_task_rows_expose_the_observed_pr_state(client: TestClient, home: Path):
    """The browser badges merged off the same read model, and the dashboard
    never probes a forge: the field is on task.json because the manager tick
    put it there (#57)."""
    from quorum.web import app as web_app

    store = TaskStore(home)
    shipped = store.add("web-proj", "shipped it", "fake", status="done")
    store.update(shipped.id, pr_state="merged", pr_state_at="2026-01-01T00:00:00Z")
    quiet = store.add("web-proj", "never observed", "fake", status="done")

    rows = {r["id"]: r for r in client.get("/api/tasks").json()}
    assert rows[shipped.id]["pr_state"] == "merged"
    assert rows[shipped.id]["pr_state_at"] == "2026-01-01T00:00:00Z"
    assert rows[quiet.id]["pr_state"] is None

    page = (Path(web_app.__file__).parent / "static" / "index.html").read_text()
    assert 't.pr_state === "merged"' in page and 't.pr_state === "closed"' in page
