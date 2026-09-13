# quorum

Queue long-running coding tasks and let the coding-agent CLI you already
use — `claude`, `codex`, `opencode`, anything that takes a prompt — do them in
isolated git worktrees. The supervisor is that same harness: quorum's
**manager** reads a digest of everything happening and decides what to launch,
nudge, relaunch or escalate, following a prompt you can edit. One process, no
root, no cron, no database; every byte of state is a plain file.

## Five steps

Needs Python 3.11 or newer.

```bash
# 1. install and configure a harness
uv tool install quorum-orchestrator     # or: pip install quorum-orchestrator
quorum init                             # scaffolds ~/.quorum and a config.toml
$EDITOR ~/.quorum/config.toml           # uncomment one [harness.*] block,
                                        # then set [tasks].default_harness
quorum doctor                           # check that config against reality

# 2. register a project
quorum project add ~/work/my-api

# 3. queue a task
quorum task add my-api "fix the flaky auth tests and open a PR"

# 4. start the supervisor
quorum up --detach                      # `quorum down` stops it
                                        # (without --detach it runs in the
                                        #  foreground and Ctrl-C stops it)

# 5. read status
quorum status                           # supervisor, agents, tasks, projects
quorum tui                              # the same, live
```

From there: `quorum task log <id> -f` follows a run, `quorum task nudge <id>
"..."` steers one, and `quorum manager tell "..."` steers the manager.

For a home that is already working rather than a scaffold to fill in, copy
[examples/dogfood-home/](https://github.com/kvndhrty/quorum/tree/main/examples/dogfood-home) —
the home that builds quorum itself: its config, its manager's house rules, the
issue-driven loop, and what a development cycle cost.

![quorum terminal dashboard](https://raw.githubusercontent.com/kvndhrty/quorum/main/docs/images/tui.png)

## What's genuinely different

Two things had no equivalent in a 2026-08 survey of about thirty
orchestration projects. That is a snapshot of a landscape that moves
monthly, not a permanent claim — if you know of prior art,
[open an issue](https://github.com/kvndhrty/quorum/issues).

**Your live session becomes a supervised task.** `quorum task adopt` inverts
the usual ownership: the interactive session you are already sitting in is
recorded as a task the manager watches and sends guidance to, rather than
one quorum spawned. The closest neighbour surveyed relays a session to your
phone so that *you* can steer it; none handed the session to a supervisor.

**The supervisor is the same harness, reading a file digest.** Among the
open-source tools surveyed, supervision meant keystroke automation — daemons
pressing enter, blind auto-confirmation. A model-driven supervisor appeared
only in hosted products, where the inputs and the decisions stay in someone
else's cloud. Here every input and decision is a file you can open: the task
records the digest is computed from, the policy that reads it
(`~/.quorum/prompts/manager.md`), and the journal of what it did and why
(`quorum manager journal`).

## Documentation

- **[docs/guide.md](https://github.com/kvndhrty/quorum/blob/main/docs/guide.md)** —
  everything above in depth, then a reference half that starts with a glossary.
- [docs/architecture.md](https://github.com/kvndhrty/quorum/blob/main/docs/architecture.md) —
  the design record: why each piece works the way it does.
- [CLAUDE.md](https://github.com/kvndhrty/quorum/blob/main/CLAUDE.md) — repo
  conventions and the layer-by-layer map, for contributors.

The PyPI distribution is `quorum-orchestrator`; the command it installs, and
the import name, are both `quorum`. From a checkout: `uv sync --all-extras`,
then `uv run pytest` and `uv run ruff check .`.
