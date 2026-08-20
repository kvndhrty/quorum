# Writing your own agent

An agent is a class with a synchronous `tick()`. Drop a module into
`QUORUM_HOME/plugins/`, reference it from `config.toml`, done — no
packaging, no entry points.

## A complete example

`~/.quorum/plugins/wordcount.py`:

```python
from pathlib import Path

from quorum.agent import Agent


class WordCount(Agent):
    """Posts a note whenever a watched manuscript grows past a milestone."""

    def tick(self):
        manuscript = Path(self.ctx.settings.get("file", "")).expanduser()
        if not manuscript.is_file():
            return
        words = len(manuscript.read_text(errors="ignore").split())
        state = self.ctx.load_state()
        last = state.get("last_milestone", 0)
        milestone = (words // 1000) * 1000
        if milestone > last:
            self.ctx.bus.post(
                self.name, "writing", "milestone",
                text=f"{manuscript.name} passed {milestone} words ({words} now)",
            )
            self.ctx.log_action("milestone", f"{manuscript.name}: {words} words")
            state["last_milestone"] = milestone
            self.ctx.save_state(state)
```

`~/.quorum/config.toml`:

```toml
[agents.wordcount]
type = "wordcount:WordCount"
schedule = "every 2h"
[agents.wordcount.settings]
file = "~/work/thesis/main.tex"
```

Test it immediately with `quorum agent run-once wordcount`.

## The contract

- `tick()` must be **idempotent**: it can be re-run at any time (missed
  schedules coalesce, `run-once` exists, crashes get retried). Use
  `self.ctx.load_state()` / `save_state()` to remember what you already did.
- Raising is fine: the supervisor logs the traceback, marks your heartbeat
  `error`, posts to the `system` topic, and pauses the agent after 5
  consecutive failures. You cannot take down other agents.
- Use `self.ctx.now()` instead of `datetime.now()` — it makes your agent
  testable with a fake clock.

## What the context gives you

| Member | Purpose |
|---|---|
| `ctx.settings` | your `[agents.<name>.settings]` table, verbatim |
| `ctx.bus.post(sender, topic, type=, text=, payload=)` | broadcast to the board |
| `ctx.bus.send(sender, to, ...)` | direct mail to another agent's inbox |
| `ctx.bus.claim(name)` | consume your own inbox (call `.ack()` per message) |
| `ctx.bus.read_after_cursor(topic, cursor)` | follow a board topic incrementally |
| `ctx.projects.list()` / `.get(slug)` | registered projects, marker-merged |
| `ctx.llm.complete(prompt)` | completion or `None` — always handle `None` |
| `ctx.prompt(name, **placeholders)` | render a template from `prompts/` |
| `ctx.load_state()` / `ctx.save_state(d)` | your private JSON state |
| `ctx.log_action(type, text, **data)` | feed the dashboards' activity log |
| `ctx.now()` | injectable clock |

## Testing

```python
from quorum.agent import AgentContext
from wordcount import WordCount

def test_milestone(tmp_path):
    home = tmp_path / "qhome"
    from quorum.home import scaffold; scaffold(home)
    (tmp_path / "ms.txt").write_text("word " * 1500)
    ctx = AgentContext(home=home, name="wc", settings={"file": str(tmp_path / "ms.txt")})
    WordCount(ctx).tick()
    assert ctx.bus.read_topic("writing")
```
