"""Local web dashboard: a thin FastAPI layer over the same files every other
view reads. Binds to 127.0.0.1 only. Reads dominate; the write actions are
posting a board note, acking one off the #attention banner, editing a
project's deadline/notes, sending guidance to a task, and creating/controlling
agents — all routed through the same code paths as the CLI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import transcript, views
from ..messages import MessageBus
from ..projects import ProjectRegistry
from ..tasks import TaskStore, read_reports, read_transcript_tail, runner_alive

STATIC_DIR = Path(__file__).parent / "static"


class BoardPost(BaseModel):
    text: str
    type: str = "note"


class ProjectPatch(BaseModel):
    deadline: str | None = None
    notes: str | None = None


class Nudge(BaseModel):
    text: str


class AgentCreate(BaseModel):
    name: str
    schedule: str = "every 1h"
    prompt_text: str
    harness: str = ""
    run_timeout_seconds: int = 0
    max_actions_per_run: int = 0


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
            # the narrative the CLI and the TUI print, rendered here so the
            # browser is not a fourth reading of the transcript format
            "narrative": transcript.render(read_transcript_tail(home, task.id, limit=40)),
            "reports": read_reports(home, task.id, limit=20),
        }

    @app.post("/api/tasks/{task_id}/nudge")
    def nudge(task_id: str, body: Nudge) -> dict:
        from ..tasks import nudge as nudge_task

        task = TaskStore(home).get(task_id)
        if task is None:
            raise HTTPException(404, f"no task {task_id!r}")
        msg = nudge_task(home, task, body.text, sender="user@web")
        return {"id": msg.id}

    @app.get("/api/agents/{name}")
    def agent_detail(name: str) -> dict:
        detail = views.agent_detail(home, name)
        if detail is None:
            raise HTTPException(404, f"no agent {name!r}")
        return detail

    @app.post("/api/agents")
    def agent_create(body: AgentCreate) -> dict:
        from ..config import ConfigError, create_agent

        settings: dict = {}
        if body.harness:
            settings["harness"] = body.harness
        if body.run_timeout_seconds:
            settings["run_timeout_seconds"] = body.run_timeout_seconds
        if body.max_actions_per_run:
            settings["max_actions_per_run"] = body.max_actions_per_run
        try:
            create_agent(
                home,
                body.name,
                schedule=body.schedule,
                settings=settings,
                prompt_text=body.prompt_text,
            )
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e
        MessageBus(home).send(
            "user@web", "supervisor", type="agent.reload", payload={"agent": body.name}
        )
        return {"name": body.name}

    @app.post("/api/agents/{name}/{command}")
    def agent_command(name: str, command: str) -> dict:
        if command not in ("pause", "resume", "run-now", "reload"):
            raise HTTPException(422, f"unknown agent command {command!r}")
        if not any(r["name"] == name for r in views.agent_rows(home)):
            raise HTTPException(404, f"no agent {name!r}")
        msg = MessageBus(home).send(
            "user@web", "supervisor", type=f"agent.{command}", payload={"agent": name}
        )
        return {"id": msg.id}

    @app.get("/api/board/{topic}")
    def board(topic: str, limit: int = 50) -> list:
        bus = MessageBus(home)
        return [m.dump() for m in bus.read_topic(topic, limit=limit)]

    @app.post("/api/board/{topic}")
    def post_note(topic: str, body: BoardPost) -> dict:
        msg = MessageBus(home).post("user@web", topic, type=body.type, text=body.text)
        return {"id": msg.id}

    @app.post("/api/board/{topic}/ack/{message_id}")
    def ack_note(topic: str, message_id: str) -> dict:
        """Archive one board message — the same `MessageBus.ack_board_message`
        `quorum board ack` and the TUI's `a` call, so the banner drops it and
        `messages/archive/` keeps it. Unknown and ambiguous both fail loudly:
        a silently-wrong ack archives someone else's escalation."""
        try:
            msg = MessageBus(home).ack_board_message(message_id, topic=topic)
        except KeyError:
            raise HTTPException(404, f"no live message {message_id!r} on {topic!r}") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"id": msg.id, "short_id": msg.short_id, "topic": msg.topic}

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
