"""File steward: keeps watched directories (a downloads folder, a papers
inbox) organized according to user rules.

Safe by default: with apply=false (the default) it only posts proposals to
the board. With apply=true it moves files — never deletes, never overwrites
(collision gets a numeric suffix) — and records every move in
state/steward/undo.jsonl, which `quorum steward undo` replays backward.

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

from .. import fsio
from ..agent import Agent


class Steward(Agent):
    default_schedule = "every 1h"

    def tick(self) -> None:
        watch = [Path(w).expanduser() for w in self.ctx.settings.get("watch", [])]
        rules = self.ctx.settings.get("rules", [])
        apply = bool(self.ctx.settings.get("apply", False))
        state = self.ctx.load_state()
        seen = state.setdefault("seen", {})  # path -> mtime already handled

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
                if seen.get(key) == mtime:
                    continue
                dest = self._match(entry, rules)
                via_llm = False
                if dest is None and rules:
                    dest = self._classify(entry, rules)
                    via_llm = dest is not None
                if dest is None:
                    if key not in seen:
                        self.ctx.bus.post(
                            self.name, "steward", "steward.unmatched",
                            text=f"no rule matches {entry.name} (in {directory})",
                            payload={"file": str(entry)},
                        )
                    seen[key] = mtime
                    continue
                if apply:
                    moved_to = self._move(entry, dest)
                    if moved_to is not None:
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
                    seen[key] = mtime

        # forget entries whose files are gone, so state stays bounded
        state["seen"] = {k: v for k, v in seen.items() if Path(k).exists()}
        self.ctx.save_state(state)

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
        prompt = self.ctx.prompt(
            "steward-classify", filename=entry.name, destinations="\n".join(destinations)
        )
        answer = self.ctx.llm.complete(prompt)
        if answer is None:
            return None
        answer = answer.strip().splitlines()[0].strip()
        return Path(answer) if answer in destinations else None

    def _move(self, src: Path, dest_dir: Path) -> Path | None:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / src.name
            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = dest_dir / f"{stem}-{n}{suffix}"
                n += 1
            shutil.move(str(src), str(target))
        except OSError as e:
            self.ctx.bus.post(
                self.name, "steward", "steward.error",
                text=f"failed to move {src.name}: {e}", payload={"src": str(src)},
            )
            return None
        fsio.append_jsonl(
            self.ctx.home / "state" / "steward" / "undo.jsonl",
            {"at": fsio.iso(self.ctx.now()), "src": str(src), "dest": str(target)},
        )
        return target


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
