from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from quorum import fsio
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.web.app import create_app


@pytest.fixture
def client(home: Path) -> TestClient:
    return TestClient(create_app(home))


def test_index_and_overview(client: TestClient, home: Path, tmp_path: Path):
    ProjectRegistry(home).add(tmp_path, name="Web Proj", deadline="2026-09-01")
    MessageBus(home).post("sentinel", "reminders", text="heads up")
    assert "<title>quorum</title>" in client.get("/").text
    data = client.get("/api/overview").json()
    assert data["supervisor"]["alive"] is False
    assert data["projects"][0]["name"] == "Web Proj"
    assert any(m["text"] == "heads up" for m in data["board"])


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


def test_adopt_candidate(client: TestClient, home: Path, tmp_path: Path):
    pdir = tmp_path / "cand"
    pdir.mkdir()
    fsio.atomic_write_json(home / "state/scout/candidates.json", {"cand": str(pdir)})
    r = client.post("/api/projects/cand/adopt")
    assert r.status_code == 200
    assert ProjectRegistry(home).get("cand") is not None
    assert client.post("/api/projects/ghost/adopt").status_code == 404


def test_brief_latest_empty_then_present(client: TestClient, home: Path):
    assert client.get("/api/brief/latest").json()["date"] is None
    (home / "briefs" / "2026-08-20.md").write_text("# hi")
    got = client.get("/api/brief/latest").json()
    assert got["date"] == "2026-08-20" and got["markdown"] == "# hi"
