# quorum

**Orchestrate long-running coding tasks with the harness you already use —
supervised by that same harness.** Point quorum at your repos, queue tasks
in plain English, and let your coding agent CLI — `claude`, `codex`,
`opencode`, anything that takes a prompt — do the work in isolated git
worktrees. The supervisor is an agent too: quorum's **manager** periodically
hands your harness a digest of everything happening (every task's status,
output, and liveness, plus its own past actions and their outcomes) and lets
it decide — launch, nudge, relaunch, spin up follow-up work, or escalate to
you. Supervision policy is a prompt you can edit, not code. One process, no
root, no cron, no database; everything is a plain file you can `cat`.

```
quorum task add my-api "add rate limiting to the public endpoints, then open a PR"
        │
┌─ quorum up ─────────────────────────────────────────────────────┐
│  manager    your harness, reading the whole situation and       │
│             acting: launch, nudge, relaunch, task add,          │
│             escalate — every action journaled and auditable     │
│                                                                 │
│  task runs  your harness (claude/codex/opencode/...) working    │
│             in worktrees/<task>/, reporting progress back       │
│             through `quorum task report` and reading guidance   │
│             from `quorum task inbox`                            │
└──────────────────── all state in ~/.quorum ─────────────────────┘
        ▲                    ▲                    ▲
   quorum status        quorum tui           quorum web
   quorum manager tell  (steer with `n`)     (localhost only)
```

## Install

```bash
uv tool install "quorum-orchestrator[web,tui]"   # or: pipx install
uvx quorum-orchestrator --help                   # zero-install trial run
```

The PyPI distribution is `quorum-orchestrator`; the command it installs is
plain `quorum` (and the import name is `quorum` too).

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
start  = ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]
resume = ["claude", "-p", "{prompt}", "--resume", "{session}", "--output-format", "stream-json", "--verbose",
          "--allowedTools", "Edit", "Write", "Read", "Bash(git:*)", "Bash(quorum:*)", "Bash(gh:*)"]
```

(Runs are unattended, so the harness needs permission to act without asking —
the scoped `--allowedTools` list covers editing, git, the PR, and quorum's
progress protocol, without a blanket permission bypass.)

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
quorum manager tell "the api task is urgent; park everything else"
quorum manager journal       # what the manager did, and why
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
- **The manager is an agent, not a ruleset.** Each cycle it compiles a
  situation digest — including its own recent actions and whether they
  changed anything — and your harness decides what to do, with real
  authority: `task run`, `task nudge`, `task add`, `task cancel`, escalate.
  Every action is auto-journaled (`quorum manager journal`); the journal
  feeds back into the next digest so the manager never loops on an
  intervention that isn't working. Steer it with `quorum manager tell`.
- **Guidance is a message, not a keystroke.** Your nudges and the manager's
  pokes travel the same file-based inbox; the next run starts with them in
  its prompt, and a cooperative harness picks them up mid-run.
- **Failure is loud and recovery is automatic.** If your LLM service goes
  down, every harness-driven tick fails visibly — and keeps being scheduled,
  so the first tick after service returns reads the world from files and
  relaunches whatever died. No degraded fallback mode to babysit.
- **All state is files** under `QUORUM_HOME` (default `~/.quorum`): task
  records, transcripts, a message board, inboxes — written with atomic
  tmp+rename. The TUI and web dashboard are pure readers and work even when
  the supervisor is down. Copy the directory and your whole setup moves.

## Supervision policy is a prompt

`~/.quorum/prompts/manager.md` is the manager's constitution: how patient it
is, when it escalates, how it words its pokes, when creating follow-up work
is warranted. Edit it to retune your manager; delete it to restore the
default. (An optional `[llm]` section separately gives *plugin* agents a
small-completion client — the manager and tasks run your full harness
directly.)

## Optional sandbox

Quorum pairs naturally with [nono](https://github.com/nolabs-ai/nono)
(kernel-enforced sandboxing via Landlock/Seatbelt): wrap the whole thing with
`nono run --profile quorum -- quorum up`, or set `[sandbox] use_nono = true`
to confine each task run to its worktree plus `QUORUM_HOME`. Fails closed:
if sandboxing was requested and nono-py is missing, nothing runs unsandboxed.
See [docs/guide.md](docs/guide.md#sandboxing).

## Customizing

- **Configure** harnesses, schedules, and the manager's action budget in
  `config.toml` — quorum never rewrites that file.
- **Retune** the task preamble and the manager's policy by editing
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
