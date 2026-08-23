# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras            # dev setup (extras: web, tui, nono)
uv run pytest                   # full suite
uv run pytest tests/test_steward.py::test_apply_mode_moves_with_undo_and_collision
uv run pytest -m "not nono_integration"   # what CI's unit-test matrix runs
uv run pytest -m nono_integration -v      # real kernel sandbox tests (need [nono] + Landlock/Seatbelt)
uv run ruff check .             # lint (line-length 100; E4,E7,E9,F,I,UP,B)
uv run quorum <cmd>             # run the CLI from a checkout
```

`tests/test_nono_integration.py` self-skips when nono-py is missing or the platform
lacks Landlock/Seatbelt; a dedicated CI job asserts support so it can never silently
skip there. `test_web.py` needs the `web` extra (it `importorskip`s FastAPI).

## Architecture

Read `docs/architecture.md` first — it is the design record. The three invariants
below govern nearly every change:

1. **No privileged infrastructure.** `quorum up` is one ordinary foreground process
   hosting an APScheduler `BackgroundScheduler`. No cron, systemd, daemonization, root,
   or open ports (the web dashboard is opt-in, localhost-only).
2. **All state is plain files** under `QUORUM_HOME` (resolution: `--home` > `$QUORUM_HOME`
   > `./quorum-home` if present > `~/.quorum`). No database. Adding new durable state
   means adding a file layout, documented in `docs/architecture.md`.
3. **Degrade gracefully.** LLM, dashboards, and sandbox are all optional. Every
   LLM-using agent must have deterministic no-LLM behavior; every view must work with
   the supervisor stopped.

### Layers

- `fsio.py` — the primitives everything else stands on: `atomic_write_*` (dot-prefixed
  tmp in the same dir + fsync + rename), `append_jsonl`, ULID generation, `sorted_entries`
  (skips dotfiles), and a pid-lock built on `O_EXCL` rather than `flock` so it behaves
  identically under any sandbox-granted filesystem. **Never write state with plain
  `open(...,'w')`** — readers must never observe a partial file.
- `messages.py` — one `Message` schema over two channels: an append-only board
  (`messages/board/<topic>/`, filenames `<utc-compact>-<ULID>.json` so lexicographic
  order is chronological and no index exists) and maildir-style inboxes
  (`new/` → `cur/` claimed by `os.rename`, so exactly one claimant wins). Board consumers
  hold their own cursor in private state; the board itself carries no consumption marks.
  Exactly one of `to`/`topic` is set, and `payload.text` always exists so any reader can
  render any message.
- `agent.py` — `Agent` (synchronous, idempotent `tick()`) plus `AgentContext`, the single
  seam through which agents touch the world: `ctx.bus`, `ctx.projects`, `ctx.llm`,
  `ctx.prompt()`, `ctx.load_state()/save_state()`, `ctx.log_action()`, `ctx.now()`.
  Agents take a clock as a callable — use `ctx.now()`, never `datetime.now()`.
- `supervisor.py` — one scheduler job per enabled agent, wrapped by `run_agent_tick`
  for crash isolation: heartbeat files, an `agent.error` post to the `system` topic, and
  auto-pause after `MAX_CONSECUTIVE_FAILURES` (5). An hourly janitor archives expired
  board messages and returns crash-orphaned `cur/` claims to `new/`.
- `views.py` — the shared read-model assembled purely from files; `quorum status`, the
  web app, and the TUI are all pure readers of it.
- `registry.py` — resolves an agent `type` string: builtin short name, else `module:Class`
  with `QUORUM_HOME/plugins` prepended to `sys.path`. Third-party agents need no packaging.
- `llm/` — `LLMBackend` is a one-method protocol. `LLMClient.complete()` **never raises**;
  `None` means "no LLM today". No module outside `llm/` may assume the `cli` backend
  (`proxy` is a reserved seam for a future credential-injecting localhost proxy).
- `sandbox.py` — the *only* module that imports `nono_py`, always lazily and inside
  functions. It **fails closed**: if sandboxing was requested and nono-py is absent,
  LLM subprocesses do not run at all rather than running unsandboxed.
- `config.py` — `config.toml` is user-owned and **quorum never writes it back**; machine
  state goes to JSON. Schedules are validated by regex and translated to APScheduler
  trigger kwargs by `parse_schedule`.
- `projects.py` — `projects/<slug>.json` is canonical, but a `.quorum.toml` marker inside
  the project directory merges over it at read time. `ProjectRegistry` is the single merge
  point; agents and views must go through it, and must only ever *read* project dirs.
- `prompts.py` — `QUORUM_HOME/prompts/<name>.md` overrides the packaged
  `default_prompts/`; deleting a file restores the default. `format_map` with a
  missing-key-preserving dict, so stray braces in data don't explode.

### Adding an agent

Builtins are registered in `agents/__init__.py::BUILTIN_NAMES` (lazy import by dotted
`module.Class`) and usually get a stanza in `home.py::DEFAULT_CONFIG`. `docs/writing-agents.md`
is the user-facing contract and doubles as the checklist: idempotent `tick()`, dedupe
repeat announcements through `load_state()/save_state()`, raising is safe.

### Testing idioms

`tests/conftest.py` provides `home` (scaffolded `QUORUM_HOME` in `tmp_path`, exported via
`$QUORUM_HOME`), `clock` (a `FakeClock` passed as `AgentContext(now=...)`), and `fake_llm`
(argv for `tests/bin/fake_llm.py`, whose behavior is driven by `FAKE_LLM_MODE` /
`FAKE_LLM_OUTPUT`). Agents are tested by constructing an `AgentContext` directly — no
scheduler. `test_sandbox.py` injects a fake `nono_py` module via `sys.modules` so the glue
is pinned down installation-independently; `test_nono_integration.py` exercises the real
kernel enforcement.

Docs are part of the deliverable here: a change to the file layout, message protocol, or
sandbox modes should update `docs/architecture.md` (or `docs/nono.md`) in the same commit.
