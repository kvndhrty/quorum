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
