# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras            # dev setup (one extra: nono; the TUI is a core dep)
uv run pytest                   # full suite
uv run pytest tests/test_tasks.py::test_run_creates_worktree_and_streams_transcript
uv run pytest -m "not nono_integration"   # what CI's unit-test matrix runs
uv run pytest -m nono_integration -v      # real kernel sandbox tests (need [nono] + Landlock/Seatbelt)
QUORUM_HARNESS_TESTS=1 uv run pytest -m "codex_integration or opencode_integration" -v
                                # real codex/opencode adoption tests (binaries + auth; spend tokens)
uv run ruff check .             # lint (line-length 100; E4,E7,E9,F,I,UP,B)
uv run quorum <cmd>             # run the CLI from a checkout
uv run python scripts/surfaces.py   # count what quorum exposes (see #102)
```

The PyPI distribution is `quorum-orchestrator` (plain `quorum` was taken); the
import name and CLI command stay `quorum`. Releases: bump `version` in
pyproject.toml, tag `vX.Y.Z`, push the tag — `.github/workflows/release.yml`
builds with uv and publishes to PyPI via trusted publishing (OIDC, no token).

`tests/test_nono_integration.py` self-skips when nono-py is missing or the platform
lacks Landlock/Seatbelt; a dedicated CI job asserts support so it can never silently
skip there.

## What quorum is

Bring-your-own-harness orchestration for long-running coding tasks: the user
registers projects, queues tasks in plain English, and a configured harness CLI
(claude / codex / opencode / anything) executes each task as a sequence of *runs*
in a per-task git worktree. Supervision is **itself harness-driven**: the one
built-in agent (the manager) runs the same harness over a situation digest and
acts through the quorum CLI. Quorum ships the manager and the comms substrate
(file-based board + inboxes), **not** prescriptive worker agents, and supervision
policy lives in `prompts/manager.md`, not Python.

Read `docs/architecture.md` first — it is the design record. `docs/guide.md`
is the user-facing manual, and its **Glossary** fixes one name per idea:
guidance (not nudge/directive), escalation, archive, journal, usage log,
transcript, digest, attached, working directory vs worktree, overlay, note,
agent / manager / prompt agent / plugin agent, dial. Use those words in docs,
help text and prompts. The three invariants below govern nearly every change:

1. **No privileged infrastructure.** `quorum up` is one ordinary process hosting an
   APScheduler `BackgroundScheduler` — foreground by default, or detached with
   `up --detach` the same way task runs detach (`quorum down` SIGTERMs it and polls
   the lock). Task runs are detached child processes (they survive supervisor
   restarts). No cron, systemd, daemonization frameworks, root, or open ports —
   quorum has no server and nothing listens.
2. **All state is plain files** under `QUORUM_HOME` (resolution: `--home` > `$QUORUM_HOME`
   > `./quorum-home` if present > `~/.quorum`). No database. Adding new durable state
   means adding a file layout, documented in `docs/architecture.md`.
3. **Fail loudly, recover automatically.** Views degrade gracefully (pure file
   readers; they work with the supervisor stopped) and a harness that ignores the
   report protocol is still observed passively — but supervision has **no
   no-model fallback by design**: without a working harness the manager's tick raises
   every time, and its `auto_pause = false` config keeps the schedule firing so it
   self-recovers when the model service returns. Do not add degraded supervision
   paths.

These are the project's *current* design commitments, not gospel. Quorum is
evolving: any recorded stance — including the big three above and the smaller
ones noted per layer below — is open to deliberate revision when a change is
worth it. Don't contort a feature to fit an old rule; propose breaking the
rule, and when it changes, update this file and `docs/architecture.md` in the
same commit so the record stays true.

Two lists keep the *dials* apart from the *invariants*:
`docs/guide.md#loosening-the-rails-as-trust-is-earned` tables every setting
that records current trust in the model (launch cap, `max_actions_per_run`,
`run_timeout_seconds`, the per-run budget, the stall watchdog, manager
cadence, who launches / decomposes / merges) with its home, default and
loosening condition, facing "What does not move". A dial moves by editing
the value where it lives; an invariant moves only through the process above.
`dials.py` is the registry behind the table and behind doctor's `dial.*`
lines, and `tests/test_dials.py` fails when a numeric `[tasks]`/`[agents]`
option with a default has no row.

### Layers

Each entry says what the module owns and the rules a change must respect.

