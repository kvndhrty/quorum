<!-- Appended to the run prompt of a task queued with `task add --perpetual`,
     in place of the {{perpetual}} placeholder in task-preamble.md (an
     ordinary task gets nothing here). Placeholders: {{task_id}}. Edit
     freely — this file is yours; delete it to restore the packaged
     default. -->
This is a PERPETUAL task: it is not expected to finish, and the delivery
conventions above bend accordingly.

- Work in cycles. A cycle is one useful pass over the job — look at the
  current state, do what it needs, deliver, then start the next one.
- Deliver every cycle, not at the end: commit and push (`git push -u origin
  HEAD`) as soon as a cycle produces anything worth keeping. There is no
  "before finishing", so uncommitted work is stranded work.
- Report each cycle with a *changing* status word so progress is visible:
    quorum task report {task_id} --status cycle-3 "<what this pass did>"
  Report `idle` (or similar) for a cycle that correctly found nothing to do
  — an unchanging status is what tells a supervisor something is stuck.
- Never report `done` or `cancelled`. Only the user ends this task, with
  `quorum task cancel`. Report `blocked` only if you genuinely cannot
  continue without a human, and say exactly what you need.
- Check `quorum task inbox {task_id} --claim` between cycles: guidance is
  how the job is retuned while it runs.
- If your run ends anyway (crash, timeout, a harness turn limit), that is
  fine: the manager relaunches you and the worktree still holds your work.
