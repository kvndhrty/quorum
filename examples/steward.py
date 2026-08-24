"""File steward — the worked example of a quorum plugin agent.

Keeps watched directories (a downloads folder, a papers inbox) organized
according to user rules. To use it: copy this file into
`QUORUM_HOME/plugins/` and add to config.toml:

    [agents.steward]
    type = "steward:Steward"
    schedule = "every 1h"
    [agents.steward.settings]
    watch = ["~/Downloads"]
    apply = false             # false = propose on the board; true = move files
    rules = [{ match = "*.pdf", dest = "~/papers/inbox" }]

It demonstrates the whole plugin contract: settings, idempotent ticks with
state-based dedupe, board posts, `log_action`, LLM-with-deterministic-
fallback, and bounded retries. Test it with `quorum agent run-once steward`.

Safe by default: with apply=false (the default) it only posts proposals to
the board. With apply=true it moves files — never deletes, never overwrites
(collision gets a numeric suffix) — and records every move in
state/steward/undo.jsonl, which `undo_moves()` below replays backward, e.g.
    python -c "import steward, pathlib; steward.undo_moves(pathlib.Path('~/.quorum').expanduser(), last=1)"

Per-file state records *what* was done, not just when: an unchanged file is
never re-proposed tick after tick, but flipping apply=false to apply=true
reconsiders files that were only ever proposed, so the advertised
propose-then-apply workflow actually acts on the backlog.

A move that fails is retried a bounded number of times and reported twice at
most — once when it first fails and once when the steward gives up.

Files matching no rule are left alone. If an LLM is configured, unmatched
files can be classified against the rule destinations (the reply must
exactly name a destination, otherwise the file is skipped); without one they
are reported once and left in place.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path

from quorum import fsio
from quorum.agent import Agent

CLASSIFY_PROMPT = """\
You are a file steward organizing a user's directory. Choose the best
destination for this file, or SKIP if none clearly fits.

File: {filename}

