"""Scribe: composes the daily brief.

Always produces a complete deterministic Markdown brief (reminders first,
project table, then the rest of the board). When an LLM is configured, a
short narrative paragraph is prepended — and if the LLM call fails, the
deterministic brief still stands in full.
"""

from __future__ import annotations

from datetime import timedelta

from .. import fsio
from ..agent import Agent

WINDOW_HOURS = 24


class Scribe(Agent):
    default_schedule = "cron 30 17 * * *"

    def tick(self) -> None:
        now = self.ctx.now()
        today = now.date()
        path = self.ctx.home / "briefs" / f"{today}.md"

        sections = self._collect(now)
        body = self._deterministic_brief(today, sections)

        narrative = None
        if self.ctx.llm.enabled:
            digest = self._digest_for_llm(sections)
            prompt = self.ctx.prompt("scribe-brief", date=str(today), data=digest)
            narrative = self.ctx.llm.complete(prompt)

        if narrative:
            body = f"# Daily brief — {today}\n\n{narrative.strip()}\n\n---\n\n" + body.split("\n", 2)[2]

        fsio.atomic_write_text(path, body)
        text = f"daily brief ready: briefs/{today}.md"
        self.ctx.bus.post(self.name, "system", "brief.ready", text=text,
                          payload={"path": f"briefs/{today}.md"})
        self.ctx.log_action("brief.ready", text)

    # -- data gathering ----------------------------------------------------

    def _collect(self, now) -> dict:
        since = now - timedelta(hours=WINDOW_HOURS)
        topics: dict[str, list] = {}
        for topic in self.ctx.bus.topics():
            msgs = self.ctx.bus.read_topic(topic, since=since)
            if msgs:
                topics[topic] = msgs
        projects = []
        today = now.date()
        for p in self.ctx.projects.list():
            status = {}
            try:
                status = fsio.read_json(
                    self.ctx.home / "state" / "projects" / p.slug / "status.json"
                )
            except (OSError, ValueError):
                pass
            projects.append(
                {
                    "name": p.name,
                    "deadline": p.deadline,
                    "days_left": p.days_left(today),
                    "last_activity": status.get("last_activity_at"),
                    "summary": status.get("summary", ""),
                }
            )
        return {"topics": topics, "projects": projects}

    # -- rendering -----------------------------------------------------------

    def _deterministic_brief(self, today, sections: dict) -> str:
        lines = [f"# Daily brief — {today}", ""]
        reminders = sections["topics"].get("reminders", [])
        if reminders:
            lines += ["## Deadlines", ""]
            lines += [f"- {m.payload.get('text', m.type)}" for m in reminders]
            lines.append("")
        if sections["projects"]:
            lines += ["## Projects", "", "| project | deadline | activity |", "|---|---|---|"]
            for p in sections["projects"]:
                dl = "—"
                if p["deadline"]:
                    dl = p["deadline"]
                    if p["days_left"] is not None:
                        dl += f" ({p['days_left']}d)" if p["days_left"] >= 0 else f" (overdue {-p['days_left']}d)"
                lines.append(f"| {p['name']} | {dl} | {p['summary'] or '—'} |")
            lines.append("")
        for topic, msgs in sorted(sections["topics"].items()):
            if topic == "reminders":
                continue
            lines += [f"## {topic}", ""]
            lines += [f"- `{m.sender}` {m.payload.get('text', m.type)}" for m in msgs]
            lines.append("")
        if len(lines) <= 2:
            lines += ["Nothing to report in the last 24 hours.", ""]
        return "\n".join(lines)

    def _digest_for_llm(self, sections: dict) -> str:
        parts = []
        for p in sections["projects"]:
            parts.append(
                f"project {p['name']}: deadline={p['deadline'] or 'none'} "
                f"days_left={p['days_left']} activity={p['summary'] or 'unknown'}"
            )
        for topic, msgs in sorted(sections["topics"].items()):
            for m in msgs:
                parts.append(f"[{topic}] {m.sender}: {m.payload.get('text', m.type)}")
        return "\n".join(parts) or "(no activity)"