- `fsio.py` — the primitives everything stands on. `atomic_write_*` (dot-prefixed
  tmp in the same dir + fsync + rename) — **never write state with plain
  `open(...,'w')`**; `read_json_or` / `read_pid` / `agent.read_heartbeat` are the
  fail-soft readers — **never hand-roll a `read_json` try/except**; a pid-lock on
  `O_EXCL`, not `flock`, so it behaves the same under any sandbox. One
  implementation each of the id grammar (`resolve_handle`: full id, then unique
  prefix or suffix) and the window grammar (`parse_window`/`window_start`), shared
  by every command that takes an id or a `--since`.
- `messages.py` — one `Message` schema over two channels: an append-only board
  (`messages/board/<topic>/`, filenames sorting chronologically) and maildir inboxes
  (`new/` → `cur/` claimed by `os.rename`, so exactly one claimant wins). Task
  guidance and the supervisor control channel both ride this; add no new transport.
  Archival is the janitor's per-message path exposed (`archive_board_message`,
  `ack_board_message`, `archive_topic`, `clear_inbox`) — **archive, never a
  read-state flag**, which is what keeps the board free of consumption marks.
  `archived_records` is the one scan of `messages/archive/*.jsonl.gz`.
- `tasks.py` — the task substrate: `Task`/`TaskStore` over `tasks/<id>/task.json`,
  `report()` (the harness's return channel), and the path helpers runner, manager,
  views and CLI share. **Status is a free-form reported string**; only
  `TERMINAL_STATUSES` mean anything. Quorum orders nothing — `TaskStore.list` is
  chronological, no reader sorts, and `--after` (`depends_on`, validated once at
  `add`, read back by the total `dependency_state`) is the only ordering the
  substrate enforces. `workdir_git_state` is the stranded-work probe; `issue_url`,
  `handoff` and `perpetual`/`attached` are written once and read by prompts, views
  and the digest. Not a DAG engine: the manager decides every launch.
- `prune.py` — on-demand cleanup under the bus's rule, **archive never delete**:
  `tasks/<id>/` is *moved* to `tasks/.archive/<id>/`, dot-prefixed so every listing
  skips it and restoring is one `mv`. Total readers (`select`, `refusal`,
  `dependents_first`, `plan`, `worktree_plan`) separate from the two doers. The
  refusals are substrate rails of the runner's class — live runner, attached task,
  a task something still depends on, stranded work (the only `--force`-able one).
  **`--force` never reaches `git worktree remove`**; its two meanings are waiving
  the stranded-work refusal and upgrading `branch -d` to `-D`.
- `export.py` — `quorum task export <id>`: one `.tar.gz` of a task, a pure reader
  that adds no state. The only write is the archive, refused inside the home and
  over an existing file. Nothing from a project directory is exported, so the
  worktree diff is refused loudly for an attached or `--no-worktree` task;
  `redact_transcript` is structural and its failure direction is "dropped".
- `runner.py` — one harness run: `runner.lock` → worktree under `worktrees/<id>`
  (branch `quorum/<short-id>`) → claim the task inbox → compose the prompt
  (preamble + task + dependency note + notebook + guidance) → substitute the
  `[harness.<name>]` argv → stream stdout to `transcript.jsonl`, capturing the
  session id and usage. `inject = "stream-json"` moves the prompt to stdin and the
  `GuidancePump` keeps it open for mid-run guidance. The runner **never sets task
  status**, and refuses an attached task, unfinished dependencies and an
  over-budget last run (`--force` waives the last two) — substrate rails, not
  policy. `[tasks].auto_commit` is a mechanical net that never pushes and never
  sets status.
- `usage.py` — token/cost usage read out of harness result events: loose
  extraction, canonical keys, **fail-soft** (silence records `usage = null` and
  readers omit rather than print `$0.00`). Reduction is elementwise **max** within
  a run (harnesses report run-cumulative totals) and a **sum** across runs. Task
  runs carry it on the `TaskRun`; agent runs append a line to
  `state/<agent>/usage.jsonl` (every run, failures included), read back over a
  bounded tail. The budget gate refuses the *next* run, never the current one.
- `stats.py` — `quorum usage`, the aggregate read across tasks, harnesses, weeks
  and agents. A pure reader with no cache and no network; spend is one
  `usage.total` per group rather than a re-derived reduction, nothing is
  estimated, and `share_merged` is measured over tasks with any `pr_state`,
  never over done tasks, because absence is not "not merged".
- `transcript.py` — the **one** renderer of a transcript and the one place that
  knows how each harness spells an event (`tool_call`, `session_id`, `normalize` —
  `manager.loop_signal` and the runner's session capture call in here). One reader
  per transcript, not a `log`/`tail` pair: `-n` bounds, `-f` follows, `--raw`
  prints the original lines. **Fail-soft is the rule** — an unknown event is its
  raw line — because this runs in dashboard refreshes and `-f` tails.
  `render_run` reads one agent tick out of four files.
- `agents/manager.py` — the flagship builtin, and it makes **no decisions in
  Python**. `build_digest` is pure over files: task status, runner liveness, quiet
  time, report and transcript tails, `loop_signal`, `overlap_signal`, `ci.pr_state`,
  usage, the journal with then-vs-now outcomes, the notebook, and guidance from the
  `manager` inbox. All of those are **observations the manager judges, never
  rails**; their thresholds are commented module constants tuned to prefer false
  negatives. It then renders `prompts/manager.md` and runs the harness
  synchronously, tagged with the `actor.py` env protocol.
- `agents/harness_run.py` / `agents/prompt_agent.py` — the extracted run mechanics
  (`run_agent_harness`) and the generic sibling of the manager (builtin `prompt`):
  renders `prompts/<name>.md` with no digest and no wake condition (conditional
  behaviour belongs in the prompt) under the same journal and cap rails.
  `quorum agent create` writes the two files; the shipped `babysitter` prompt is a
  whole CI-reactive policy as prompt text.
- `agent.py` — `Agent` (synchronous, idempotent `tick()`) plus `AgentContext`, the
  single seam through which agents touch the world: `ctx.bus`, `ctx.projects`,
  `ctx.prompt()`, `ctx.load_state()/save_state()`, `ctx.log_action()`, `ctx.now()`.
  There is no `ctx.llm`: a plugin agent that wants a model call runs a harness
  through `run_agent_harness`. Use `ctx.now()`, never `datetime.now()`.
- `supervisor.py` — one scheduler job per enabled agent, wrapped by `run_agent_tick`
  for crash isolation: heartbeats, an `agent.error` post to `system`, auto-pause
  after `MAX_CONSECUTIVE_FAILURES` (5) unless `auto_pause = false`, in which case a
  sustained streak escalates once to `attention`. A 15s `_control` job claims the
  `supervisor` inbox; `agent.reload` re-reads config and is the hot add/edit/remove
  path. Pause is durable (it lands in the heartbeat). An hourly janitor archives
  expired board messages and returns crash-orphaned `cur/` claims to `new/`.
- `views.py` — the shared read model assembled purely from files; `quorum status`
  and the TUI read it and nothing else, which is why they cannot disagree. It also
  renders a task row's marks once for every surface (`task_marker`, `task_badges`,
  `task_flags`, `usage_badge`), described by `status --legend`: a surface chooses
  where to put them, never how to spell them. `task_detail` is the one assembly of
  a task's whole record — `task show` prints its rows through `detail_line` and
  `--json` dumps them, so text and JSON cannot disagree; `task_self_detail` and
  `agent_detail_rows` are the same rows plus the `self` section a run gets about
  itself, rendered from an `actor.SelfRun` it is handed so this stays a pure file
  reader — and `task_history` is the
  post-hoc reader over every file that records part of a task's life; both are
  bounded, fail-soft and record nothing. Write affordances (TUI `n`, `m`, `s`, `c`, `a`) are thin calls into the
  same code the CLI uses — **never view-local write logic** — and all go through
  `_write`, so an unwritable home notifies instead of taking the dashboard down.
- `cli/` — one module per command group (`task`, `agent`, `manager`, `board`,
  `project`, `prompt`, `integration`, `notify`, and `root` for
  `init/up/down/status/doctor/tui/usage`) over `_common.py`, which holds the typer
  apps, `get_home`, `_actor_guard`, `_resolve_task`, the table builders and every
  shared option object. `cli/__init__.py` re-exports what callers and tests import.
  `--home` is one option on the **root** app and goes before the subcommand.
- `actor.py` — the actor-identity env protocol: who a CLI call is acting as.
  An agent tags the harness it spawns (`actor_env`), the CLI resolves
  `current_actor()` for journalling and message attribution, and the runner strips
  the launcher's tag before setting the task's own (`QUORUM_ACTOR=task-<id>`,
  identity only — no journal, no cap). Owns `journal_path` / `notes_path` /
  `transcript_path` and the two per-run agent defaults. It also owns the **read**
  side of the tag (`show self`): `self_task_id` / `self_agent_name` split it so
  exactly one answers, and `self_run` bundles the run-scoped facts as a `SelfRun`
  for views to render — resolution here, rendering there, and `actions_used` is
  the one count both the cap guard and `show self` read. **Read-only**: nothing
  on this path changes a cap or a budget.
- `notes.py` — the notebook: an agent's or a task's *standing* memory, a separate
  buffer from the journal (a bounded tail of one run) and the board (which anything
  may post to). Append-only `notes.jsonl` plus tombstones; `Notebook.render` gets
  its own budget in the prompt so nothing else can crowd it out, and says what it
  dropped. `may_write` reads `QUORUM_ACTOR`, so it is a **convention against
  accidental crowding, not a security boundary** (the sandbox is) — say so in docs
  rather than overselling it. `agent_notebook` and `task_notebook` are the only
  entry points. No Python summarization: consolidation is prompt policy.
- `registry.py` — resolves an agent `type`: a builtin short name (`manager`,
  `prompt`), else `module:Class` with `QUORUM_HOME/plugins` on `sys.path`.
- `sandbox.py` — the *only* module that imports `nono_py`, always lazily and inside
  functions. It **fails closed**. `build_capabilities` (mode 2) blocks network
  unless `[sandbox].profile_file` grants it; since mode 2 covers every child, a
  harness-driven manager under it needs that grant. `build_task_capabilities`
  (per-run) grants the worktree, the project's `.git` and the configured extras,
  with network open.
- `herdr.py` — the *only* module that talks to a herdr server. **Fails soft**, the
  deliberate opposite of sandbox.py: herdr absent or broken degrades every call to
  `None`/`False` rather than breaking a digest or a nudge. Two uses, both for
  attached tasks with a `herdr_pane`: pane status in the digest, and a doorbell on
  `task nudge`. The inbox stays the only transport; the doorbell carries no payload.
- `notify.py` — the `[notify]` hook, the one board consumer quorum ships for a
  person. `drain` reads each listed topic past a private cursor in
  `state/notify.json` and runs the argv template per message. The cursor is
  persisted **before** each delivery: **at-most-once** on purpose. Fail-soft in
  herdr's mold; the first drain arms at the tail without delivering. It fires on
  topic membership, never on content — no policy here. `quorum notify test` is the
  loud path.
- `forge.py` — the *only* module that shells out to a forge CLI (`gh` today), and
  where two opposite contracts meet: `run_json` and `auth_status` are soft (any
  failure is `None`), `issue_view` is **loud** (`task add --issue` runs in front of
  a person, so every failure raises `ForgeError` naming the fix). One `_invoke`, so
  the unattended-invocation details are stated once; `cli_name(home)` is the single
  seam a second backend lands on. **No write path**: quorum reads a forge and never
  labels, comments on or closes anything.
- `ci.py` — the digest-facing half over `forge.py`, and the second fail-soft probe:
  `pr_state` runs one `gh pr view` inside the task's working directory and returns
  `None` for every disappointment, so a digest always builds and a missing `ci:`
  line means nothing. Only `build_digest` calls it, which is what keeps `views.py`
  a pure file reader. Exactly **one** probe result is materialized —
  `tasks.record_pr_state` writes `pr_state`/`pr_state_at` so views can badge a
  merged PR without a network call — and the five properties fencing that exception
  are in `docs/architecture.md`; a second exception must earn all five again. What
  to *do* about red CI lives in the prompts.
- `dials.py` — the trust-dial registry: `DIALS` (key, label, where it lives,
  default, a reader for the current value), `numeric_options` over a pydantic model
  (annotation-checked, so a `bool` switch is not a dial) and `NUMERIC_AGENT_SETTINGS`
  for the per-agent settings the model cannot enumerate. Reads config, decides
  nothing.
- `doctor.py` — the one place that looks at everything that fails soft. One small
  function per check returning `Check(name, status, summary, fix)` in three states
  (`ok`/`problem`/`na`, only `problem` exits non-zero), so each has a passing and a
  failing test. It **diagnoses and never repairs**: every ✗ names its fix and there
  is no `--fix`. A pure reader apart from the opt-in `--smoke` probe, which runs a
  harness for real through the runner's own argv/pump/transcript code, in a scratch
  directory with a scratch `QUORUM_HOME`. It borrows rather than duplicates
  (`forge.auth_status`, `home.classify_prompt`, `sandbox.availability`,
  `dials.current`). A ✗ is reserved for something actually wrong, so
  `quorum init && quorum doctor` exits 0.
- `config.py` — one place to load config: `load_config` raises, `try_load_config`
  returns defaults for a *missing* file and `None` for a malformed one (which is
  what the fail-soft probes read, so an unreadable config means their feature is
  **off**, never fail-open), and `load_config_or_default` fills in defaults for the
  CLI, views and digest. `config.toml` is user-owned and **quorum never writes it
  back**; the one config location quorum may write is `agents/<name>.toml`.
  Schedules are validated by regex and translated by `parse_schedule`.
- `projects.py` — `projects/<slug>.json` is canonical, but a `.quorum.toml` marker
  inside the project directory merges over it at read time. Agents and views go
  through `ProjectRegistry` and may only ever *read* project directories; task
  writes happen in the worktree.
- `prompts.py` — four layers, resolved in order: packaged `default_prompts/` → home
  copy `prompts/<name>.md` → home overlay `prompts/<name>.local.md` at `{local}` →
  project overlay (registry notes, then `<project>/.quorum/task-preamble.local.md`)
  at `{project}`. `render` is `format_map` with a missing-key-preserving dict.
  Reading a template stays **loud**; reading an overlay is **fail-soft**, because
  `render` is on every tick and every run. An empty slot leaves no hole; a missing
  `{local}` slot prepends, a missing `{project}` slot deliberately does not.
  `quorum init` upgrades a copy whose sha256 still matches `prompts/.seeded.json`,
  so changing a packaged default needs no bookkeeping in `home.py`.
- `examples/steward.py` — the one shipped example plugin (a file organizer with
  undo), loaded by path in `tests/test_example_steward.py` so the worked example in
  the guide stays true. Not a builtin; users copy it into `plugins/`.

### Adding an agent

Builtins live in `agents/__init__.py::BUILTIN_NAMES` (`manager`, `prompt`) —
prefer plugins unless quorum itself needs the behavior. The user-facing contract is
`docs/guide.md#writing-your-own-agents`: idempotent `tick()`, dedupe repeat
announcements through `load_state()/save_state()`, raising is safe.

### Testing idioms

`tests/conftest.py` is where a shared fixture or helper belongs — test modules import
from it by name (`from conftest import make_repo`), so a second copy of one of these
in a test file is a bug to fix rather than a style choice. Fixtures: `home` (scaffolded
`QUORUM_HOME` in `tmp_path`, exported via `$QUORUM_HOME`), `clock` (a `FakeClock` passed
as `AgentContext(now=...)`), `path_without_gh` (a PATH holding only real git, so a fake
`gh` installed by `install_gh` is provably the one under test) and `tui` (mounts the
Textual dashboard and awaits `script(app, pilot)` against it; several scripts in one
call share the app, so a test that would otherwise mount twice pays the startup once).
Helpers: `make_repo` (a git repo with one commit and a committer identity in its own
config, which committing inside a worktree needs), `repo_git` / `git_out` (loud and
quiet git in a repo), and `harness_table` / `harness_config` (the `[harness.*]` TOML for
the fake harness, and a whole config.toml defaulting to it).
`tests/bin/fake_harness.py` is a fake coding harness (echoes argv/prompt, emits a
`session_id`; `report` mode calls `python -m quorum task report`; `manager_act` /
`manager_flood` modes act like a manager — each `[harness.*]` table pins its mode via
its `env` field, so a fake task harness and a fake manager harness coexist). Runner and
manager tests build real git repos and run the loop for real; when a test needs a
"live" runner, its lock holds pid 1 — never `os.getpid()`, which same-process lock
takeover treats as stale.
`test_sandbox.py` injects a fake `nono_py` via `sys.modules`; `test_nono_integration.py`
exercises real kernel enforcement.

Two rules about what a test is allowed to depend on. Prose in `docs/` and in the
packaged prompts is **not** a contract: assert the marker, the command or the
placeholder a rule teaches (`test_manager.rule_mentioning` finds a numbered rule by its
list-item boundary, not by its number; `test_cli` checks that every `quorum <verb>` in a
packaged prompt names a real command), and keep exact wording only where a user greps
for it, such as a CLI error message. And a family of tests that differ only in their
inputs — a config in, a status and a substring out — is one `@pytest.mark.parametrize`
with `pytest.param(..., id=...)` per case; tests whose setups differ materially stay
apart.

Docs are part of the deliverable here: a change to the file layout, message protocol,
task/run lifecycle, or sandbox modes should update `docs/architecture.md` (and the
user-facing `docs/guide.md`) in the same commit. A change that adds or renames a
user-facing noun updates the guide's glossary too, so the vocabulary stays single.
