"""Local web dashboard: a thin FastAPI layer over the same files every other
view reads. Binds to 127.0.0.1 only. Reads dominate; the write actions are
posting a board note, editing a project's deadline/notes, and sending
guidance to a task — all routed through the same code paths as the CLI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import views
from ..messages import MessageBus
from ..projects import ProjectRegistry
from ..tasks import TaskStore, inbox_name, read_reports, read_transcript_tail, runner_alive

STATIC_DIR = Path(__file__).parent / "static"


class BoardPost(BaseModel):
    text: str
    type: str = "note"


class ProjectPatch(BaseModel):
    deadline: str | None = None
    notes: str | None = None


class Nudge(BaseModel):
    text: str


def create_app(home: Path) -> FastAPI:
    app = FastAPI(title="quorum", docs_url=None, redoc_url=None)
    home = Path(home)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/overview")
    def overview() -> dict:
        return views.overview(home)

    @app.get("/api/projects")
    def projects() -> list:
        return views.project_rows(home)

    @app.get("/api/tasks")
    def tasks() -> list:
        return views.task_rows(home)

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str) -> dict:
        task = TaskStore(home).get(task_id)
        if task is None:
            raise HTTPException(404, f"no task {task_id!r}")
        return {
            **task.model_dump(),
            "running": runner_alive(home, task.id),
            "transcript": read_transcript_tail(home, task.id, limit=40),
            "reports": read_reports(home, task.id, limit=20),
        }

    @app.post("/api/tasks/{task_id}/nudge")
    def nudge(task_id: str, body: Nudge) -> dict:
        task = TaskStore(home).get(task_id)
        if task is None:
            raise HTTPException(404, f"no task {task_id!r}")
        msg = MessageBus(home).send("user@web", inbox_name(task.id), type="guidance", text=body.text)
        return {"id": msg.id}

    @app.get("/api/board/{topic}")
    def board(topic: str, limit: int = 50) -> list:
        bus = MessageBus(home)
        return [m.dump() for m in bus.read_topic(topic, limit=limit)]

    @app.post("/api/board/{topic}")
    def post_note(topic: str, body: BoardPost) -> dict:
        msg = MessageBus(home).post("user@web", topic, type=body.type, text=body.text)
        return {"id": msg.id}

    @app.patch("/api/projects/{slug}")
    def patch_project(slug: str, body: ProjectPatch) -> dict:
        try:
            project = ProjectRegistry(home).update(slug, deadline=body.deadline, notes=body.notes)
        except KeyError:
            raise HTTPException(404, f"no project {slug!r}") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"slug": project.slug, "deadline": project.deadline, "notes": project.notes}

    return app
