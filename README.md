# quorum

**Orchestrate long-running coding tasks with the harness you already use.**
Point quorum at your repos, queue tasks in plain English, and let your coding
agent CLI — `claude`, `codex`, `opencode`, anything that takes a prompt — do
the work in an isolated git worktree while quorum's monitor watches progress,
pokes agents that stall, and escalates to you when a human is really needed.
One process, no root, no cron, no database; everything is a plain file you
can `cat`.

```
quorum task add my-api "add rate limiting to the public endpoints, then open a PR"
        │
┌─ quorum up ─────────────────────────────────────────────────────┐
│  monitor    launches queued tasks, watches for stalls,          │
│             pokes stuck agents, escalates when blocked          │
│                                                                 │
│  task runs  your harness (claude/codex/opencode/...) working    │
│             in worktrees/<task>/, reporting progress back       │
│             through `quorum task report` and reading your       │
│             guidance from `quorum task inbox`                   │
└──────────────────── all state in ~/.quorum ─────────────────────┘
        ▲                    ▲                    ▲
   quorum status        quorum tui           quorum web
   quorum task tail     (steer with `n`)     (localhost only)
```

## Install

```bash
uv tool install "quorum[web,tui]"     # or: pipx / pip install
```

From a checkout: `uv sync --all-extras`, then prefix commands with `uv run`.

## Five minutes to a running task

```bash
quorum init                             # scaffold ~/.quorum
```

Tell quorum how to invoke your harness (uncomment in `~/.quorum/config.toml`):

```toml
[tasks]
default_harness = "claude"

[harness.claude]
start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose"]
resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--verbose"]
```

Register a repo and queue work:

```bash
quorum project add ~/work/my-api
quorum task add my-api "fix the flaky auth tests and open a PR"
quorum up                               # foreground; Ctrl-C stops
```

Background `quorum up` however you like — `nohup`, tmux, a `screen` session.
Then, from another shell:

```bash
quorum status                # supervisor, agents, tasks, deadlines
quorum task tail a3f2k9 -f   # live harness transcript
quorum task nudge a3f2k9 "prefer the retry approach over sleeps"
quorum tui                   # dashboard; select a task, press n to steer
quorum web                   # http://127.0.0.1:8787
```

## How it works

- **A task is a sequence of harness runs.** Each run executes in the task's
  own git worktree (`~/.quorum/worktrees/<id>`), so parallel tasks on one
  repo never collide and your checkout stays clean. Session ids are captured
  from the harness's output so follow-up runs can `--resume`.
- **Status is the harness's own words.** The run prompt teaches a simple
  protocol — report progress with `quorum task report`, check guidance with
  `quorum task inbox` — and the conventional flow is
  `planning → executing → reviewing → pr → done`. Quorum records what the
  harness says; it never enforces a state machine.
- **The monitor shepherds.** Quorum's one built-in agent launches queued
  tasks, notices silence (no output, no reports), drops a poke into the
  task's inbox, resumes runs that exited without finishing — a bounded number
  of times — and then marks the task `blocked` and tells you.
- **Guidance is a message, not a keystroke.** Your nudges and the monitor's
  pokes travel the same file-based inbox; the next run starts with them in
  its prompt, and a cooperative harness picks them up mid-run.
- **All state is files** under `QUORUM_HOME` (default `~/.quorum`): task
  records, transcripts, a message board, inboxes — written with atomic
  tmp+rename. The TUI and web dashboard are pure readers and work even when
  the supervisor is down. Copy the directory and your whole setup moves.

## Optional LLM

The monitor works without one (mtime-based stall detection, canned pokes).
Give it an LLM in `[llm]` and it reads transcripts to draft specific,
situation-aware nudges. Any CLI that turns a prompt into text works.

## Optional sandbox

Quorum pairs naturally with [nono](https://github.com/nolabs-ai/nono)
(kernel-enforced sandboxing via Landlock/Seatbelt): wrap the whole thing with
`nono run --profile quorum -- quorum up`, or set `[sandbox] use_nono = true`
to confine each task run to its worktree plus `QUORUM_HOME`. Fails closed:
if sandboxing was requested and nono-py is missing, nothing runs unsandboxed.
See [docs/guide.md](docs/guide.md#sandboxing).

## Customizing

- **Configure** harnesses, stall thresholds, and resume budgets in
  `config.toml` — quorum never rewrites that file.
- **Retune** the task preamble and the monitor's nudges by editing
  `~/.quorum/prompts/*.md`; delete a file to restore the default.
- **Extend** with your own agents: drop a ~20-line Python file into
  `~/.quorum/plugins/` — [examples/steward.py](examples/steward.py) is a
  complete worked example (a rule-based file organizer with undo).

Everything above, in depth: **[docs/guide.md](docs/guide.md)**.
Design record: [docs/architecture.md](docs/architecture.md).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```
