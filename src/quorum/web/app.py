"""Local web dashboard: a thin FastAPI layer over the same files every other
view reads. Binds to 127.0.0.1 only. Reads dominate; the only write actions
are posting a board note, editing a project's deadline/notes, and adopting a
scout proposal — all routed through the same code paths as the CLI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import views
from ..agents.scout import load_candidates
from ..messages import MessageBus
from ..projects import ProjectRegistry

STATIC_DIR = Path(__file__).parent / "static"


class BoardPost(BaseModel):
    text: str
    type: str = "note"


class ProjectPatch(BaseModel):
    deadline: str | None = None
    notes: str | None = None


def create_app(home: Path) -> FastAPI:
    app = FastAPI(title="quorum", docs_url=None, redoc_url=None)
    home = Path(home)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/overview")
    def overview() -> dict:
        data = views.overview(home)
        data["candidates"] = load_candidates(home)
        return data

    @app.get("/api/projects")
    def projects() -> list:
        return views.project_rows(home)

    @app.get("/api/board/{topic}")
    def board(topic: str, limit: int = 50) -> list:
        bus = MessageBus(home)
        return [m.dump() for m in bus.read_topic(topic, limit=limit)]

    @app.get("/api/brief/latest")
    def latest_brief() -> dict:
        briefs = sorted((home / "briefs").glob("*.md"))
        if not briefs:
            return {"date": None, "markdown": ""}
        return {"date": briefs[-1].stem, "markdown": briefs[-1].read_text(encoding="utf-8")}

    @app.post("/api/board/{topic}")
    def post_note(topic: str, body: BoardPost) -> dict:
        msg = MessageBus(home).post("user@web", topic, type=body.type, text=body.text)
        return {"id": msg.id}

    @app.patch("/api/projects/{slug}")
    def patch_project(slug: str, body: ProjectPatch) -> dict:
        try:
            project = ProjectRegistry(home).update(slug, deadline=body.deadline, notes=body.notes)
        except KeyError:
            raise HTTPException(404, f"no project {slug!r}")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"slug": project.slug, "deadline": project.deadline, "notes": project.notes}

    @app.post("/api/projects/{slug}/adopt")
    def adopt(slug: str) -> dict:
        path = load_candidates(home).get(slug)
        if path is None:
            raise HTTPException(404, f"no candidate {slug!r}")
        try:
            project = ProjectRegistry(home).add(path)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"slug": project.slug, "path": project.path}

    return app
