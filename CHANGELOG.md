# Changelog

All notable changes to quorum are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may change
behaviour, patch bumps are fixes only).

The PyPI distribution is `quorum-orchestrator`; the CLI and import name are `quorum`.

## [Unreleased]

The "Operate unattended" package (#67): a home should run for a week without
anyone editing files by hand, and every escalation should reach a person the
minute it is posted.

### Added
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
- Attention acknowledgement: `quorum board ack <message-id>` archives
  one board message, so an escalation you have handled
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
  — the `--all` argument is itself the topic. (#56)
- Per-project prompt conventions (#63): the task preamble gains a
  `{project}` slot filled from the project's registry `notes` (now editable
  with `quorum project set <slug> --notes-file <path>`, `-` for stdin) and
  from `.quorum/task-preamble.local.md` inside the project directory — a
  read-only, user-owned file, like the `.quorum.toml` marker. Repo
  conventions ("base on develop", "run just check") no longer have to go
  into the home-wide overlay, which is wrong for a home with several
  projects. It follows the `{local}` rules: rendered through
  `prompts.render`, read fail-soft (an undecodable file costs the block, not
  the run), and an empty block takes its line with it. It has no prepend
  fallback — `quorum prompt list` now lists every project that contributes a
  block, marks one it cannot decode, and warns when a rewritten
  `task-preamble.md` has no `{project}` slot to render them into.
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
- **Hung-session restart** (#42). A harness session that hangs — blocked on
  stdin, waiting on a turn that never returns — used to cost a whole
  supervision cycle at best and a whole night at worst, because the only kill
  was `task cancel --kill`, which ends the task too.
  - `quorum task stop <id>` ends the *run* and nothing else: SIGTERM (then
    SIGKILL) to the runner's process group, so the harness and everything it
    spawned go with it, while the task keeps its status, its queue position
    and its worktree. The interrupted run is recorded (`stopped`, its
    `fresh_session` kind, a `run.stopped` transcript note, the stale lock
    cleared) rather than left looking live — including for a runner that was
    already dead when the stop looked, which gets the same tidying without a
    signal. Attached tasks are refused — quorum never kills your own
    interactive session.
  - `quorum task run <id> --fresh-session` forgets the stored session id and
    starts a new session in the same worktree, for when *resuming* is what
    keeps failing. Recorded as `fresh_session` on the run — by the runner,
    and by `task stop` for a fresh run it killed, so the manager's
    "escalate after two fresh restarts" can actually count them.
  - `[tasks].run_stall_timeout_seconds` (0 = off, the default) is a runner
    watchdog: no harness output for that long ends the run, marks it
    `stalled`, and turns a hang into an ordinary dead runner. It counts
    silence, not progress — set it above your longest quiet step.
  - The manager digest gains a `STALLED` observation (a live runner whose
    transcript has not grown for 30 minutes — or, for a run that hung before
    printing anything at all, one that started that long ago) plus `stopped=` /
    `fresh_sessions=` / `last-run=stalled` marks, and `prompts/manager.md`
    gains the policy that reads them: look at the tail once, stop and
    resume, then restart with a fresh session and a summarizing nudge, then
    escalate after two fresh restarts. Observations, never rails — quorum
    still ends no run on its own judgement.
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
  of relaunching as-is — sharpen the nudge, decompose, escalate.
- Overlap observation: the manager digest marks any two live worktree tasks
  on one project whose branches change the same files with
  `overlaps=<id> paths=N` on both lines, plus an `overlap:` line naming up
  to three shared paths — read from the worktrees with local read-only git
  (committed, uncommitted and untracked changes against the base branch:
  the project checkout's branch — what the runner forked the worktree from
  — else `origin/HEAD`, else the upstream), no network,
  bounded by `OVERLAP_MAX_PAIRS`. Attached sessions and `--no-worktree`
  tasks are never compared. An observation like `possible-loop`, never a
  rail; the manager prompt says to nudge both to rebase or serialize them.
  (#58)
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
  out, escalate after hitting the cap two runs running. A prompt agent whose
  template writes `{notes}` gets the same self-observation lines above its
  notebook (no new placeholder).
- `quorum task add <project> -` reads the prompt from stdin, and
  `--prompt-file <path>` reads it from a file — both byte-for-byte, so a
  piped GitHub issue is stored exactly as it arrived. Exactly one of the
  three sources is allowed, and empty input is refused. Makes the
  issue-driven loop a one-liner without putting `gh` inside quorum:
  `gh issue view 14 --json title,body -q '"\(.title)\n\n\(.body)"' | quorum task add my-api -`
  (#60)
- **Task priority and hold/release** (#61): two ways to steer the queue
  without editing `task.json` or cancelling anything. `task add --priority N`
  and `task set-priority <id> N` record an ordering hint the manager reads
  (higher first, negative to the back) — quorum sorts nothing by it, the
  digest renders `priority=N` only when it is not 0, the views badge `↑N` /
  `↓N`, and `prompts/manager.md` is what turns the number into a launch
  order. `task hold <id>` parks a task and `task release <id>` puts it back:
  a parking brake, not an ending, so unlike `task cancel` the status stays
  the harness's word and the worktree, branch and queue position survive.
  A held task is refused by the runner (`--force` runs it once and does not
  release the hold) — the fifth substrate rail, beside `runner.lock`, the
  attached-task, dependency and budget refusals — shows `held=true` on the
  digest with a line telling the manager never to launch or release one, and
  badges `⏸` everywhere. In the TUI, `h` toggles hold and `+` / `-` nudge
  priority; every verb is journaled and capped like any other mutating
  action.

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
- The task preamble's delivery protocol now says to `git fetch` and rebase
  onto the base branch before pushing, to push again with
  `--force-with-lease` (never a bare `--force`, never off the task's own
  branch) when the rebase leaves an already-pushed branch unable to
  fast-forward, and to report `blocked` naming the conflicting files when
  the rebase cannot complete. (#58)
- **A zombie process no longer counts as a live run.** An exited process its
  parent has not reaped is still a process-table entry, so `kill(pid, 0)`
  and `killpg(pgid, 0)` both answered "alive" for one — which made
  `quorum task stop` report a run that survived SIGKILL and left the next
  `task run` refusing to start. `runner.launch_detached` now reaps its child
  from a daemon thread (a caller that keeps running, such as the TUI's `s`
  binding, no longer leaves one behind), and `fsio.pid_alive` /
  `fsio.group_alive` confirm a live-looking pid or group against the process
  state `ps` reports, so a corpse reads as dead. Fail-soft: where `ps`
  cannot answer, the old reading stands. (#42)
- The `ci:` digest line renders `state=merged` where it used to render
  `state=MERGED` (all PR states are lowercase now). A merged PR never
  carries `CI-FAILING`, even if the forge still serves a stale red rollup.
  (#57)
- `docs/architecture.md`'s "nothing materializes its result to disk" note is
  revised: `pr_state` is the one deliberate exception, and the section now
  lists the five properties that fence it — this is the case that note said
  to revisit for. (#57)

### Fixed
- The guidance pump could close a stream-json harness's stdin with a nudge
  in flight: a message was claimed (renamed out of `new/`) before it was
  counted as delivered, so a `result` event landing in that gap saw an
  idle run and ended it — the nudge bounced back to `new/` and the run
  recorded one result event instead of two (a rare CI flake). The claim
  and the count now happen under the same lock the close check takes.

### Upgrading
After installing, in each `QUORUM_HOME`:

1. `quorum init` — both `manager.md` (merged/closed PRs, the budget gate,
   self-observations, overlaps, the hung-session ladder: eighteen rules now)
   and `task-preamble.md` (rebase before push) changed. A copy you never
   edited is upgraded in place, recognized by hash, including copies seeded
   from any intermediate 0.2.x main; an edited one is left alone — move
   house rules into `prompts/<name>.local.md` and delete the edited copy, or
   `quorum prompt diff <name>` shows the gap.
2. `quorum down && quorum up` — the manager tick and the new `_notify` job
   run inside the supervisor process, which keeps the old code until
   restarted. Detached task runs are unaffected.
3. Optionally add a `[notify]` table (`docs/guide.md#getting-notified`) and
   `quorum notify test "hello"` to prove it.

No file migrates: `task.json` gains `pr_state` fields only when the manager
next observes a PR, `state/notify.json` appears on the first drain, and
older `usage.jsonl` lines read back with an unknown outcome.

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
