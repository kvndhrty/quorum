# scripts/

Development scripts. Not shipped in the wheel and not imported by quorum.

- `surfaces.py` — prints the inventory of everything quorum exposes: CLI
  commands, options and arguments, config keys, the home layout, TUI key
  bindings, prompt placeholders, doctor checks, digest markers, and the
  guide's H2 sections, each with a count. Run it from the repo root with
  `uv run python scripts/surfaces.py`. Issue #102 asks for it to be re-run
  after each surface change so growth stays visible; the command, option and
  config-key counts come from `quorum.surfaces`, the module behind
  `quorum doctor`'s `surfaces` line, so the two cannot disagree.
- `evidence.py` — the other half: `uv run python scripts/evidence.py <QUORUM_HOME>`
  reads a home and prints, per surface class, what that home records as actually
  *used* — every CLI verb, option, config key, TUI binding and prompt
  placeholder, split by actor (person, manager, prompt agent, task harness),
  beside the counts `surfaces.py` reports, with a blank `verdict` column. Read
  only: it never writes to the home and never runs a quorum command against it.
  A call that named another home or went through `uv run quorum` is counted in
  its own column and never as use. Issue #128 is the review it was written for.
