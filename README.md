# quorum

**Orchestrate long-running coding tasks with the coding agent you already
use — cross-harness, local-first, policy-owned, and supervised by the same
harness that does the work.** Point
quorum at your repos, queue tasks in plain English, and let your coding
agent CLI — `claude`, `codex`, `opencode`, anything that takes a prompt —
do the work in isolated git worktrees. The supervisor is an agent too:
quorum's **manager** periodically hands your harness a digest of everything
happening (every task's status, output, and liveness, plus its own past
actions and their outcomes) and lets it decide — launch, nudge, relaunch,
spin up follow-up work, or escalate to you. Supervision policy is a prompt
you can edit, not code. One process, no root, no cron, no database;
everything is a plain file you can `cat`.

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

Needs Python 3.11+.

```bash
uv tool install quorum-orchestrator              # includes the TUI dashboard
uv tool install "quorum-orchestrator[web]"       # + the localhost web dashboard
uvx --from quorum-orchestrator quorum --help     # zero-install trial run
```

(or `pip install "quorum-orchestrator[web]"` if you don't use uv.)

The PyPI distribution is `quorum-orchestrator`; the command it installs is
plain `quorum` (and the import name is `quorum` too).

From a checkout: `uv sync --all-extras`, then prefix commands with `uv run`.

## Five minutes to a running task

```bash
quorum init                             # scaffold ~/.quorum
```

Tell quorum how to invoke your harness: the scaffolded
`~/.quorum/config.toml` ships ready-to-uncomment blocks for claude, codex,
and opencode, plus a template for any custom agentic binary — uncomment one
and set `default_harness`. (Runs are unattended, so the harness needs
permission to act without asking; the shipped blocks use a scoped tool
allowlist, not a blanket bypass.)

Register a repo and queue work:

```bash
quorum doctor                           # verify the setup end to end
quorum project add ~/work/my-api
quorum task add my-api "fix the flaky auth tests and open a PR"
quorum up --detach                      # supervisor in the background (`quorum down` stops it)
```

(`quorum up` without `--detach` runs it in the foreground — Ctrl-C stops.)
Then:

```bash
quorum status                # supervisor, agents, tasks, deadlines
quorum task tail a3f2k9 -f   # live harness transcript
quorum task nudge a3f2k9 "prefer the retry approach over sleeps"
quorum manager tell "the api task is urgent; park everything else"
quorum manager journal       # what the manager did, and why
quorum tui                   # dashboard; select a task, press n to steer
quorum web                   # http://127.0.0.1:8787
```

## What it looks like

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/kvndhrty/quorum/main/docs/images/web-dark.png">
  <img alt="quorum web dashboard" src="https://raw.githubusercontent.com/kvndhrty/quorum/main/docs/images/web-light.png">
</picture>

![quorum terminal dashboard](https://raw.githubusercontent.com/kvndhrty/quorum/main/docs/images/tui.png)

## What's genuinely different

Plenty of tools run coding agents in parallel. Two things here had no
equivalent in a 2026-08 survey of ~30 orchestration projects (agent
frameworks, coding-agent orchestrators, harness-native orchestration, and
the research literature). That's a snapshot of a landscape that moves
monthly, not a permanent claim — if you know of prior art,
[open an issue](https://github.com/kvndhrty/quorum/issues) and this section
gets corrected.

- **Your live session becomes a supervised task.** `quorum task adopt`
  inverts the usual ownership: instead of quorum spawning an agent, the
  interactive session you are already sitting in is recorded as a task the
  supervisor can watch and guide (mechanics in
  [Adopt a live session](#adopt-a-live-session) below). The closest
  neighbor surveyed (Omnara) relays a session to your phone so *you* can
  steer it; none of the surveyed tools hand the session to a supervisor.
- **The supervisor is the same harness, reading a file digest.** Among the
  open-source tools surveyed, "supervision" meant keystroke automation —
  daemons pressing enter, blind auto-confirmation. An actual LLM supervisor
  showed up only in hosted commercial products (Factory's Mission Control,
  Devin's coordinator), where the inputs and the decisions stay in someone
  else's cloud. Quorum runs that pattern on your disk, and every input and
  decision is a file you can open: the task records and transcripts the
  digest is computed from, the policy that interprets it
  (`~/.quorum/prompts/manager.md`), and the journal of what it did and why
  (`quorum manager journal`).
  → [The manager](https://github.com/kvndhrty/quorum/blob/main/docs/guide.md#the-manager),
  [design notes](https://github.com/kvndhrty/quorum/blob/main/docs/architecture.md#the-manager).

Why run an external orchestration layer at all, when (as of that same
2026-08 survey) every major harness ships native subagents and vendor-cloud
background runs? Because the same wave made sessions externally
addressable — hooks, streaming protocols, control-plane APIs — and an
outside layer can still own what a single vendor's cloud cannot: one queue
over every harness, on your own disk, under a supervision policy you edit.
No daemonization framework, no database, and no open ports beyond the
opt-in localhost-only dashboard. The survey's ranked implications became
the project roadmap:
[issue #23](https://github.com/kvndhrty/quorum/issues/23).

## Adopt a live session

The work is already underway in an interactive session? Don't re-queue it —
adopt it:

```bash
quorum integration install codex     # once per harness (also: opencode, claude-code)
quorum task adopt "refactoring the auth flow"    # from the session's directory
```

The session becomes an *attached* task: the manager observes it (liveness,
git state, reports) but never runs it — your nudges and the manager's pokes
are delivered *inside* the live session by the harness's hook the next time
it stops. `quorum task detach` hands it back to the headless runner. See
[docs/guide.md#adopting-a-live-session](https://github.com/kvndhrty/quorum/blob/main/docs/guide.md#adopting-a-live-session)
and the per-harness adapters under
[integrations/](https://github.com/kvndhrty/quorum/tree/main/integrations).

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
See [docs/guide.md](https://github.com/kvndhrty/quorum/blob/main/docs/guide.md#sandboxing).

## Customizing

- **Configure** harnesses, schedules, and the manager's action budget in
  `config.toml` — quorum never rewrites that file.
- **Retune** the task preamble and the manager's policy by editing
  `~/.quorum/prompts/*.md`; delete a file to restore the default.
- **Add** a prompt-driven agent — a prompt, a schedule, your harness, no
  Python. `quorum agent create babysitter --schedule "every 10m"` starts the
  shipped CI babysitter: it watches your tasks' pull requests with `gh` and
  relaunches the ones whose checks went red, giving up to you after two
  tries. That whole policy is `~/.quorum/prompts/babysitter.md` — yours to
  edit.
- **Extend** with your own agents: drop a ~20-line Python file into
  `~/.quorum/plugins/` — [examples/steward.py](https://github.com/kvndhrty/quorum/blob/main/examples/steward.py) is a
  complete worked example (a rule-based file organizer with undo).

Everything above, in depth: **[docs/guide.md](https://github.com/kvndhrty/quorum/blob/main/docs/guide.md)**.
Design record: [docs/architecture.md](https://github.com/kvndhrty/quorum/blob/main/docs/architecture.md).

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Repo conventions and the layer-by-layer map live in
[CLAUDE.md](https://github.com/kvndhrty/quorum/blob/main/CLAUDE.md).
