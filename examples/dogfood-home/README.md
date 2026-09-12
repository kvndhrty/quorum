# The dogfood home: quorum building quorum

This is the working `QUORUM_HOME` that builds quorum itself, minus the state
(tasks, worktrees, message board, journals — all machine-written, none of it
worth copying). What is left is the part a person wrote: one config, two
prompt overlays, and the loop below.

It is a *worked example*, not a starter template with blanks to fill in. It
runs one project, one harness and one agent, which is the smallest shape in
which the whole thing is visible.

```
config.toml                        the harness, the manager's hour, the notify hook
prompts/manager.local.md           the house rules: two at a time, oldest first, the human owns PRs
prompts/task-preamble.local.md     the delivery conventions every task is held to
```

## The loop

```bash
quorum init                              # scaffold ~/.quorum, then replace
                                         # config.toml with the one next to this file
cp prompts/*.local.md ~/.quorum/prompts/ # the overlays; init never touches these
quorum project add ~/src/quorum          # slug: quorum
quorum up --detach                       # one process; `quorum down` stops it

# then, for each piece of work:
gh issue create ...                      # file the issue (a person decides what)
quorum task add quorum --issue 64        # queue it — title and body become the prompt
                                         # (the manager launches it, two at a time)
quorum status                            # or `quorum tui` for the live version
gh pr merge 131 --squash                 # review and merge; PRs are yours, not the manager's
quorum task prune --worktrees            # archive what finished, reclaim the worktrees
```

That is the whole cycle. You file issues and merge PRs; everything between
those two is the manager's. `quorum up` stays running across cycles — the
queue is the plan, and adding an issue to it is the only scheduling there is.

A few things that are easy to miss:

- **Nothing starts until the supervisor is up.** `quorum task add` queues;
  the manager launches. `quorum task run <id> --detach` starts one yourself.
- **Steering is mid-flight.** `quorum task nudge <id> "..."` reaches a running
  task on this harness (`inject = "stream-json"`), and
  `quorum manager tell "..."` reaches the manager at its next tick — the house
  rule about launch order names a user directive as its one exception.
- **The board is where it asks for help.** `quorum board read attention`, or
  uncomment `[notify]` in config.toml and let it find you.
- **Prune when a cycle ends, not before.** `task prune` refuses a task whose
  worktree holds uncommitted or unpushed work, so merging first is what makes
  it a no-op decision.

## What it costs

From this home's own ledger, over the 0.2.0 development cycle, with workers on
`--model opus`:

- **$3–$12 per issue-sized task**, most of them $3–$8. An issue like "move
  `task show` into the shared read model" — one new view function, a CLI and
  JSON surface, tests, docs, changelog — came in at $6.52 and 8.0M tokens.
- **$66 for eighteen queued tasks**, twelve of them finished, over nine days.
- **The hourly manager tick is the cheap part**: 15 ticks for $3.33, a median
  of 15 seconds each. It reads a digest and spends a few actions, not a work
  session.

Where those come from: `quorum usage --by project` and `--by agent` (spend per
project, harness, ISO week or agent) and the `usage` column of
`quorum task list` (spend per task), all of which copy the figures the harness
CLI reports and price nothing themselves. On a subscription plan that number
is the CLI's notional API-rate cost, not an invoice — read it as relative
spend. A harness that
reports no usage shows nothing rather than `$0.00`, so a mixed-harness home
has fewer tasks behind its `$` than behind its token count.

The cap that keeps this bounded is not a budget, it is the two-at-a-time house
rule plus the hourly tick. If you want a hard one, set `max_cost_per_run`
under `[tasks]`: a task whose last run went over is refused its next run until
you `--force` it.

## What this example leaves out

The real home also runs an A/B harness on a second model, an unrelated
mail-triage agent, and their prompts. None of that is the quorum-builds-quorum
loop, so none of it is here. Two omissions worth naming:

- **No sandbox.** These runs need the network (the model API, `gh`, `uv`) and
  the isolation that matters is the worktree. See
  [Sandboxing](../../docs/guide.md#sandboxing) for the homes where that is the
  wrong trade.
- **No second project.** Everything here is home-wide. The moment a second
  repository joins, the test commands in `task-preamble.local.md` belong in
  that repository instead, at `.quorum/task-preamble.local.md` — see
  [Prompts and overlays](../../docs/guide.md#prompts-and-overlays).

`tests/test_example_home.py` loads this config and renders these overlays on
every run of the suite, so an option or a prompt slot cannot be renamed out
from under the example.
