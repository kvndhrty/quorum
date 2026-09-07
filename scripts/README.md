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
