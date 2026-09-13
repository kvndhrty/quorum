# Changelog

All notable changes to quorum are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may change
behaviour, patch bumps are fixes only).

The PyPI distribution is `quorum-orchestrator`; the CLI and import name are `quorum`.

Every pull request appends to the same `[Unreleased]` list, so two branches
nearly always touch adjacent lines here. `.gitattributes` marks this file
`merge=union` for that reason: a merge keeps both sides' bullets instead of
reporting a conflict over lines that do not disagree. Read the merged list
before a release — union can duplicate or reorder a bullet both branches
edited.

## [Unreleased]

The "Operate unattended" package (#67): a home should run for a week without
anyone editing files by hand, and every escalation should reach a person the
minute it is posted.

### Added
- The surface inventory (#102): `scripts/surfaces.py` prints one table per
  class of thing quorum exposes — CLI commands, options and arguments,
  config keys, the home layout, TUI key bindings, prompt placeholders,
  doctor checks, digest markers, guide sections — each with a count, read
  from the code rather than the docs. `quorum doctor` ends with the same
  three numbers as one informational `–` line (`surfaces: N commands,
  M options, K config keys`, `surfaces` in `--json`, never a ✗), counted by
  the shared `quorum.surfaces` module so the two cannot disagree. Re-run the
  script after a change that adds or removes a surface.
- The trust dials in one place (#85): a guide section, "Loosening the rails
  as trust is earned", tables every setting that records how far a home
  currently trusts its models — the launch cap, `max_actions_per_run`,
  `run_timeout_seconds`, the per-run budget, the stall watchdog, the
  manager's cadence, who launches, who decomposes, who merges — with where
  it lives, its default and the condition for moving it, facing a list of
  what does not move (the invariants). `dials.py` is the registry behind
  it: `quorum doctor` ends with each dial's current value as an
  informational `–` line (`dial.*` in `--json`, never a ✗), and a test
  fails when a numeric `[tasks]`/`[agents]` option with a default has no
  row in the table. `docs/architecture.md` and `CLAUDE.md` link to the
  section as the place a change to either list is argued.
- Readable logs: one narrative renderer (`quorum.transcript`) behind
  `quorum task log`, so a run reads as what it tried
  and what came back rather than as a few hundred JSON events — assistant
  text in full, one line per tool call with its first argument, results
  collapsed to a size or an exit code, reasoning and noise events folded
  (`-v` unfolds all of it, `--raw` prints the old output byte for byte, an
  unrecognized event prints as its raw line rather than raising). The TUI's
  transcript pane renders through the same function, so the surfaces cannot
  disagree, and the per-harness
  event shapes now live in one place — `manager.loop_signal` and the
  runner's session-id capture read it too.
  `quorum agent log <name> [--last N | --run <id>]` reads one *tick* end to
  end: the digest it was given, what it said, the actions the CLI journaled
  for it with their then-vs-now outcome, and what the run cost. That first
  part needed the one new file, `state/<agent>/runs/<run>.md` — bounded like
  the journal tail (newest fifty per agent, head-truncated) and read by
  nothing that decides anything. The manager is an agent here like any
  other: `quorum agent log manager`, with `-f` to follow a live tick. (#82)
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
  leaves the `#attention` banner in `quorum status` and the TUI header
  instead of sitting there for the seven-day window — while
  `messages/archive/` keeps it with its original `created_at`. Ids resolve
  like task ids (full id, unique prefix, or the short suffix `board read`
  now prints); unknown and ambiguous are refused, never guessed at.
  A whole topic at once is `board clear <topic>`.
  The same ack is a keystroke in the TUI (`a` opens the `#attention`
  list and acks the highlighted line, notifying rather than crashing on an
  unwritable home) — a thin call to one shared
  `MessageBus.ack_board_message`. Every list an ack acts on is a snapshot, so
  a message archived out of band (the janitor, a second `board ack`)
  between the render and the keystroke is reported, never a traceback:
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
    `task list`, the TUI, the manager digest — skips it
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
  show` and the TUI badge it (`✔` merged, `⊘` closed
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
- `quorum task add <project> -` reads the prompt from stdin, byte-for-byte,
  so a piped GitHub issue is stored exactly as it arrived — and a prompt
  kept in a file goes in as `quorum task add <project> - < file`. Empty
  input is refused. Makes the
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
- Issue intake: `quorum task add <project> --issue <number|url>` fetches an
  issue's title and body through `gh`, composes them (plus the issue URL)
  into the prompt, and records `issue_url` on the task — so `task list`,
  the TUI and the manager's digest all show `issue=#62`,
  `task show` prints the full URL, and the run preamble tells the harness
  which issue it is working from. A prompt given as well is appended as
  extra instructions. Unlike the manager's PR probe this fails loudly: no
  `gh`, no auth, an unknown issue or a timeout is an error naming the fix
  and nothing is queued. Every forge subprocess now lives in one new
  module, `forge.py` (`ci.py` keeps the digest half); quorum still only
  ever *reads* from a forge. (#62)
- Per-task notebook: `tasks/<id>/notes.jsonl` is the manager's notebook
  generalized to a task — same schema, tombstones, `--ttl` expiry, torn
  lines skipped — written with `quorum task remember <id> "…"` and
  `quorum task forget <id> <note>` by the task's own harness, the manager
  or you (another task or a prompt agent is refused and pointed at `task
  nudge`; a convention read off `QUORUM_ACTOR`, not a boundary), and
  rendered by the runner into every run's prompt, resumed or fresh, under
  its own byte budget with a drop count. `task show` prints it; the
  digest does not. The runner now tags a task's harness
  `QUORUM_ACTOR=task-<id>` (identity only: nothing journals or caps a
  task), so a task's `task nudge` and `board post` carry `task-<id>` as
  sender where they used to read as `user`. The default preamble gains a
  memory protocol paragraph and the manager prompt learns `task
  remember`; re-run `quorum init` to pick up never-edited copies. An
  adopted (attached) task is the exception: quorum does not compose its
  prompt, so its notebook is written but never rendered into the session —
  `task show` is the read path there. (#90)
- Handoffs: `quorum task report <id> --status done --handoff <file|->`
  stores a body for the tasks that depend on this one — what changed,
  what is not done, what to check first — whole and atomically at
  `tasks/<id>/handoff.md` (one per task, a later `--handoff` replaces it,
  an empty one is refused). A dependent's prompt gains a `## Handoff from
  <id>` section per upstream that left one, cut at 8 KiB per dependency
  with a note on what was dropped; `task show` prints it in full and adds
  a `dependents:` line listing the tasks waiting on this one; the digest
  says only `handoff=true`. The task preamble tells a task with
  dependents to leave one. Quorum never writes or summarizes a handoff
  itself. (#92)
- Task history: `quorum task history <id>` prints one list, oldest first,
  of everything that happened to a task — queued (issue, dependencies),
  each run's start and end (exit code, reported cost, stopped by `task
  stop`, stalled, fresh session, auto-commit), every report, guidance sent
  to it and by whom (`(waiting)` / `(claimed)` until a run consumes it),
  the PR state the manager's probe recorded, every agent action journaled
  against it, and its archival by `task prune` — with `--json` for the raw
  rows. Built in `views.py` as a pure reader over the files that already
  record each fact (`task.json`, `reports.jsonl`, the inbox and message
  archive, the agents' journals, `tasks/.archive`); nothing new is written.
  It still answers for a pruned task, resolved out of the archive. The same
  list is the TUI's `t` tab on a task; the
  tab is a snapshot rather than a follower, since building it is too much
  work for the two-second tick — `r` rebuilds it. (#95)
- `quorum usage [--by project|harness|week|agent] [--since 7d] [--json]`:
  the report that used to be a hand-written script over `task.json` files.
  Rows of tasks, runs, reruns, cost and tokens (the harness's own figures,
  summed with `usage.py`'s rules; a task that reported nothing is counted,
  never estimated, and a harness that reports tokens but no cost gets an
  empty cost cell), and — where the manager recorded a `pr_state` — the
  delivery figures: median queue-to-first-run, queue-to-done,
  done-to-merged and the share merged over the PRs it observed. A Rich
  table on a terminal, plain text piped; a pure reader over `task.json`,
  `reports.jsonl` and the agent ledgers, no cache. (#96)
- Task export: `quorum task export <id> [--out <path>]
  [--with-worktree-diff] [--redact]` packs one task into a `.tar.gz` for
  sharing or a bug report — `tasks/<id>/` whole (record, reports,
  transcript, runner log, any subdirectory; never `runner.lock`), the
  task's inbox (waiting, claimed, and already-delivered guidance read back
  out of `messages/archive/`), an `export.json` manifest, and optionally
  `worktree.diff`, the worktree against the branch it forked from with
  untracked files included. Nothing from the project directory: the diff
  is refused for a `--no-worktree` or adopted task. Read-only apart from
  the archive, which defaults to the current directory and is refused
  inside the home or over an existing file; an ambiguous id is refused
  like everywhere else. `--redact` replaces every tool result in the
  archived transcript with a marker (claude and codex shapes), keeping
  assistant text and tool calls, and says how many plain-text lines it
  could not classify. A new `export.py` holds the reader. (#98)

### Changed
- Docs restructured; one name per concept; glossary added (#102). The guide
  opens with a five-step path (install, register a project, queue a task,
  start the supervisor, read status), then Watching and Steering, then a
  `## Reference` half that begins with a 33-entry glossary fixing one name
  per idea — guidance (not nudge/directive), escalation, archive, journal,
  usage log, transcript, digest, attached, working directory vs worktree,
  overlay, note, agent / manager / prompt agent / plugin agent, dial. Those
  words are now used consistently in `docs/guide.md`, `docs/architecture.md`,
  `README.md`, `CLAUDE.md`, `--help` text, `status --legend` and the packaged
  prompts; no command, option, config key, file name or Python name changed.
  `README.md` is the five-step path plus links, `docs/architecture.md` keeps
  the design record without the passages that only narrated how it got there,
  and `CLAUDE.md` is one short paragraph per layer.
- `--home` is one option on the root command and goes **before** the
  subcommand: `quorum --home /path/to/home task list`. It used to be
  declared on 54 of the 55 commands — 54 of 172 option declarations for one
  path — and `quorum task list --home /path` is now an unknown option.
  `$QUORUM_HOME` resolution is unchanged, the root option is still exported
  into the environment, and every process quorum spawns is still handed the
  resolved home. (#102)
- The marks on a task row are rendered in one place (`views.task_marker`,
  `task_badges`, `task_flags`, `usage_badge`) and every surface uses them,
  so the CLI table and the TUI no longer disagree. In the TUI a task's
  liveness mark moved from the status cell to the front of its id, the way
  `task list` has always shown it, and a blocked dependency reads
  `waiting-on <ids>` there rather than an hourglass — with `DEP-CYCLE` and
  the dependency ids it used to leave out. `quorum status --legend`
  describes exactly that set of glyphs. (#102)
- One window grammar behind every window option: a positive count and one
  of `s m h d w`. `quorum usage --since 30s` and `board read --since 2w`
  are now accepted (each used to refuse one of the units the other took),
  and a malformed window is refused as a bad option (exit 2) wherever it
  is given — `usage --since` used to exit 1, and `board read --since`,
  `board clear --before` and `task prune --older-than` used to end a window
  too large to subtract from now (`142857142w`) in an OverflowError
  traceback. (#102)
- `quorum manager remember` from inside a task run is now refused. The
  runner used to strip the launcher's actor tag and set nothing in its
  place, so a task harness ran as `user` and the manager's notebook
  admitted it; the harness is now tagged `QUORUM_ACTOR=task-<id>` and the
  refusal points it at `quorum task report` and `quorum board post
  attention`, which is how a task was always meant to reach the manager
  (`quorum task nudge` is the same pointer on a task's own notebook). A
  task's own notebook
  (`quorum task remember`) is unaffected. (#90)
- `quorum init` recognizes a never-edited prompt seed by a record in the
  home (`prompts/.seeded.json`: the sha256 of what init last wrote, kept
  up to date by init alone) instead of a list of superseded hashes in
  Python. Changing a packaged prompt no longer needs a hash appended to
  `home.py`, and a lost or malformed record classifies a differing copy as
  edited — never upgraded — so the failure direction stays "not
  overwritten".
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

### Removed
- Second spellings of commands that already existed (#102, "one way to do
  each thing"). `quorum manager log` and `quorum manager tail` are gone:
  they were aliases of `agent log manager` / `agent tail manager` — the
  manager is an agent and is read with the agent commands.
- `quorum task tail` and `quorum agent tail` are gone, merged into `task
  log` and `agent log`. The surviving command covers what `tail` did: `-n N`
  bounds the output to the last N transcript entries and `-f` follows a live
  run. With no `-n`, `task log` prints the whole transcript as before, and
  `agent log <name>` renders the run narrative (digest, transcript, journal,
  ledger) as before; `-n`/`-f` there read the transcript file directly,
  which is what a tick still running needs, and cannot be combined with
  `--run`. `--raw` keeps its byte-for-byte promise on both.
- `quorum board ack --all <topic>`, with the `--before` and `--yes` options
  that existed only for it. `board clear <topic>` is the one spelling of the
  sweep and is unchanged; `board ack` now takes one message id, `--topic`
  and `--dry-run`. The shared `_clear_topic` helper no longer takes an
  action name, since only `board clear` journals through it.
- `quorum task add --prompt-file <path>`. The positional prompt and `-` for
  stdin remain, so a file goes in as `quorum task add <project> - < file`,
  read as bytes and decoded here exactly as `--prompt-file` was.
- `quorum agent create --prompt-file` and `--prompt-text`. The prompt body
  is now the command's second argument, or `-` to read stdin — the same
  grammar `task add` uses, through the same verbatim-bytes helper, which
  also fixes `--prompt-file`'s locale-dependent decoding. `--prompt <name>`
  (reuse an existing template) is unchanged, and an agent whose template
  already resolves still needs no prompt text at all.
- The queue controls (#102). `quorum task hold`, `task release` and
  `task set-priority`, the `task add --priority` option, the `priority` and
  `held` fields on `task.json`, the digest's `priority=` / `held=true`
  marks and the hold clauses in `prompts/manager.md`, the runner's held
  refusal (`--force` keeps its other three: attached task, unfinished
  dependencies, a last run over budget), the `⏸` / `↑N` / `↓N` badges in
  `quorum status`, `task list`, `task show` and the TUI, and the TUI keys
  `h`, `+` and `-`. Evidence: in 30 days of the dogfood home the three
  commands were never called and no task among the 33 had a non-default
  `priority` or `held`. Ordering stays where the design already put it —
  the manager's judgement from the digest, steered by
  `quorum manager tell` — and `task add --after <id>` remains the one
  ordering constraint the substrate enforces.
- The web dashboard (#102). `quorum web`, the `web` optional-dependency
  extra (fastapi, uvicorn), `src/quorum/web/` and its thirteen HTTP routes
  are gone. The terminal dashboard is the one dashboard: `quorum tui` has
  nudge, manager directive, run, cancel and attention-ack, `quorum status [--json]` is the one-shot read of the same
  model, and `quorum task history <id>` is the per-task list the web task
  page carried. What the browser could do and the TUI cannot is on the CLI:
  `quorum agent create`, `agent pause|resume|run-now|reload`, `project set`
  and `board post`. Evidence for
  the removal: the extra was never installed in the dogfood home and none
  of the thirteen routes was ever exercised. Quorum now opens no ports at
  all, which invariant 1 says outright.
- The `llm/` package and the `[llm]` config table. It provided plugin agents
  with a small-completion client (`LLMClient.complete()`, a `cli` backend
  shelling out to a configured executable, and a `proxy` backend that was a
  raising stub); nothing in shipped code called it, and the sandbox's
  `sandboxed_exec` leg — the `/bin/sh` stdin-staging hop and the
  `state/llm/` staging directory — existed only to run it. `AgentContext.llm`
  is gone with it. A plugin agent that wants a model call now runs a harness
  itself: `quorum.agents.harness_run.run_agent_harness(ctx, prompt)`, the
  same function the manager and prompt agents use, which resolves the
  agent's `[harness.*]` table, applies the per-run action cap and streams
  the run to the agent's transcript. The shipped `examples/steward.py` lost
  its optional classification of unmatched files; it reports them on the
  board and leaves them in place, as it already did without an LLM
  configured.
- `sandbox.build_capabilities` no longer opens the network for a configured
  `[llm]` executable, and no longer grants that executable read access. It
  now blocks the network unless `[sandbox].profile_file` lists a non-empty
  `network`. This is the mode-2 capability set (`quorum up --self-sandbox`),
  which applies to the supervisor and every child it spawns, so a
  harness-driven manager under mode 2 needs that grant. Task runs are
  unaffected: `build_task_capabilities` leaves the network open as before.

### Fixed
- A `runner.lock` holding valid JSON that is not an object (hand-edited, or
  truncated and refilled) no longer fails the manager tick. The liveness and
  stall readings called `.get()` / `["started_at"]` on whatever the file
  held and caught neither the AttributeError nor the TypeError that
  followed, so one bad lock raised out of every digest build until someone
  deleted the file. Every state-file read now goes through
  `fsio.read_json_or` (a dict or the caller's default, never a raise) and
  every lock read through `fsio.read_pid` (an int pid or None).
- `quorum task history`, the TUI history tab and the web task detail no
  longer fail over a corrupt message archive. The archive scan caught
  `gzip.BadGzipFile` and EOFError but not `zlib.error`, which is what gzip
  raises when the damage is inside the compressed data rather than at its
  start or end. Both scanners of the archive are now one function
  (`MessageBus.archived_records`), so the views and `task export` cannot
  disagree about what a damaged month means.
- Reading a pid out of a lock file is one function instead of five
  hand-written try/except blocks that disagreed about which exceptions to
  catch: `task inbox`, `tasks.runner_alive` (which the views, the digest and
  doctor all call) and `task cancel --kill` each read the record inside the
  try and used it outside, so a lock that was not an object raised past the
  handler.
- An out-of-range cron field (`schedule = "cron 99 * * * *"`) is rejected
  when the config is loaded instead of raising out of `quorum up`. The
  schedule pattern only counted five fields; the expression is now handed to
  APScheduler's own parser at validation time, so a bad `agents/<name>.toml`
  is one named config error rather than a supervisor that will not start.
- Notebook fixes from the review of the task-notebook change: a
  `tasks/<id>/notes.jsonl` that cannot be read at all — a directory, or a
  file the run has no permission for — failed every run of that task inside
  prompt composition, before the harness was spawned and so with no run
  record to say why; it now reads as an empty notebook, like every other
  fail-soft read. A manager configured under another name
  (`[agents.boss] type = "manager"`) was refused every `quorum task
  remember`, because a task's notebook admitted the extra writer by the
  literal name "manager" while the harness is tagged with the agent's own
  name; the extra writer is now every agent whose configured type is
  `manager`. The notebook's byte budget counted characters, so a notebook
  written in a non-Latin script was handed up to three times the budget it
  names; it counts UTF-8 bytes. `quorum task show` dropped the notebook's
  header line, which its own comment said it kept, and printed no
  `task remember` hint for a notebook whose live notes had all fallen
  outside the read window. `quorum task remember` on an attached task said
  "every future run reads it", which is not true of an adopted session —
  nothing renders a notebook into one — and now says where the notes are
  read instead. An agent could be named `task-` exactly: the name check
  read it as an agent name rather than as the task namespace it collides
  with.
- Six review leftovers from the package (#81): a PR still `open` is no
  longer recorded onto a live task's `task.json` — the one file its own
  runner is concurrently writing, and a state no surface renders — while a
  merge, which every surface badges, is recorded wherever it is seen; a task
  already recorded `merged` is never probed again; `$! GATED` now renders in
  the TUI, not only in `task list`; `quorum task add
  <slug> -` validates the project,
  harness and `--after` ids *before* draining stdin, so a typo no longer eats
  a piped issue (and says so when `-` is typed at a terminal); `task prune`
  no longer refuses a `--no-worktree` task over unrelated dirt in the user's
  own checkout; the TUI's `a` list carries every escalation the banner
  counts, so each one can be acked; and `quorum down` asks an in-flight
  notification drain to stop after the message it is delivering instead of
  waiting for the whole batch.
- A stream-json run whose nudge was answered *inside* the turn already
  running never ended (#109). The guidance pump expected one `result` event
  per delivered turn, but a CLI that drains queued input into the turn in
  flight emits one result for both, so the close condition was never met:
  stdin stayed open on an idle harness and `runner.lock` stayed held on a
  task that had reported done — 35 minutes, until someone ran `task stop`.
  The pump now tracks whether a turn is in flight rather than counting
  deliveries, and closes stdin at the first result that leaves no turn open
  and nothing waiting in the inbox.
- The guidance pump could close a stream-json harness's stdin with a nudge
  in flight: a message was claimed (renamed out of `new/`) before it was
  counted as delivered, so a `result` event landing in that gap saw an
  idle run and ended it — the nudge bounced back to `new/` and the run
  recorded one result event instead of two (a rare CI flake). The claim
  and the count now happen under the same lock the close check takes.

### Upgrading
- Command spellings that changed, old to new:
  `quorum task tail X` → `quorum task log X -n 25`;
  `quorum task tail X -f` → `quorum task log X -f`;
  `quorum manager log` → `quorum agent log manager`;
  `quorum manager tail -f` → `quorum agent log manager -f`;
  `quorum agent tail X -f` → `quorum agent log X -f`;
  `quorum board ack --all T` → `quorum board clear T`;
  `quorum task add P --prompt-file F` → `quorum task add P - < F`;
  `quorum agent create N --prompt-text "..."` → `quorum agent create N "..."`;
  `quorum agent create N --prompt-file F` → `quorum agent create N - < F`.
  Nothing is deprecated in place: the old spellings exit 2 with a usage
  error. `quorum init` reseeds `prompts/manager.md` and
  `prompts/babysitter.md`, which named `task tail`; an edited copy is left
  alone, so change `task tail <id> -n 40` to `task log <id> -n 40` there
  yourself.
- `quorum agent run-now` and `quorum agent run-once` both stay: they are two
  mechanisms, not two spellings. `run-now` sends a message to a running
  `quorum up` and returns before the tick does; `run-once` builds the agent
  in your shell and runs the tick in the foreground, which works with the
  supervisor stopped. Each command's help now says which to reach for.
- `priority` and `held` keys in existing `tasks/<id>/task.json` files are
  ignored: the model drops unknown fields, so an old record loads as an
  ordinary task and the next write leaves the keys out. Nothing needs
  editing by hand. `quorum task hold`, `quorum task release` and
  `quorum task set-priority` now exit with an unknown-command error, and
  `quorum task add --priority` with an unknown-option error.
- The web dashboard is gone, so reinstall without the extra:
  `uv tool install quorum-orchestrator` (or `pip install
  quorum-orchestrator`). `quorum-orchestrator[web]` no longer resolves;
  fastapi and uvicorn are no longer pulled in. Any `[web]` key left in a
  `config.toml` is ignored — quorum never read one, and nothing warns.
  Use `quorum tui`, `quorum status` and `quorum task history <id>` instead.
- A `[llm]` table left in config.toml is **ignored**, not rejected: the
  config model does not forbid unknown tables, and that behaviour is
  unchanged. Nothing reads the table, so delete it when convenient. If a
  plugin agent of yours called `ctx.llm.complete()`, it will now raise
  `AttributeError` — rewrite it against
  `quorum.agents.harness_run.run_agent_harness`.
- Prompt seeds are now recognized by `prompts/.seeded.json`, which the
  first `quorum init` on this version writes for every prompt copy that
  matches the packaged default. A copy that is an *older* unedited seed at
  that moment is not recognized (the superseded-hash list is gone) and is
  reported as edited: run `quorum init` on the previous version first, or
  delete the file and re-run `quorum init` to reseed it.
After installing, in each `QUORUM_HOME`:

1. `quorum init` — both `manager.md` (merged/closed PRs, the budget gate,
   self-observations, overlaps, the hung-session ladder: eighteen rules now)
   and `task-preamble.md` (rebase before push, plus the `{issue}` slot)
   changed. A copy you never edited is upgraded in place, including copies
   seeded from any intermediate 0.2.x main; an edited one is left alone — move
   house rules into `prompts/<name>.local.md` and delete the edited copy, or
   `quorum prompt diff <name>` shows the gap.
2. `quorum down && quorum up` — the manager tick and the new `_notify` job
   run inside the supervisor process, which keeps the old code until
   restarted. Detached task runs are unaffected.
3. Optionally add a `[notify]` table (`docs/guide.md#getting-notified`) and
   `quorum notify test "hello"` to prove it.

No file migrates: `task.json` gains `pr_state` fields only when the manager
next observes a PR and `issue_url` only on a task queued with `--issue`,
`state/notify.json` appears on the first drain, and older `usage.jsonl`
lines read back with an unknown outcome.

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
