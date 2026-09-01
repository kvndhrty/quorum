"""A generic harness-driven agent: a user-written prompt on a schedule.

Where the manager's tick compiles a task-supervision digest, a prompt agent
just renders its own prompt template (`prompts/<name>.md` by default) and
runs the configured harness over it, tagged with the actor protocol so its
CLI actions are journaled and capped exactly like the manager's. There is no
wake condition — a scheduled prompt agent spends a harness run every tick;
conditional behavior belongs in the prompt (or in a sparser schedule).
"""

from __future__ import annotations

from .. import fsio, notes
from ..agent import Agent
from ..runner import guidance_note
from .harness_run import run_agent_harness


class PromptAgent(Agent):
    default_schedule = "every 1h"

    def tick(self) -> None:
        claimed = list(self.ctx.bus.claim(self.ctx.name))
        directives = [guidance_note(c.message) for c in claimed]
        try:
            template = self.ctx.settings.get("prompt") or self.ctx.name
            rendered_directives = (
                "\n".join(f"- {d}" for d in directives) if directives else "(none)"
            )
            prompt = self.ctx.prompt(
                template,
                now=fsio.iso(self.ctx.now()),
                directives=rendered_directives,
                # a template that never writes `{notes}` simply never sees
                # its own notebook — but one that does gets the same
                # rendering, under the same caps, as the manager's digest
                notes="\n".join(
                    notes.digest_section(self.ctx.home, self.ctx.name, now=self.ctx.now())
                ),
            )
            run_agent_harness(self.ctx, prompt)
        except BaseException:
            for c in claimed:
                c.reject()  # directives go straight back to new/ for the next tick
            raise
        for c in claimed:
            c.ack()
        self.ctx.log_action("agent.run", f"{self.name} run complete")
