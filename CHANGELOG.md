# Changelog

All notable changes to quorum are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may change
behaviour, patch bumps are fixes only).

The PyPI distribution is `quorum-orchestrator`; the CLI and import name are `quorum`.

## [Unreleased]

### Added
- `quorum task add <project> -` reads the prompt from stdin, and
  `--prompt-file <path>` reads it from a file — both byte-for-byte, so a
  piped GitHub issue is stored exactly as it arrived. Exactly one of the
  three sources is allowed, and empty input is refused. Makes the
  issue-driven loop a one-liner without putting `gh` inside quorum:
  `gh issue view 14 --json title,body -q '"\(.title)\n\n\(.body)"' | quorum task add my-api -`
  (#60)
- The task budget gates the next run (the enforcement half of #19): with
  `[tasks].max_cost_per_run` / `max_tokens_per_run` set, a task whose
  *last* run reported more than the budget is refused by `run_task`,
  `quorum task run` (and `--detach`, in the parent) and the TUI's `s`
  key until `--force` or a run that comes in under budget — a run that
  reports no usage counts as under. A rail of the rate-limit class the
  per-run action cap belongs to: it never kills a run in progress, never
  sets status, and never vetoes a choice. `task list` marks a gated task
  `$! GATED`, `task show` adds a `gated:` line, `task_rows` carries
  `budget_gated`, and the digest's `BUDGET-EXCEEDED` line now ends
  `(next run gated; --force to override)` on the last run (`(an earlier
  run; a later one cleared the gate)` on older ones) so the manager knows
  why a relaunch failed. The packaged `manager.md` says what to do instead
  of relaunching as-is — sharpen the nudge, decompose, escalate — and
  `quorum init` upgrades unedited copies.

### Fixed

- The guidance pump could close a stream-json harness's stdin with a nudge
  in flight: a message was claimed (renamed out of `new/`) before it was
  counted as delivered, so a `result` event landing in that gap saw an
  idle run and ended it — the nudge bounced back to `new/` and the run
  recorded one result event instead of two (a rare CI flake). The claim
  and the count now happen under the same lock the close check takes.
- **Merged pull requests are visible** (#57). A task ends at the harness's
  word (`done`); its work is delivered when the PR merges. The CI probe now
  normalizes the PR's own state to `open` / `merged` / `closed` and the
  digest's `ci:` line carries it, so the manager can tell "done and shipped"
  from "done and waiting on a human" — the default `manager.md` reads a
  merged task as needing nothing, and a `done` task whose PR was *closed
  unmerged* as one line for the human. `quorum status`, `task list`, `task
  show`, the TUI and the web dashboard badge it (`✔` merged, `⊘` closed
  unmerged) without making a network call, because the manager tick records
  what it saw as `pr_state` / `pr_state_at` on `tasks/<id>/task.json`.

  Quorum still never changes a status because a PR merged: `done` is the
  harness's word, merged is the forge's. Fail-soft like the rest of `ci.py`
  — no `gh`, no PR, `[ci].enabled = false` → nothing recorded and no badge,
  and **no badge never means "not merged"**, only "never observed". Field
  names are forge-neutral so a GitLab backend (#51) fills the same ones.

### Changed

- The `ci:` digest line renders `state=merged` where it used to render
  `state=MERGED` (all PR states are lowercase now). A merged PR never
  carries `CI-FAILING`, even if the forge still serves a stale red rollup.
- `docs/architecture.md`'s "nothing materializes its result to disk" note is
  revised: `pr_state` is the one deliberate exception, and the section now
  lists the five properties that fence it — this is the case that note said
  to revisit for.

### Upgrading

Run `quorum init` to pick up the new `manager.md` (a copy you never edited
is upgraded in place, recognized by hash; an edited one is left alone —
`quorum prompt diff manager.md` shows what changed). Existing `task.json`
records need no migration: the new fields are absent until the manager next
observes a PR.
- **The manager can see its own last few runs** (#59). Each agent harness run
  already appended a line to `state/manager/usage.jsonl` (or
  `state/agents/<name>/usage.jsonl`); that line now also carries `outcome`
  (`ok` / `raised` / `timeout`) and `duration_seconds`, and the digest opens
  with `Your last 5 runs: ok 2m10s · TIMEOUT 15m00s · …` next to the
  existing spend line. A run that times out reports no usage at all, so its
  outcome is exactly what a spend-only ledger lost. Ledger lines written by
  earlier versions read back as unknown (`?`), never as `ok`.
- **The action cap says so in the journal** (#59). Refusing an action over
  `max_actions_per_run` now appends one `cap.hit` entry per run, so the next
  digest's journal section shows the run that ran out of budget; the digest
  header also states the budget (`Actions this run: 0 of 20 (cap)`). Both are
  observations — nothing pauses, throttles or changes the cap — and the
  manager prompt gains one rule: shorten your own work when your runs time
  out, escalate after hitting the cap two runs running.
- A prompt agent whose template writes `{notes}` gets the same
  self-observation lines above its notebook (no new placeholder).
- Notification hook: a `[notify]` table holds an argv template
  (`{text}`, `{from}`, `{topic}`, `{type}`, `{id}` substituted per argument,
  no shell) that the supervisor runs once for every new message on the
  listed board topics — `attention` by default, so a manager escalation or
  an `agent.failing` reaches you without your looking. A private cursor in
  `state/notify.json`, advanced and persisted *before* each delivery,
  makes it at-most-once across restarts — nothing is ever sent twice, and
  a crash mid-hook loses one notification rather than repeating it forever
  (posts while the supervisor is down go out on the next start, oldest
  first; enabling it starts from now). Delivery fails soft: a missing
  binary, nonzero exit or timeout is one `supervisor.log` line and the
  cursor has already advanced.
  `quorum notify test "…"` proves the wiring loudly; `quorum doctor` gains
  a `notify` line. (#55)
- On-demand cleanup, all of it "archive, never delete" (#53):
  - `quorum task prune [--status] [--older-than] [--worktrees] [--dry-run]
    [--force]` moves finished tasks into `tasks/.archive/<id>/`. The
    directory is dot-prefixed, so every existing reader — `status`,
    `task list`, the TUI, the web dashboard, the manager digest — skips it
    with no code change, and restoring a task is one `mv` back. Refuses a
    task with a live runner, an attached task, one another task still
    depends on, and (unless `--force`) one whose worktree holds uncommitted
    or unpushed work. `--worktrees` adds `git worktree remove` plus branch
    deletion, keeping an unmerged branch unless forced. `--force` is never
    passed to `git worktree remove`: a dirty worktree is left alone and its
    task unarchived. Its two meanings are waiving the stranded-work refusal
    and upgrading `git branch -d` to `-D` — the one destructive thing here,
    said out loud in the confirm prompt. `--worktrees --dry-run` names each
    worktree and branch it would touch.
  - `quorum board clear <topic> [--before 7d|<date>] [--dry-run]` archives a
    board topic into the same `messages/archive/YYYY-MM.jsonl.gz` the
    janitor writes — `board clear attention` empties the escalation banner.
  - `quorum task inbox <id> --clear` archives guidance still waiting
    undelivered (unclaimed mail only).
- Attention acknowledgement (#56): `quorum board ack <message-id>` archives
  one board message down the same path, so an escalation you have handled
  leaves the `#attention` banner in `quorum status`, the TUI header and the
  web header instead of sitting there for the seven-day window — while
  `messages/archive/` keeps it with its original `created_at`. Ids resolve
  like task ids (full id, unique prefix, or the short suffix `board read`
  now prints); unknown and ambiguous are refused, never guessed at.
  `board ack --all <topic>` is `board clear <topic>`, implemented on top of
  it. The same ack is a keystroke in the TUI (`a` opens the `#attention`
  list and acks the highlighted line, notifying rather than crashing on an
  unwritable home) and an **Ack** button per escalation in the web
  dashboard's new Attention panel — both thin calls to one shared
  `MessageBus.ack_board_message`. Every list an ack acts on is a snapshot, so
  a message archived out of band (the janitor, a second `board ack`, the web
  panel) between the render and the keystroke is reported, never a traceback:
  the TUI notifies and stays up, and the CLI archives the path it already
  resolved instead of resolving twice. `--topic` alongside `--all` is refused
  — the `--all` argument is itself the topic.
### Changed
- `quorum status`, `task list`, `agent list` and `project list` render
  Rich tables instead of concatenated lines: one headed column per field,
  fitted to the terminal (the report and flags columns are ellipsized
  where the window runs out — never wrapped mid-cell — so id, status,
  harness, pr and usage stay whole down to the width where the give-way
  column has nothing left to give; narrower still, the id is the last
  column clipped), the PR URL shortened to `#N`
  (`task show` keeps the full URL), usage in its own `usage` column, and
  columns nothing fills dropped. Piped or redirected, the same tables come
  out plain and at full width, so grepping an id or status keeps working.
  The guide now says where the `$` figure comes from: the harness CLI's
  own reported cost, never a quorum estimate. (#52)

## [0.2.0] - 2026-09-01

### Upgrading from 0.1.0

After installing the new version, in each `QUORUM_HOME`:

1. `quorum init` — refreshes every prompt you never edited to the new
   packaged default (recognized by hash) and seeds the new
   `task-perpetual.md` and `babysitter.md`. Config is left untouched.
2. If you edited `prompts/manager.md`, move your house rules into
   `prompts/manager.local.md` and delete the edited copy, then run
   `quorum init` again — an edited template is never upgraded, so it would
   otherwise miss every 0.2.0 policy change (`quorum prompt diff manager`
   shows the gap).
3. `quorum down && quorum up` — the manager tick runs inside the supervisor
   process, which keeps the old code until restarted. Detached task runs
   are unaffected.
4. `quorum doctor` — confirms the result.

### Added
- `quorum doctor`: one pass over everything that fails soft — config (the one
  strict parse), `[harness.*]` binaries and argv templates, git, projects, gh
  auth, herdr, nono, prompt staleness, supervisor lock and version, orphaned
  `runner.lock`s, stale inbox claims, agent failure streaks. One line per
  check (✓ / ✗ / –, only ✗ exits non-zero), `--json` for scripts, and an
  opt-in `--smoke [HARNESS]` that runs your harness for real through the
  runner's own code — in a scratch directory *and* a scratch `QUORUM_HOME`,
  killing the whole process tree on timeout. Diagnoses only; never repairs.
  (#39, #49)
- Task dependencies: `quorum task add --after <id>...` queues a task that
  waits on others. The runner refuses to start it while any upstream is
  still unfinished (`⏳` in status/TUI/web, `waiting-on=` in the digest, and
  the manager prompt tells it never to launch one); an upstream that ended
  `blocked`/`cancelled` or was pruned no longer blocks — it is surfaced as
  `DEP-FAILED` / `DEP-MISSING` for the manager to judge. A perpetual task
  can never be an upstream. (#31, #45)
- Prompt overlays: `prompts/<name>.local.md` is merged into the packaged
  template at a `{local}` slot (manager, task preamble, perpetual block), so
  house policy lives beside the default instead of forking it, and
  `quorum init` keeps upgrading the unedited template. An unreadable overlay
  renders as no overlay rather than failing every tick; `quorum prompt
  list|diff` show overlays and degrade per file. (#37, #46)
- Manager notebook: `quorum manager remember|notes|forget` is a standing
  memory separate from the scrolling action journal — `notes.jsonl` per
  agent, optional `--ttl`, rendered in its own bounded slot at the top of
  every digest (with a line for what was dropped or not scanned). Only the
  owner or the user may write to it (a convention, not a security boundary);
  malformed lines are skipped, never crash a tick. Both dashboards show an
  agent's notebook. (#35, #47)
- TUI write affordances beyond the nudge: `m` sends the manager a directive
  (`quorum manager tell`), `s` starts a detached run (refused on an attached
  task or a live runner), `c` cancels a task behind a yes/no confirmation.
  All four act on the *highlighted* row while the task table has focus —
  `enter` opens a transcript, it does not arm the write keys — and all four
  report an unwritable home as a notification rather than crashing the
  dashboard. (#11, #44)
- Sustained-failure escalation for agents exempt from auto-pause: after
  `MAX_CONSECUTIVE_FAILURES` the supervisor posts one `agent.failing` to
  `attention` (the banner `quorum status`, the TUI and the web header read) —
  the only failure path that reaches `attention`; auto-pause, tick errors and
  the closing `agent.recovered` all stay on `system`. Deduped by an
  `escalated_at` heartbeat stamp written after the post lands, and cleared by
  every success path (scheduled tick, `agent run-once`, `agent resume`). (#38)
- First-class perpetual tasks: `quorum task add --perpetual` marks work that
  is not meant to finish. The preamble's `{perpetual}` block (new packaged
  `task-perpetual.md`, appended even on homes with an edited preamble)
  softens delivery to commit+push per cycle; the digest renders
  `perpetual=true`, withholds `possible-loop`, and flags `PERPETUAL-ENDED`
  when such a task reports a terminal status; the manager prompt relaunches
  it forever and never cancels it; status/TUI/web badge it `∞`. (#12, #36)
- Agent-run usage ledger: manager and prompt-agent harness runs append
  `{at, run, usage}` to `state/manager/usage.jsonl` /
  `state/agents/<name>/usage.jsonl`; surfaced on agent rows and as the
  digest's opening self-cost line. (#32, #36)
- Optional auto-commit safety net: with `[tasks].auto_commit = true` a dirty
  worktree is committed to the task branch after the harness exits — never
  pushed, never sets status, refuses detached-HEAD / mid-merge trees, skipped
  under the nono sandbox, recorded on the run and in the transcript. (#13, #25)
- `possible-loop` digest flag: a repetition read over a live run's tool calls,
  an observation for the manager to judge, thresholds tuned to prefer false
  negatives. (#18, #28)
- Token/cost usage captured from harness result events (claude `result`, codex
  `turn.completed`/`token_count`) onto each `TaskRun`, fail-soft (`null` when a
  harness reports nothing), surfaced in `quorum status`, the TUI, the web
  dashboard and the manager digest. `[tasks].max_cost_per_run` /
  `max_tokens_per_run` flag an over-budget run (`BUDGET-EXCEEDED`) without
  enforcing anything. (#19 capture half, #29)
- Fail-soft CI probe: `ci.py` runs `gh pr view` inside a task's workdir and
  adds a `ci:` line (state, checks, failing check names, merge conflict) to
  the digest, with `CI-FAILING` on a finished task over red checks; every
  disappointment degrades to no line. Optional `[ci]` table. (#17, #30)
- Shipped `babysitter` prompt agent: a whole CI-reactive policy written as
  prompt text, started with `quorum agent create babysitter --schedule "every 10m"`. (#17, #30)
- Positioning pass across README and docs: cross-harness, local-first,
  policy-owned supervision and session adoption claimed explicitly. (#22, #26)

### Fixed
- `[ci].enabled = false` (and `[herdr]`) inside a malformed `config.toml`
  was silently ignored — the probes fell back to enabled. An unreadable
  config now disables them; a missing one still auto-detects; neither can
  raise into the manager tick. The four private load-config fallbacks in
  cli/views/manager/ci became `config.try_load_config` /
  `load_config_or_default`. (#33, #34, #36)
- Harnesses with `inject = "stream-json"` receive the prompt as the opening
  stdin turn instead of an ignored argv argument — previously every such run
  hung until timeout. (#24)
- A successful tick clears stale failure fields on the agent heartbeat, so a
  recovered agent no longer reports its last error forever. (#27)
- Codex usage fallback no longer double-counts `cached_input_tokens` (a
  subset of `input_tokens`, unlike claude's disjoint fields); malformed usage
  data on disk degrades to silence instead of raising out of status/web. (#29)
- The CI probe's fail-soft contract now covers every exception (a non-UTF-8
  `gh` output could previously crash the manager tick), and the per-digest
  probe budget is spent only on tasks that can actually be probed. (#30)

## [0.1.0] - 2026-08-29

First tagged release. Bring-your-own-harness orchestration for long-running
coding tasks: projects, plain-English tasks executed as harness runs in
per-task git worktrees, a harness-driven manager whose policy lives in
`prompts/manager.md`, a file-based board + maildir inboxes as the only
transports, live-session adoption for claude-code / codex / opencode, TUI and
web dashboards as pure readers, optional nono sandboxing, and a herdr doorbell.

[Unreleased]: https://github.com/kvndhrty/quorum/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kvndhrty/quorum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kvndhrty/quorum/releases/tag/v0.1.0
