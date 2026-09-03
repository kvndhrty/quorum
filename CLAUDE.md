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
noted per-layer below (e.g. the TUI/web reading files and writing only through
thin shared bus calls) — is open to deliberate revision when a change is worth
it. Don't contort a feature to fit an old rule; propose breaking the rule, and
when it changes, update this file and `docs/architecture.md` in the same commit
so the record stays true.

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
  On-demand archival is the janitor's per-message path exposed
  (`archive_board_message`), with `ack_board_message` (one message, resolved
  from a full id / unique prefix / `short_id` suffix — `TaskStore.resolve`'s
  grammar, raising the same `KeyError`/`ValueError`), `archive_topic` and
  `clear_inbox` on top. Acking is **archival, never a flag**: that is what
  keeps the board free of read-state while still letting a handled
  escalation leave `views.attention_summary`'s seven-day window.
- `tasks.py` — the task substrate: `Task`/`TaskStore` over `tasks/<id>/task.json`,
  `report()` (the harness's return channel), path helpers shared by runner/manager/
  views/CLI. **Status is a free-form reported string**; only `TERMINAL_STATUSES`
  (`done`/`blocked`/`cancelled`) mean anything to quorum. `short_id` is the ULID's
  random *tail* (the head is a same-instant-shared timestamp); `resolve()` accepts
  unique prefixes or suffixes. `workdir_git_state` is the stranded-work probe
  (dirty/unpushed in a task's workdir) surfaced by views and the manager digest —
  the preamble tells harnesses to commit+push with plain git before reporting done.
  `issue_url` (`task add --issue <number|url>`) is where a task came from:
  written once at `add` from what the forge reported (never re-probed, never
  written back), rendered by the total `issue_ref` as `#62` for the digest
  and every view, full url in `task show`, and injected into the run
  preamble's `{issue}` slot. Fetching it is `forge.issue_view` and it fails
  **loud**, not soft — see `forge.py`.
  `depends_on` (`task add --after <id>`, repeatable) lists full ids a task
  must not start before — validated once at `add` (`resolve_dependencies`:
  unknown/ambiguous/self rejected, and a perpetual upstream refused because it
  never finishes) and read back by the total, pure `dependency_state`
  (waiting/failed/missing/cycle) that the digest, views and runner share. Only
  a dependency that still *might* finish blocks: `failed` and `missing` are
  both unsatisfiable upstreams, so both are reported and neither is waited on
  — a task that silently never runs would hide the decision. Not a DAG engine:
  the manager still decides every launch.
  `priority` (`task add --priority N`, `task set-priority`) and `held`
  (`task hold` / `task release`) are the user's two hands on the queue and
  neither is a scheduler: priority is an int the digest renders (only when
  non-zero) and `prompts/manager.md` reads as an ordering preference —
  **nothing in Python sorts by it** — while `held` is a parking brake that
  is *not* a status (status stays the harness's word) and joins
  `runner.lock`/attached/`depends_on`/the budget gate as a runner refusal
  waivable only by `--force` (which never releases the hold). Only a human
  releases one; the manager prompt is told so, which makes it a convention
  and not a boundary.
  `perpetual = true` (`task add --perpetual`) marks a task that is not meant to
  finish: the substrate is unchanged, but the preamble's `{perpetual}` block
  softens delivery into commit+push per cycle, the digest renders
  `perpetual=true` and withholds `possible-loop` (repetition is the job), the
  manager prompt relaunches it forever and never cancels it, and views badge it
  `∞`.
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
- `prune.py` — on-demand cleanup, and the same "archive, never delete" rule
  the bus follows: `quorum task prune` **moves** `tasks/<id>/` to
  `tasks/.archive/<id>/` (dot-prefixed, so `TaskStore.list` and therefore
  every view/digest/doctor scan skips it with no code change; restoring is
  one `mv`). Total readers — `select` (pure, over an already-loaded task
  list; skips perpetual tasks, ages off `updated_at`), `refusal`,
  `dependents_first` (pure batch order), `plan`, `worktree_plan` (the
  `--dry-run` preview of the git half) — plus `remove_task_worktree` and
  `archive_task`, kept separable because #57 will re-use the selection. The
  refusals are substrate rails of the runner's class, not policy: live
  runner, attached task, a task something else still `depends_on` (a
  dependent pruned in the same pass doesn't count), and stranded work in the
  worktree (`workdir_git_state`) — only the last is `--force`-able. The
  archive loop re-derives `refusal` per task right before archiving, so a
  runner that appeared during the confirm is caught and a task skipped
  mid-sweep leaves the batch (its upstream is refused again instead of
  dangling) — which is what `dependents_first` ordering is for.
  `--worktrees` treats the two git objects asymmetrically because git does:
  a worktree it refuses to remove leaves the task unarchived, an unmerged
  branch is kept with a note and the task archived anyway. **`--force` never
  reaches `git worktree remove`** — a tidy-up flag must not destroy
  uncommitted files; its two meanings are waiving the stranded-work refusal
  and upgrading `branch -d` to `-D` (which does lose commits, so the confirm
  prompt says so). One `_actor_guard` entry per *command*, not per task, so
  a sweep can't burn an agent's action cap half-way through. The board/inbox
  half lives in `messages.py` (`archive_board_message` → `ack_board_message`,
  `archive_topic`, `clear_inbox`), behind `quorum board ack`, `board clear`
  and `task inbox --clear`. `board ack --all <topic>` and `board clear
  <topic>` share one CLI helper (`_clear_topic`) so the alias cannot drift
  from what it aliases.
- `export.py` — `quorum task export <id>`: one `.tar.gz` of a task for
  sharing or a bug report, a **pure reader** in the #88 mold (no new
  state; the only write is the archive, refused inside the home and over
  an existing file; an ambiguous id refused by `_resolve_task` as
  everywhere). `task_entries` walks `tasks/<id>/` whole (tmp files and
  `runner.lock` — a pid, not a record — skipped), `inbox_entries` takes
  `new/` + `cur/`, `delivered_guidance` reads acked guidance back out of
  `messages/archive/` (months from the task's creation onward only),
  `worktree_diff` (`--with-worktree-diff`) diffs the worktree against
  `tasks._worktree_base` plus `--no-index` per untracked file — read-only
  git, and **refused loud** for an attached/`--no-worktree` task because
  nothing from a project directory is exported. `redact_transcript`
  (`--redact`) is pure and structural like `loop_signal`'s extraction:
  result-kind dicts lose their output fields and keep their ids, call
  items keep name/arguments, `tool_use_result` goes whole, too-deep nodes
  are dropped (failure direction: dropped), plain-text `line` entries are
  kept and counted so the CLI can say so. `write_archive` builds beside
  the target and renames, strips uid/gid. No `_actor_guard` — it mutates
  nothing.
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
  the user's live checkout. The same class of rail refuses a held task, and one whose
  `depends_on` are unfinished, unless `--force` (a premature dependent is pure
  waste); `dependency_note` puts each dependency's status/pr_url in the
  composed prompt. `launch_detached` spawns `python -m quorum task run`
  in a new session.
- `usage.py` — token/cost usage read back out of harness result events
  (claude `result`, codex `turn.completed`/`token_count`): loose extraction,
  canonical keys, **fail-soft** (a harness that reports nothing records
  `usage = null`, and readers omit rather than show `$0.00`). Reduction is
  elementwise **max** within a run (harnesses report run-cumulative totals
  and a pumped run emits one result per turn — summing would multiply the
  spend; max prefers under-counting) and a **sum** across runs. The runner
  records it on the `TaskRun`; agent runs (`agents/harness_run.py`) have no
  such record, so each appends one `{at, run, usage}` line to
  `actor.usage_path` (`state/manager/usage.jsonl`,
  `state/agents/<name>/usage.jsonl`) — every run, failures included, read back
  over a bounded tail by `usage.agent_usage`. Views/`quorum status`/the digest
  surface both (the digest opens with the manager's own spend).
  `[tasks].max_cost_per_run`/`max_tokens_per_run` (0 = off) only *flag* an
  over-budget run (`BUDGET-EXCEEDED`, `$!`) — an observation of the same
  class as `possible-loop`; enforcement is deliberately not implemented.
- `agents/manager.py` — the flagship builtin, and it makes **no decisions in Python**:
  its tick builds a situation digest (`build_digest`, pure over files — task
  statuses, runner liveness, quiet time, report/transcript tails, a
  `possible-loop` flag from `loop_signal` — a repetition read over the current
  run's tool calls (live runners only, deduped by call id, JSON-event
  harnesses only), an `overlaps=` mark from `overlap_signal` — pairs of
  live worktree tasks on one project whose worktrees change the same paths
  (`tasks.worktree_changed_paths`: read-only git against the checkout's
  branch — what the runner forked from, so an unpushed commit in the
  checkout is not charged to every task — else origin/HEAD, else the
  upstream; attached and `--no-worktree`
  tasks skipped; bounded by `OVERLAP_MAX_PAIRS`), plus a `ci:` line from
  `ci.pr_state` — all **observations the manager judges, never rails**;
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
  web app, and the TUI all read it and nothing else. `agent_rows` estimates a stale
  `next_run` from the schedule (`next_run_estimated`); `agent_detail` adds journal +
  per-agent actions. Write affordances stay thin bus/store/config calls shared with
  the CLI — never view-local write logic. The two surfaces overlap only on nudge;
  neither is a superset of the other. **TUI**: nudge (`n`), manager directive (`m`,
  the `manager` inbox, same as `quorum manager tell`), run (`s`,
  `runner.launch_detached`, refused on an attached task or a live runner) and cancel
  (`c`, a `cancelled` status update, the one destructive binding so it confirms
  through `ConfirmScreen`) — all four target the *highlighted* row while the task
  table has focus (`enter` opens a transcript, it does not arm the write keys),
  falling back to the open task, and all four go through `_write`, so an unwritable
  home notifies instead of taking the dashboard down. `a` is the fifth,
  aimed at the banner rather than a task: `AttentionScreen` is a picker (the
  banner is a count and the board pane is a log, so neither can be pointed
  at) that dismisses with a message id, and the app acks it through `_write`. **Web**: nudge, board posts,
  project edits, and agent create (via `config.create_agent`) /
  pause / resume / run-now / reload, plus an Ack button per live escalation
  (`POST /api/board/{topic}/ack/{message_id}`) — the same shared bus call.
- `actor.py` — the actor-identity env protocol: who a quorum CLI call is acting
  as, name-generic over harness-driven agents. An agent tags the harness it
  spawns (`actor_env(name, run_id, cap)`), the CLI resolves `current_actor()`
  for journaling and message attribution, and the runner `strip_actor_env`s
  spawned children so they act as themselves. Also owns `journal_path`/
  `notes_path`/`transcript_path` (manager at `state/manager/`, others at
  `state/agents/<name>/`).
- `notes.py` — the notebook: an agent's *standing* memory, deliberately a
  separate buffer from both the journal (a bounded tail of one run's actions,
  which a busy tick scrolls) and the board (which anything may post to).
  Append-only `notes.jsonl`; `quorum manager remember "…" [--ttl N]` writes
  through `_actor_guard`, `forget` appends a tombstone, and `may_write` refuses
  any actor that is not the notebook's own agent or an untagged human — tasks
  reach the manager with `task report` and the board. That fence reads
  `QUORUM_ACTOR`, so it is a **convention against accidental crowding, not a
  security boundary** (the sandbox is); say so in docs rather than overselling
  it. Reads are owner-checked too (`check_owner`, `--agent` is a path
  component), and a malformed line is skipped, never raised, so one bad line
  can't fail every tick. `digest_section` renders it **before** the task
  section under its own `NOTES_MAX_ENTRIES`/`NOTES_MAX_BYTES` (nothing else
  spends that budget, so noisy tasks can't shrink it), keeps the newest over
  the cap and says how many it dropped — plus how many bytes fell outside
  `NOTES_SCAN_BYTES`, so a truncated memory is visible. No Python
  summarization: consolidation is prompt policy.
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
- `notify.py` — the `[notify]` hook: the one board *consumer* quorum ships
  for a person. `drain(home, cfg, bus)` reads each listed topic past a
  private per-topic cursor in `state/notify.json` (the on-disk filename,
  via `MessageBus.entries_after_cursor` — never `Message.filename()`,
  which is only what `post()` happened to write) and runs the argv
  via `MessageBus.entries_after_cursor`'s `limit`, and armed at the tail
  through `topic_tail` so nothing is parsed to be discarded) and runs the
  argv template once per message via `deliver` (`build_argv` substitutes
  `{text}/{from}/{topic}/{type}/{id}` per element, appending the text
  when the template has no `{text}` — the harness `{prompt}` convention).
  Supervisor job `_notify` on the `_control` cadence plus once at
  startup — that startup call runs *before* `scheduler.start()` and the
  janitor, and `drain` holds a module lock, so the two callers can never
  interleave over one cursor. The cursor is advanced and persisted
  **before** each delivery: **at-most-once** on purpose, a lost
  notification being cheaper than one that repeats every 15s because the
  cursor write is what failed. **Fail-soft in herdr's mold**: missing
  binary / nonzero exit / hang past `timeout_seconds` → one
  `supervisor.log` line, `drain` never raises (an unwritable cursor is
  logged; an unreadable one re-initialized). First drain arms the cursor
  at the tail *without* delivering (enabling must not replay history);
  `MAX_PER_TICK` bounds one tick. Fires on topic membership, never on
  content — no policy here. `quorum notify test` is the loud path (exit
  1 with the reason, touches neither board nor cursor); doctor's
  `check_notify` is static (`–` absent, `✗` argv[0] not on PATH).
- `forge.py` — the *only* module that shells out to a forge CLI (`gh` today),
  and the one place two opposite failure contracts meet: `run_json` (soft,
  behind `ci.pr_state`) and `auth_status` (soft, `True`/`False`/`None` for
  doctor) versus `issue_view` (**loud** — `task add --issue` runs in front of
  a person, so no gh / no auth / unknown issue / timeout / no url each raise
  `ForgeError` naming the fix, and `issue_ref` rejects a bad reference before
  spending a subprocess, and a full issue url is handed to the CLI whole so
  it names its own repository). One `_invoke` for all three, so
  unattended-invocation details are stated once; `cli_name(home)` is the single seam #51's
  `gh | glab | none` switch lands on. **No write path** — quorum reads a
  forge and never labels, comments on or closes anything.
- `ci.py` — the digest-facing half over `forge.py`, and the second fail-soft
  probe (herdr's mold, not sandbox.py's; both read config through
  `try_load_config`, so an unreadable config.toml disables them):
  `pr_state(home, task)` runs one
  `gh pr view --json ...` *inside* the task's workdir (gh resolves repo from the
  remote, PR from the checked-out branch) and returns state / check counts /
  failing check names / merge conflict — or `None` for every disappointment
  (disabled, no gh, no auth, no remote, no PR, timeout, garbage), so a digest
  always builds and a missing `ci:` line means nothing. The PR's own state is
  normalized (`normalize_state` → `tasks.PR_STATES`: `open|merged|closed`,
  anything else `unknown`) so a second forge backend fills the same field.
  Only `build_digest`
  calls `pr_state` (a `ci:` line per task, `CI-FAILING` on a finished task over red
  checks — suppressed explicitly for a merged PR, bounded by
  `manager.CI_MAX_PROBES` since digest build blocks the
  tick), which is what keeps `views.py` a pure file reader. Exactly **one**
  probe result is materialized: that closure calls `tasks.record_pr_state`
  to write `pr_state`/`pr_state_at` onto `task.json` (one writer, one call
  site, closed vocabulary, never a status, `updated_at` untouched, fail-soft)
  so views badge `✔` merged without a network call — the rule this used to
  state outright ("never materialize") was revised for that case in #57, and
  the five properties fencing it are in `docs/architecture.md` ("The merged
  observation"). A second exception must earn all five again.
  What to *do* about red CI lives in `prompts/manager.md` and the shipped
  `prompts/babysitter.md`, never here. Optional `[ci]` table (`enabled`,
  `timeout_seconds`), shared with `forge.py` — the same two switches gate
  issue intake.
- `doctor.py` — `quorum doctor`: the one place that looks at everything that
  fails soft (config, `[harness.*]` binaries/templates, git, projects, gh
  auth, herdr, nono, prompt staleness, supervisor lock + version, orphaned
  runner.locks, stale `cur/` claims, agent failure streaks). One small
  function per check returning `Check(name, status, summary, fix)` in three
  states — `ok`/`problem`/`na` (✓/✗/–), only `problem` exits non-zero — so
  each has a passing and a failing test. It **diagnoses and never repairs**
  (every ✗ names its fix; no `--fix`), and is a pure reader apart from the
  opt-in `--smoke [harness]` probe, which runs a harness for real in a
  `TemporaryDirectory` through the runner's own argv/pump/transcript code
  (inject included) and asserts a `result` event plus a session id — the
  check that would have caught the stream-json hang (#24). The smoke child
  gets its own session (`start_new_session=True`, so a timeout `killpg`s the
  wrapper's whole tree) and a *scratch* `QUORUM_HOME` (an installed
  integration hook firing mid-probe must never touch the live home).
  It borrows rather than duplicates: gh through `forge.auth_status`, prompt
  staleness through `home.classify_prompt`. A `✗` is reserved for something
  actually wrong — an offline gh and a fresh home with no harness yet are
  both `–`, so `quorum init && quorum doctor` exits 0. `check_config` is
  the codebase's one deliberate strict `load_config` caller.
- `config.py` — one place to load config: `load_config` raises,
  `try_load_config` returns defaults for a *missing* file (the user said
  nothing) and None for a malformed/undecodable one — what the fail-soft
  probes use, so an unreadable config means their feature is **off**, never
  fail-open, and it never raises — `load_config_or_default` fills in defaults
  for everything (cli/views/digest).
  `config.toml` is user-owned and **quorum never writes it back**; machine
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
  `default_prompts/` (`task-preamble`, `task-perpetual`, `manager`, `babysitter`); deleting a file restores the
  default. `format_map` with a missing-key-preserving dict. Re-running `quorum
  init` upgrades seeded-but-never-edited copies, recognized by the seed record
  `prompts/.seeded.json` (`home.read_seeded_record`: {filename: sha256 of what
  init last wrote or found identical to the default}; written by `_seed_prompts`
  only, fail-soft — no/malformed record classifies a differing copy as `edited`,
  never overwrites). Changing a file in `default_prompts/` therefore needs no
  bookkeeping in `home.py`.
  `prompts/<name>.local.md` is the *overlay* (#37): user-owned, never seeded,
  never touched by `init`, merged by `render` at the template's first unescaped
  `{local}` slot (packaged `manager.md`, `task-preamble.md` and
  `task-perpetual.md` carry one) or prepended when there is none;
  absent/blank/unreadable renders to nothing (the slot's own line goes with it) —
  `load_local` is fail-soft because `render` is on the manager tick and every task
  run, while `load` of the template itself stays loud. It exists so adding house
  policy does not fork the whole template and strand the home on an old default —
  a rewritten `<name>.md` still wins. `quorum prompt list|diff <name>` shows
  home-copy-vs-packaged-default and degrades per file (`?`) over an unreadable one.
  `{project}` (#63) is the fourth layer and task-facing only: `project_block`
  assembles the preamble's per-project text from the registry `notes`
  (`project set --notes-file`) then `<project>/.quorum/task-preamble.local.md`
  — a **read**, fail-soft for `load_local`'s reason doubled (every run, a
  file quorum does not own), taken from the *project* dir rather than the
  worktree because that is the copy the user maintains — and read *before*
  `apply_task_sandbox`, which grants the worktree and the project's `.git`
  but never the project dir, so `compose_prompt` takes the block as an
  argument. Same empty-slot rule
  as `{local}`, deliberately **no prepend fallback** (a rescued overlay is
  policy the home already had; a project block is new and has no defensible
  place in a rewritten template) — `prompt list` names the projects that
  contribute one and warns when the template has no slot. Full order:
  packaged default → home copy → home overlay → project slot.
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
