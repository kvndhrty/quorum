# quorum

**An agentic ecosystem of specialists** — a small collection of always-on
agents that make a busy researcher's life easier: project tracking, deadline
reminders, file organization, daily briefs. One process, no root, no cron,
no database; everything is a plain file you can `cat`.

```
┌─ quorum up ────────────────────────────────────────────┐
│  tracker   watches your project dirs (git or mtimes)   │
│  sentinel  escalating deadline reminders               │
│  steward   keeps ~/Downloads organized (undo-able)     │
│  scribe    writes a daily brief (LLM optional)         │
│  scout     finds projects you forgot to register       │
└──────────────── all state in ~/.quorum ────────────────┘
        ▲                     ▲                    ▲
   quorum status         quorum tui           quorum web
```

## Install

```bash
uv tool install "quorum[web,tui]"     # or: pipx / pip install
```

From a checkout: `uv sync --all-extras`, then prefix commands with `uv run`.

## Five minutes to a working quorum

```bash
quorum init                                          # scaffold ~/.quorum
quorum project add ~/work/big-paper --deadline 2026-09-15
quorum up                                            # foreground; Ctrl-C stops
```

Background it however you like — `nohup quorum up &`, a tmux pane, a
`screen` session. No systemd or cron needed (if you *have* cron, you can
skip the supervisor entirely and schedule `quorum agent run-once <name>`).

Then, in another shell:

```bash
quorum status              # liveness, heartbeats, deadlines
quorum tui                 # live terminal dashboard
quorum web                 # http://127.0.0.1:8787
quorum board read          # the agents' shared message board
quorum brief               # today's daily brief
```

## How it works

- **One supervisor process** hosts [APScheduler](https://apscheduler.readthedocs.io/);
  each agent is a scheduled job with crash isolation, heartbeat files, and
  auto-pause after repeated failures.
- **All state is files** under `QUORUM_HOME` (default `~/.quorum`):
  projects, agent state, logs, briefs, and a **filesystem message bus** — an
  append-only public board plus maildir-style per-agent inboxes, written
  with atomic tmp+rename. Copy the directory and your whole setup moves.
- **Dashboards are pure readers.** The web UI (localhost-only) and the TUI
  render the same files and work even when the supervisor is down.
- **Projects are explicit.** `quorum project add`, or let the **scout**
  propose candidates from your workspace roots and confirm with
  `quorum project adopt <slug>`. Drop a `.quorum.toml` in a repo to carry
  its deadline/tags with it across machines.

## Optional LLM

Agents work fully without one. To upgrade summaries and file classification,
point `[llm]` in `~/.quorum/config.toml` at **any CLI** that turns a prompt
into text:

```toml
[llm]
executable = "claude"   # or codex / llm / ollama / your own script
args = ["-p"]
input = "stdin"         # or "argv" with a {prompt} placeholder
```

Prompt templates live in `~/.quorum/prompts/*.md` — edit them to retune an
agent, delete one to restore the default. Every LLM failure degrades to the
deterministic behavior; a broken API never breaks your briefs.

## Optional sandbox

Quorum pairs naturally with [nono](https://github.com/nolabs-ai/nono)
(kernel-enforced sandboxing via Landlock/Seatbelt):

```bash
nono run --profile quorum -- quorum up
```

or `quorum up --self-sandbox` / `[sandbox] use_nono = true` with the
`[nono]` extra. Profiles are short because quorum only needs its own
directory read-write and your project dirs read-only. See
[docs/nono.md](docs/nono.md).

## Customizing

- **Configure** agents (schedules, watch dirs, rules, tiers) in
  `config.toml` — quorum never rewrites that file.
- **Retune** LLM behavior by editing `prompts/*.md`.
- **Extend** by dropping a ~20-line Python file into `~/.quorum/plugins/` —
  see [docs/writing-agents.md](docs/writing-agents.md).

Architecture details: [docs/architecture.md](docs/architecture.md).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```
