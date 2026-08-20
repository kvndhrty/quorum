"""Deadline sentinel: escalating reminders for project deadlines.

Fires a reminder when a project's days-remaining crosses each configured
tier (default 14/7/3/1/0 days out), then daily once overdue. Which tier was
last fired is recorded per project in the agent's private state, so restarts
and re-runs never double-post; a changed deadline resets the escalation.
Purely deterministic — no LLM involved.
"""

from __future__ import annotations

from ..agent import Agent

DEFAULT_TIERS = [14, 7, 3, 1, 0]


class Sentinel(Agent):
    default_schedule = "cron 0 8 * * *"

    def tick(self) -> None:
        tiers = sorted((int(t) for t in self.ctx.settings.get("tiers", DEFAULT_TIERS)), reverse=True)
        today = self.ctx.now().date()
        state = self.ctx.load_state()
        per_project = state.setdefault("projects", {})

        for project in self.ctx.projects.list():
            days_left = project.days_left(today)
            if days_left is None:
                per_project.pop(project.slug, None)
                continue
            entry = per_project.setdefault(project.slug, {})
            if entry.get("deadline") != project.deadline:
                entry.clear()  # deadline changed: restart the escalation ladder
                entry["deadline"] = project.deadline

            if days_left < 0:
                if entry.get("last_overdue") != str(today):
                    entry["last_overdue"] = str(today)
                    self._post(project, days_left, "deadline.overdue")
                continue

            applicable = [t for t in tiers if days_left <= t]
            if not applicable:
                continue
            tier = min(applicable)
            if entry.get("last_tier") is None or entry["last_tier"] > tier:
                entry["last_tier"] = tier
                self._post(project, days_left, "deadline.approaching", tier=tier)

        self.ctx.save_state(state)

    def _post(self, project, days_left: int, type: str, tier: int | None = None) -> None:
        if days_left < 0:
            text = f"{project.name} is {-days_left} day(s) OVERDUE (was due {project.deadline})"
        elif days_left == 0:
            text = f"{project.name} is due TODAY ({project.deadline})"
        else:
            text = f"{project.name} due in {days_left} day(s) ({project.deadline})"
        self.ctx.bus.post(
            self.name,
            topic="reminders",
            type=type,
            text=text,
            payload={
                "project": project.slug,
                "deadline": project.deadline,
                "days_left": days_left,
                **({"tier": tier} if tier is not None else {}),
            },
        )
        self.ctx.log_action(type, text, project=project.slug)
