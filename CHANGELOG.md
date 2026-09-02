# Changelog

All notable changes to quorum are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may change
behaviour, patch bumps are fixes only).

The PyPI distribution is `quorum-orchestrator`; the CLI and import name are `quorum`.

## [Unreleased]

### Added

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