Allowed destinations (reply with exactly one line, verbatim, or SKIP):
{destinations}
"""

# A move can fail for a reason that clears on its own (a full disk, a
# destination on a volume that is briefly unmounted), so retrying is worth it —
# but the retry has to be bounded, or a permanently unwritable destination puts
# a steward.error on the board every tick for as long as the file exists.
MAX_MOVE_ATTEMPTS = 3


class Steward(Agent):
    default_schedule = "every 1h"

    def tick(self) -> None:
        watch = [Path(w).expanduser() for w in self.ctx.settings.get("watch", [])]
        rules = self.ctx.settings.get("rules", [])
        apply = bool(self.ctx.settings.get("apply", False))
        state = self.ctx.load_state()
        seen = state.setdefault("seen", {})  # path -> {"mtime": float, "action": str}

        for directory in watch:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.name.startswith(".") or not entry.is_file():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                key = str(entry)
                record = _record(seen.get(key))
                if self._settled(record, mtime, apply):
                    continue
                dest = self._match(entry, rules)
                via_llm = False
                if dest is None and rules:
                    dest = self._classify(entry, rules)
                    via_llm = dest is not None
                if dest is None:
                    if record is None:
                        self.ctx.bus.post(
                            self.name, "steward", "steward.unmatched",
                            text=f"no rule matches {entry.name} (in {directory})",
                            payload={"file": str(entry)},
                        )
                    seen[key] = {"mtime": mtime, "action": "unmatched"}
                    continue
                if apply:
                    try:
                        moved_to = self._move(entry, dest)
                    except OSError as e:
                        seen[key] = self._note_move_failure(entry, record, mtime, e)
                        continue
                    text = f"moved {entry.name} -> {moved_to}"
                    self.ctx.bus.post(
                        self.name, "steward", "steward.moved", text=text,
                        payload={"src": str(entry), "dest": str(moved_to), "via_llm": via_llm},
                    )
                    self.ctx.log_action("steward.moved", text)
                    seen.pop(key, None)
                else:
                    text = f"proposal: move {entry.name} -> {dest} (set apply=true or move it yourself)"
                    self.ctx.bus.post(
                        self.name, "steward", "steward.proposal", text=text,
                        payload={"src": str(entry), "dest": str(dest), "via_llm": via_llm},
                    )
                    self.ctx.log_action("steward.proposal", text)
                    seen[key] = {"mtime": mtime, "action": "proposed"}

        # forget entries whose files are gone, so state stays bounded
        state["seen"] = {k: v for k, v in seen.items() if Path(k).exists()}
        self.ctx.save_state(state)

    def _settled(self, record: dict | None, mtime: float, apply: bool) -> bool:
        """True when the file is unchanged *and* this mode has nothing left to do.

        A proposal only settles a file while apply is off; once apply is on the
        proposal is unfinished business and the file must be reconsidered. An
        unmatched file settles either way — no rule matched it, and re-asking the
        classifier every tick would burn an LLM call per junk file per hour. A
        failed move settles only once its retry budget is spent.
        """
        if record is None or record["mtime"] != mtime:
            return False
        action = record["action"]
        if action == "failed":
            return record.get("attempts", 0) >= MAX_MOVE_ATTEMPTS
        return not (apply and action == "proposed")

    def _note_move_failure(self, entry: Path, record: dict | None, mtime: float, error) -> dict:
        """Record a failed move, reporting it at most twice per version of a file.

        Once on the first failure, so the board shows the problem, and once when
        the budget runs out, so the silence that follows is explained rather
        than looking like the steward quietly forgot.
        """
        prior = record.get("attempts", 0) if record and record["mtime"] == mtime else 0
        attempts = prior + 1
        if attempts == 1:
            self.ctx.bus.post(
                self.name, "steward", "steward.error",
                text=f"failed to move {entry.name}: {error}",
                payload={"src": str(entry), "attempts": attempts},
            )
        elif attempts >= MAX_MOVE_ATTEMPTS:
            self.ctx.bus.post(
                self.name, "steward", "steward.error",
                text=f"giving up on {entry.name} after {attempts} attempts: {error} "
                     "(fix the destination, then touch the file to retry)",
                payload={"src": str(entry), "attempts": attempts, "gave_up": True},
            )
        return {"mtime": mtime, "action": "failed", "attempts": attempts}

    def _match(self, entry: Path, rules: list[dict]) -> Path | None:
        for rule in rules:
            pattern = rule.get("match", "")
            if pattern and fnmatch.fnmatch(entry.name, pattern):
                return Path(rule["dest"]).expanduser()
        return None

    def _classify(self, entry: Path, rules: list[dict]) -> Path | None:
        if not self.ctx.llm.enabled:
            return None
        destinations = sorted({str(Path(r["dest"]).expanduser()) for r in rules if r.get("dest")})
        prompt = CLASSIFY_PROMPT.format(
            filename=entry.name, destinations="\n".join(destinations)
        )
        answer = self.ctx.llm.complete(prompt)
        if answer is None:
            return None
        answer = answer.strip().splitlines()[0].strip()
        return Path(answer) if answer in destinations else None

    def _move(self, src: Path, dest_dir: Path) -> Path:
        """Move one file, raising OSError on failure so the caller can decide
        whether this is worth another attempt."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        stem, suffix = target.stem, target.suffix
        n = 1
        while target.exists():
            target = dest_dir / f"{stem}-{n}{suffix}"
            n += 1
        shutil.move(str(src), str(target))
        fsio.append_jsonl(
            self.ctx.home / "state" / "steward" / "undo.jsonl",
            {"at": fsio.iso(self.ctx.now()), "src": str(src), "dest": str(target)},
        )
        return target


def _record(value: object) -> dict | None:
    """Normalize a `seen` entry. State written before actions were tracked held a
    bare mtime; read it as a proposal, which is the reading that lets an upgrade
    heal itself — apply=true acts on the backlog, apply=false stays quiet."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"mtime": value, "action": "proposed"}


def undo_moves(home: Path, last: int = 1) -> list[tuple[str, str]]:
    """Replay the last N recorded moves backward. Returns (dest, src) pairs undone."""
    log_path = home / "state" / "steward" / "undo.jsonl"
    entries = fsio.read_jsonl(log_path)
    undone: list[tuple[str, str]] = []
    remaining = list(entries)
    for record in reversed(entries[-last:] if last else entries):
        dest, src = Path(record["dest"]), Path(record["src"])
        if dest.exists() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(dest), str(src))
                undone.append((str(dest), str(src)))
                remaining.remove(record)
            except OSError:
                continue
    if undone:
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in remaining)
        fsio.atomic_write_text(log_path, content)
    return undone
