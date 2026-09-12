<!-- prompts/manager.local.md — the house rules of the home that builds
     quorum, verbatim. Copy it to ~/.quorum/prompts/manager.local.md.

     This is an *overlay*, not a fork: quorum renders it into the packaged
     prompts/manager.md at that file's {local} slot, which sits near the top,
     above the general guidance. manager.md keeps receiving packaged upgrades
     because it is never edited; everything this home decided for itself is
     in these ten lines. `quorum prompt list` shows which overlays are in
     play, and `quorum manager journal` shows what the manager did with them.

     Why each rule is here:

     - Two at a time. The cap exists because of shared API rate limits, not
       because of the machine — three concurrent opus workers plus an hourly
       manager tick hit the limit, and a rate-limited run fails in a way that
       looks like a stuck task. The manager has to *count live runners*
       rather than count queued tasks, since a task can be non-terminal with
       no runner alive (crashed, stalled, waiting on a dependency). It is a
       dial, not an invariant: raise it when your limits allow.

     - Oldest-first. Without an order the manager re-derives priorities every
       tick and drifts; with one, the queue is the plan and a user directive
       (`quorum manager tell "..."`) is the only override. Quorum's substrate
       orders nothing on purpose — `--after` is the only ordering it
       enforces — so ordering policy belongs here, in prose.

     - The human owns PRs. This is the rule that keeps the loop from
       fanning out: a manager that may queue follow-up work for review
       feedback will queue three tasks per merged PR and never converge. A
       PR URL is the end of a task's life; the next phase is a new issue,
       filed by a person. -->
House rules for this home (they override the general guidance below):

- **Run at most TWO tasks at a time.** Count the tasks whose runner is alive
  before launching; never `task run` a task when two runners are already alive.
  Queued tasks wait their turn; this cap is deliberate pacing to stay under API
  rate limits — do not work around it.
- **Launch queued tasks oldest-first** unless a user directive names an order.
- These tasks implement GitHub issues on the quorum repo and finish by opening
  a draft PR. A task that reported done with a PR URL is finished — do NOT
  create follow-up tasks for its review feedback or its merge; the human
  handles PRs and queues the next phase.
