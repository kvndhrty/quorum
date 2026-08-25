# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras            # dev setup (extras: web, nono; the TUI is a core dep)
uv run pytest                   # full suite
uv run pytest tests/test_tasks.py::test_run_creates_worktree_and_streams_transcript
uv run pytest -m "not nono_integration"   # what CI's unit-test matrix runs
uv run pytest -m nono_integration -v      # real kernel sandbox tests (need [nono] + Landlock/Seatbelt)
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

1. **No privileged infrastructure.** `quorum up` is one ordinary foreground process
   hosting an APScheduler `BackgroundScheduler`; task runs are detached child
   processes (they survive supervisor restarts). No cron, systemd, daemonization,
   root, or open ports (the web dashboard is opt-in, localhost-only).
2. **All state is plain files** under `QUORUM_HOME` (resolution: `--home` > `$QUORUM_HOME`
   > `./quorum-home` if present > `~/.quorum`). No database. Adding new durable state
   means adding a file layout, documented in `docs/architecture.md`.
3. **Fail loudly, recover automatically.** Views/dashboards degrade gracefully (pure
   file readers; work with the supervisor stopped) and a harness that ignores the
   report protocol is still observed passively — but supervision has **no no-LLM
   fallback by design**: without a working harness the manager's tick raises every
   time, and its `auto_pause = false` config keeps the schedule firing so it
   self-recovers when the LLM service returns. Do not add degraded supervision paths.

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
- `runner.py` — one harness run: `runner.lock` pid-lock → git worktree under
  `worktrees/<id>` (branch `quorum/<short-id>`) → claim task inbox → compose prompt
  (preamble + task + guidance) → substitute `{prompt}`/`{session}` into the
  `[harness.<name>]` argv template → stream stdout to `transcript.jsonl`, capturing
  `session_id`/`thread_id`. A harness with `inject = "stream-json"` also gets
  mid-run guidance: `GuidancePump` holds stdin open, forwards inbox messages as
  stream-json user turns, and closes stdin at the first idle `result` event (the
  manager reuses the pump over the `manager` inbox). The runner **never sets task
  status**. `launch_detached` spawns `python -m quorum task run` in a new session.
- `agents/manager.py` — the only builtin, and it makes **no decisions in Python**:
  its tick builds a situation digest (`build_digest`, pure over files — task
  statuses, runner liveness, quiet time, report/transcript tails, the manager's own
  action journal with then-vs-now outcomes, user directives from the `manager`
  inbox), renders `prompts/manager.md`, and runs the configured harness
  synchronously (cwd=home, tagged with the `actor.py` env protocol —
  `QUORUM_ACTOR=manager`, per-run `QUORUM_MANAGER_RUN`, the resolved cap in
  `QUORUM_MANAGER_ACTION_CAP` — bounded by `run_timeout_seconds`, stdout →
  `state/manager/transcript.jsonl`).
  The harness acts via the quorum CLI; the CLI's `_manager_guard` auto-journals
  every mutating action to `state/manager/journal.jsonl` and enforces the per-run
  action cap (`max_actions_per_run`) — the only rail, a rate limit, never a veto.
  Failures raise; directives are rejected back to `new/` on crash.
- `agent.py` — `Agent` (synchronous, idempotent `tick()`) plus `AgentContext`, the single
  seam through which agents touch the world: `ctx.bus`, `ctx.projects`, `ctx.llm`,
  `ctx.prompt()`, `ctx.load_state()/save_state()`, `ctx.log_action()`, `ctx.now()`.
  Agents take a clock as a callable — use `ctx.now()`, never `datetime.now()`.
  `tick_lock_path` is held by both the supervisor wrapper and `agent run-once`.
- `supervisor.py` — one scheduler job per enabled agent, wrapped by `run_agent_tick`
  for crash isolation: heartbeat files, an `agent.error` post to the `system` topic,
  auto-pause after `MAX_CONSECUTIVE_FAILURES` (5). A 15s `_control` job claims the
  `supervisor` inbox (`quorum agent pause|resume|run-now`). An hourly janitor archives
  expired board messages and returns crash-orphaned `cur/` claims to `new/`.
- `views.py` — the shared read-model assembled purely from files; `quorum status`, the
  web app, and the TUI are all pure readers of it (the TUI/web's one write affordance
  is nudging a task, via the same bus call as the CLI).
- `actor.py` — the actor-identity env protocol: who a quorum CLI call is acting
  as. The manager tags the harness it spawns (`manager_env`), the CLI resolves
  `current_actor()` for journaling and message attribution, and the runner
  `strip_actor_env`s spawned children so they act as themselves. Also owns the
  manager action journal's path.
- `registry.py` — resolves an agent `type` string: builtin short name (only `manager`),
  else `module:Class` with `QUORUM_HOME/plugins` prepended to `sys.path`.
- `llm/` — `LLMBackend` is a one-method protocol for *plugin agents'* small
  completions — neither task harnesses nor the manager go through it. `LLMClient.complete()`
  **never raises**; `None` means "no LLM today". No module outside `llm/` may assume
  the `cli` backend (`proxy` is a reserved seam).
- `sandbox.py` — the *only* module that imports `nono_py`, always lazily and inside
  functions. It **fails closed**. `build_capabilities` (supervisor/LLM) blocks network
  unless `[llm]` is set; `build_task_capabilities` (per-run) grants the worktree, the
  project's `.git` (shared object store), and `[sandbox].task_read/task_write` extras,
  with network open.
- `config.py` — `config.toml` is user-owned and **quorum never writes it back**; machine
  state goes to JSON. `[harness.<name>]` tables are argv templates; `[tasks]` holds
  worktree/default-harness; `AgentConfig.auto_pause=false` exempts an agent from the
  5-failure auto-pause (the manager uses it). Schedules are validated by regex and
  translated to APScheduler trigger kwargs by `parse_schedule`.
- `projects.py` — `projects/<slug>.json` is canonical, but a `.quorum.toml` marker inside
  the project directory merges over it at read time. Agents and views must go through
  `ProjectRegistry` and must only ever *read* project dirs — task writes happen in
  worktrees.
- `prompts.py` — `QUORUM_HOME/prompts/<name>.md` overrides the packaged
  `default_prompts/` (`task-preamble`, `manager`); deleting a file restores the
  default. `format_map` with a missing-key-preserving dict. Re-running `quorum
  init` upgrades seeded-but-never-edited copies, recognized by hash — **when you
  change a file in `default_prompts/`, append the replaced version's sha256 to
  `home.py::SUPERSEDED_PROMPT_HASHES`** (`git show HEAD:src/quorum/default_prompts/<name> | shasum -a 256`).
- `examples/steward.py` — the one shipped example plugin (file organizer with undo),
  loaded by path in `tests/test_example_steward.py` so the docs' worked example stays
  true. Not a builtin; users copy it into `plugins/`.

### Adding an agent

Builtins live in `agents/__init__.py::BUILTIN_NAMES` (currently just `manager`) —
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
