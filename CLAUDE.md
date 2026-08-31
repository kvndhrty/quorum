# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras            # dev setup (extras: web, nono; the TUI is a core dep)
uv run pytest                   # full suite
uv run pytest tests/test_tasks.py::test_run_creates_worktree_and_streams_transcript
uv run pytest -m "not nono_integration"   # what CI's unit-test matrix runs
uv run pytest -m nono_integration -v      # real kernel sandbox tests (need [nono] + Landlock/Seatbelt)
QUORUM_HARNESS_TESTS=1 uv run pytest -m "codex_integration or opencode_integration" -v
                                # real codex/opencode adoption tests (binaries + auth; spend tokens)
uv run ruff check .             # lint (line-length 100; E4,E7,E9,F,I,UP,B)
uv run quorum <cmd>             # run the CLI from a checkout
```

The PyPI distribution is `quorum-orchestrator` (plain `quorum` was taken); the
import name and CLI command stay `quorum`. Releases: bump `version` in
pyproject.toml, tag `vX.Y.Z`, push the tag — `.github/workflows/release.yml`
builds with uv and publishes to PyPI via trusted publishing (OIDC, no token).

`tests/test_nono_integration.py` self-skips when nono-py is missing or the platform
lacks Landlock/Seatbelt; a dedicated CI job asserts support so it can never silently
skip there. `test_web.py` needs the `web` extra (it `importorskip`s FastAPI).

## What quorum is

Bring-your-own-harness orchestration for long-running coding tasks: the user
registers projects, queues tasks in plain English, and a configured harness CLI
(claude / codex / opencode / anything) executes each task as a sequence of *runs*
in a per-task git worktree. Supervision is **itself harness-driven**: the one
built-in agent (the manager) runs the same harness over a situation digest and
acts through the quorum CLI. Quorum ships the manager and the comms substrate
(file-based board + inboxes), **not** prescriptive worker agents, and supervision
policy lives in `prompts/manager.md`, not Python.

Read `docs/architecture.md` first — it is the design record. The three invariants
below govern nearly every change:

1. **No privileged infrastructure.** `quorum up` is one ordinary process hosting an
   APScheduler `BackgroundScheduler` — foreground by default, or detached with
   `up --detach` the same way task runs detach (`quorum down` SIGTERMs it and polls
   the lock). Task runs are detached child processes (they survive supervisor
   restarts). No cron, systemd, daemonization frameworks, root, or open ports (the
   web dashboard is opt-in, localhost-only).
2. **All state is plain files** under `QUORUM_HOME` (resolution: `--home` > `$QUORUM_HOME`
   > `./quorum-home` if present > `~/.quorum`). No database. Adding new durable state
   means adding a file layout, documented in `docs/architecture.md`.
3. **Fail loudly, recover automatically.** Views/dashboards degrade gracefully (pure
   file readers; work with the supervisor stopped) and a harness that ignores the
   report protocol is still observed passively — but supervision has **no no-LLM
   fallback by design**: without a working harness the manager's tick raises every
   time, and its `auto_pause = false` config keeps the schedule firing so it
   self-recovers when the LLM service returns. Do not add degraded supervision paths.

These are the project's *current* design commitments, not gospel. Quorum is
evolving: any recorded stance — including the big three above and smaller ones
noted per-layer below (e.g. the TUI/web being pure readers) — is open to
deliberate revision when a change is worth it. Don't contort a feature to fit
an old rule; propose breaking the rule, and when it changes, update this file
and `docs/architecture.md` in the same commit so the record stays true.

### Layers

- `fsio.py` — the primitives everything else stands on: `atomic_write_*` (dot-prefixed
  tmp in the same dir + fsync + rename), `append_jsonl`, ULID generation, `sorted_entries`
  (skips dotfiles), and a pid-lock built on `O_EXCL` rather than `flock` so it behaves
  identically under any sandbox-granted filesystem. **Never write state with plain
  `open(...,'w')`** — readers must never observe a partial file.
- `messages.py` — one `Message` schema over two channels: an append-only board
  (`messages/board/<topic>/`, filenames `<utc-compact>-<ULID>.json` so lexicographic
  order is chronological) and maildir-style inboxes (`new/` → `cur/` claimed by
  `os.rename`, so exactly one claimant wins). Task guidance (`task-<id>` inboxes) and
  the supervisor control channel both ride this; no new transports.
- `tasks.py` — the task substrate: `Task`/`TaskStore` over `tasks/<id>/task.json`,
  `report()` (the harness's return channel), path helpers shared by runner/manager/
  views/CLI. **Status is a free-form reported string**; only `TERMINAL_STATUSES`
  (`done`/`blocked`/`cancelled`) mean anything to quorum. `short_id` is the ULID's
  random *tail* (the head is a same-instant-shared timestamp); `resolve()` accepts
  unique prefixes or suffixes. `workdir_git_state` is the stranded-work probe
  (dirty/unpushed in a task's workdir) surfaced by views and the manager digest —
  the preamble tells harnesses to commit+push with plain git before reporting done.
  `attached = true` marks an *adopted* live interactive session (`quorum task
  adopt`): workdir = the user's checkout, no worktree, liveness from
  `tasks/<id>/attached.json` (rewritten by `task hook-session-start`/
  `hook-stop`/`hook-session-end`, which also learn the session id by cwd
  match and deliver pending inbox guidance — as the Stop-hook block-protocol
  JSON, or bare text via `hook-stop --format text` for shims that inject the
  continuation themselves); `task detach` reverts it. One adapter per
  harness under `integrations/` (claude-code, codex, opencode — the last is
  a fail-soft JS plugin, kept a dumb pipe over the same CLI entry points),
  kept true by `tests/test_integrations.py` (the opencode plugin is driven
  for real under node, skipped when node is absent). The adapters ship
  inside the wheel (hatch force-include → `quorum/integrations`) so
  `quorum integration list|install` works from a package install;
  `cli._integrations_root()` falls back to the repo dir in a checkout.
- `runner.py` — one harness run: `runner.lock` pid-lock → git worktree under
  `worktrees/<id>` (branch `quorum/<short-id>`) → claim task inbox → compose prompt
  (preamble + task + guidance) → substitute `{prompt}`/`{session}` into the
  `[harness.<name>]` argv template → stream stdout to `transcript.jsonl`, capturing
  `session_id`/`thread_id`. A harness with `inject = "stream-json"` gets its
  prompt over stdin instead of argv (stream-json CLIs ignore an argv prompt, so
  `{prompt}` is dropped from its argv): `GuidancePump` holds stdin open, writes
  the prompt as the opening user turn, forwards inbox messages as further
  turns, and closes stdin at the first idle `result` event (the
  manager reuses the pump over the `manager` inbox). With `[tasks].auto_commit`
  (default off) a dirty worktree is committed to the task branch after the harness
  exits — `auto_commit_workdir` + `_maybe_auto_commit`, a mechanical safety net
  that never pushes, never sets status, never fires outside a task's own
  (resolved) worktree or on a terminal-status task, refuses detached-HEAD and
  mid-merge trees, bypasses hooks/signing, skips (with a note) under the nono
  sandbox, and records what it did on the `TaskRun` (`auto_commit`) and in the
  transcript instead of raising. The runner **never sets task
  status**, and refuses attached tasks outright — a substrate rail (same class as
  `runner.lock`, a deliberate narrow bend of "the cap is the only rail") protecting
  the user's live checkout. `launch_detached` spawns `python -m quorum task run`
  in a new session.
- `agents/manager.py` — the flagship builtin, and it makes **no decisions in Python**:
  its tick builds a situation digest (`build_digest`, pure over files — task
  statuses, runner liveness, quiet time, report/transcript tails, a
  `possible-loop` flag from `loop_signal` — a repetition read over the current
  run's tool calls (live runners only, deduped by call id, JSON-event
  harnesses only), plus a `ci:` line from `ci.pr_state` — both
  **observations the manager judges, never rails**;
  thresholds are commented constants, tuned to prefer false negatives
  — the manager's own
  action journal with then-vs-now outcomes, user directives from the `manager`
  inbox), renders `prompts/manager.md`, and runs the configured harness
  synchronously (cwd=home, tagged with the `actor.py` env protocol —
  `QUORUM_ACTOR=manager`, per-run `QUORUM_ACTOR_RUN`, the resolved cap in
  `QUORUM_ACTOR_CAP` — bounded by `run_timeout_seconds`, stdout →
  `state/manager/transcript.jsonl`).
  The harness acts via the quorum CLI; the CLI's `_actor_guard` auto-journals
  every mutating action to the acting agent's journal and enforces the per-run
  action cap (`max_actions_per_run`) — the only rail, a rate limit, never a veto.
  Failures raise; directives are rejected back to `new/` on crash.
  `agents/harness_run.py` holds the extracted run mechanics (`run_agent_harness`)
  shared with `agents/prompt_agent.py::PromptAgent` (builtin `prompt`) — the
  generic sibling: renders `prompts/<name>.md` (no digest, no wake condition;
  conditional behavior belongs in the prompt) and runs the harness with the
  same journal/cap rails under `state/agents/<name>/`. Prompt agents are
  usually file-defined and created by `quorum agent create` or the web form
  (`agent create` accepts no prompt text when the template already resolves,
  and `--prompt <name>` reuses one — how the shipped `babysitter` example, a
  whole CI-reactive policy written as prompt text, is put to work).
- `agent.py` — `Agent` (synchronous, idempotent `tick()`) plus `AgentContext`, the single
  seam through which agents touch the world: `ctx.bus`, `ctx.projects`, `ctx.llm`,
  `ctx.prompt()`, `ctx.load_state()/save_state()`, `ctx.log_action()`, `ctx.now()`.
  Agents take a clock as a callable — use `ctx.now()`, never `datetime.now()`.
  `tick_lock_path` is held by both the supervisor wrapper and `agent run-once`.
- `supervisor.py` — one scheduler job per enabled agent, wrapped by `run_agent_tick`
  for crash isolation: heartbeat files, an `agent.error` post to the `system` topic,
  auto-pause after `MAX_CONSECUTIVE_FAILURES` (5). A 15s `_control` job claims the
  `supervisor` inbox (`quorum agent pause|resume|run-now|reload`); `agent.reload`
  is the hot-add/edit/remove path for file-defined agents (re-reads config, one
  message for all mutations — handled *before* the job-exists guard). Pause is
  durable: `_schedule_agent` creates the job paused when the heartbeat says
  `paused`. An hourly janitor archives expired board messages and returns
  crash-orphaned `cur/` claims to `new/`.
- `views.py` — the shared read-model assembled purely from files (`overview` includes
  `attention_summary`, a time-windowed read of the `attention` topic that `status`,
  the TUI banner, and the web header all surface — the board has no read-state, so
  "needs a look" is time-bounded, not tracked); `quorum status`, the
  web app, and the TUI are all pure readers of it. `agent_rows` estimates a stale
  `next_run` from the schedule (`next_run_estimated`); `agent_detail` adds journal +
  per-agent actions. Write affordances stay thin bus/config calls shared with the
  CLI: nudging a task (TUI+web), and in the web only, board posts, project edits,
  agent create (via `config.create_agent`) and pause/resume/run-now/reload.
- `actor.py` — the actor-identity env protocol: who a quorum CLI call is acting
  as, name-generic over harness-driven agents. An agent tags the harness it
  spawns (`actor_env(name, run_id, cap)`), the CLI resolves `current_actor()`
  for journaling and message attribution, and the runner `strip_actor_env`s
  spawned children so they act as themselves. Also owns `journal_path`/
  `transcript_path` (manager at `state/manager/`, others at `state/agents/<name>/`).
- `registry.py` — resolves an agent `type` string: builtin short name (`manager`,
  `prompt`), else `module:Class` with `QUORUM_HOME/plugins` prepended to `sys.path`.
- `llm/` — `LLMBackend` is a one-method protocol for *plugin agents'* small
  completions — neither task harnesses nor the manager go through it. `LLMClient.complete()`
  **never raises**; `None` means "no LLM today". No module outside `llm/` may assume
  the `cli` backend (`proxy` is a reserved seam).
- `sandbox.py` — the *only* module that imports `nono_py`, always lazily and inside
  functions. It **fails closed**. `build_capabilities` (supervisor/LLM) blocks network
  unless `[llm]` is set; `build_task_capabilities` (per-run) grants the worktree, the
  project's `.git` (shared object store), and `[sandbox].task_read/task_write` extras,
  with network open.
- `herdr.py` — the *only* module that talks to a herdr server (terminal
  multiplexer with agent-aware panes), over its unix-socket newline-JSON API.
  **Fails soft** — the deliberate opposite of sandbox.py's fail-closed: herdr
  absent/broken degrades every call to `None`/`False`, never breaking a digest
  or nudge. Two narrow uses, both for attached tasks with a `herdr_pane`:
  `agent_state` (pane status into the digest) and `ring_doorbell` (a
  `task nudge` pokes the pane that guidance is waiting — the payload stays in
  the maildir inbox; herdr is a doorbell, never a second transport). Optional
  `[herdr]` table (`socket` override, `enabled`).
- `ci.py` — the *only* module that shells out to `gh`, and the second fail-soft
  probe (herdr's mold, not sandbox.py's): `pr_state(home, task)` runs one
  `gh pr view --json ...` *inside* the task's workdir (gh resolves repo from the
  remote, PR from the checked-out branch) and returns state / check counts /
  failing check names / merge conflict — or `None` for every disappointment
  (disabled, no gh, no auth, no remote, no PR, timeout, garbage), so a digest
  always builds and a missing `ci:` line means nothing. Only `build_digest`
  calls it (a `ci:` line per task, `CI-FAILING` on a finished task over red
  checks, bounded by `manager.CI_MAX_PROBES` since digest build blocks the
  tick), which is what keeps `views.py` a pure file reader — do not
  materialize probe results to disk to feed a view without revisiting that.
  What to *do* about red CI lives in `prompts/manager.md` and the shipped
  `prompts/babysitter.md`, never here. Optional `[ci]` table (`enabled`,
  `timeout_seconds`).
- `config.py` — `config.toml` is user-owned and **quorum never writes it back**; machine
  state goes to JSON. The one config location quorum may write is `agents/<name>.toml`
  (file-defined agents, atomic whole-file writes via `write_agent_file`/`create_agent`;
  merged over `[agents.*]` at load, file wins; names validated + reserved-checked).
  `[harness.<name>]` tables are argv templates; `[tasks]` holds
  worktree/default-harness/auto-commit; `AgentConfig.auto_pause=false` exempts an agent from the
  5-failure auto-pause (the manager uses it). Schedules are validated by regex and
  translated to APScheduler trigger kwargs by `parse_schedule`.
- `projects.py` — `projects/<slug>.json` is canonical, but a `.quorum.toml` marker inside
  the project directory merges over it at read time. Agents and views must go through
  `ProjectRegistry` and must only ever *read* project dirs — task writes happen in
  worktrees.
- `prompts.py` — `QUORUM_HOME/prompts/<name>.md` overrides the packaged
  `default_prompts/` (`task-preamble`, `manager`, `babysitter`); deleting a file restores the
  default. `format_map` with a missing-key-preserving dict. Re-running `quorum
  init` upgrades seeded-but-never-edited copies, recognized by hash — **when you
  change a file in `default_prompts/`, append the replaced version's sha256 to
  `home.py::SUPERSEDED_PROMPT_HASHES`** (`git show HEAD:src/quorum/default_prompts/<name> | shasum -a 256`).
- `examples/steward.py` — the one shipped example plugin (file organizer with undo),
  loaded by path in `tests/test_example_steward.py` so the docs' worked example stays
  true. Not a builtin; users copy it into `plugins/`.

### Adding an agent

Builtins live in `agents/__init__.py::BUILTIN_NAMES` (`manager`, `prompt`) —
prefer plugins unless quorum itself needs the behavior. The user-facing contract is
`docs/guide.md#writing-your-own-agents`: idempotent `tick()`, dedupe repeat
announcements through `load_state()/save_state()`, raising is safe.

### Testing idioms

`tests/conftest.py` provides `home` (scaffolded `QUORUM_HOME` in `tmp_path`, exported via
`$QUORUM_HOME`), `clock` (a `FakeClock` passed as `AgentContext(now=...)`), and `fake_llm`.
`tests/bin/fake_harness.py` is a fake coding harness (echoes argv/prompt, emits a
`session_id`; `report` mode calls `python -m quorum task report`; `manager_act` /
`manager_flood` modes act like a manager — each `[harness.*]` table pins its mode via
its `env` field, so a fake task harness and a fake manager harness coexist). Runner and
manager tests build real git repos and run the loop for real; when a test needs a
"live" runner, its lock holds pid 1 — never `os.getpid()`, which same-process lock
takeover treats as stale.
`test_sandbox.py` injects a fake `nono_py` via `sys.modules`; `test_nono_integration.py`
exercises real kernel enforcement.

Docs are part of the deliverable here: a change to the file layout, message protocol,
task/run lifecycle, or sandbox modes should update `docs/architecture.md` (and the
user-facing `docs/guide.md`) in the same commit.
