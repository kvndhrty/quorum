#!/usr/bin/env python3
"""Print the inventory of everything quorum exposes (issue #102).

Run from the repo root:

    uv run python scripts/surfaces.py

One markdown table per surface class — CLI commands, CLI options and
arguments, config keys, the home layout, TUI key bindings, prompt
placeholders, doctor checks, digest markers, guide H2 word counts — each
followed by a count line, and a summary table at the end. Everything is read
from the code rather than the docs: typer's command tree, the pydantic config
model, the path helpers, and the AST of the TUI, doctor and manager modules.

The first three classes come from `quorum.surfaces`, the same module
`quorum doctor` counts with, so the two can never disagree.

Re-run it after any change that adds or removes a surface, and record the
counts in issue #102. Reads only; writes nothing.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "quorum"
DOCS = REPO / "docs"

sys.path.insert(0, str(SRC.parent))

from quorum import surfaces  # noqa: E402  (needs the src/ path above)

#: The #102 baseline, main at 2026-09-03. Blank where the class had no number.
BASELINE = {
    "CLI commands": "50",
    "CLI options (declarations)": "~80",
    "config keys (leaf settings)": "~48",
    "home layout entries": "~23",
    "prompt placeholders": "7",
    "guide words": "~11k",
    "architecture words": "~14k",
}


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(v: object) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "|".join([" --- "] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(out)


def section(title: str, headers: list[str], rows: list[list[str]], count_label: str) -> None:
    print(f"\n## {title}\n")
    print(md_table(headers, rows))
    print(f"\n{count_label}: {len(rows)}")


# --------------------------------------------------------------------------
# 4. Home layout: call every path helper, then scan literal `home / "..."`
# --------------------------------------------------------------------------

PLACEHOLDER = {
    "task_id": "<id>",
    "name": "<name>",
    "run_id": "<run>",
    "topic": "<topic>",
    "slug": "<slug>",
    "agent": "<name>",
    "message_id": "<message-id>",
}
FAKE_HOME = Path("/__HOME__")

HELPER_MODULES = [
    "quorum.home",
    "quorum.tasks",
    "quorum.actor",
    "quorum.notes",
    "quorum.messages",
    "quorum.prune",
    "quorum.notify",
    "quorum.agent",
    "quorum.supervisor",
    "quorum.views",
    "quorum.stats",
    "quorum.config",
    "quorum.sandbox",
    "quorum.export",
    "quorum.doctor",
    "quorum.runner",
]

TOP = {
    "config.toml",
    "supervisor.lock",
    "agents",
    "projects",
    "prompts",
    "messages",
    "state",
    "tasks",
    "worktrees",
    "logs",
    "plugins",
}
SLOT = {
    "tasks": "<id>",
    ".archive": "<id>",
    "worktrees": "<id>",
    "agents": "<name>",
    "inbox": "<name>",
    "board": "<topic>",
    "projects": "<slug>",
    "prompts": "<name>",
    "runs": "<run>",
    "archive": "<YYYY-MM>",
    "new": "<file>",
    "cur": "<file>",
}
HELPER_PREFIX = {
    "tasks_dir": "tasks",
    "task_dir": "tasks/<id>",
    "archive_dir": "tasks/.archive",
    "archived_task_dir": "tasks/.archive/<id>",
    "runs_dir": "state/agents/<name>/runs",
    "worktree_path": "worktrees/<id>",
}
#: Leaves no helper returns whole: a filename built at the write site.
DYNAMIC_LEAVES = [
    ("messages/board/<topic>/<utc>-<ULID>.json", "messages.py:post()"),
    ("messages/inbox/<name>/new/<file>.json", "messages.py:MessageBus.send()"),
    ("messages/inbox/<name>/cur/<file>.json", "messages.py:MessageBus.claim()"),
    ("messages/archive/<YYYY-MM>.jsonl.gz", "messages.py:_archive_one()"),
    ("projects/<slug>.json", "projects.py:ProjectRegistry"),
    ("logs/supervisor.log", "supervisor.py:_setup_logging()"),
    ("logs/actions.jsonl", "agent.py:AgentContext.log_action()"),
]


def home_layout() -> tuple[list[list[str]], list[str], list[str], list[str]]:
    from quorum import config as qconfig_mod
    from quorum import home as qhome
    from quorum import prompts as qprompts
    from quorum import prune as qprune
    from quorum import tasks as qtasks

    entries: dict[str, set[str]] = {}

    def add(pattern: str, source: str) -> None:
        p = pattern.lstrip("/")
        if p:
            entries.setdefault(p, set()).add(source)

    for modname in HELPER_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        short = modname.split(".")[-1] + ".py"
        for fname, fn in vars(mod).items():
            if not (fname.endswith("_path") or fname.endswith("_dir")):
                continue
            if not inspect.isfunction(fn) or fn.__module__ != modname:
                continue
            try:
                params = list(inspect.signature(fn).parameters.values())
            except (TypeError, ValueError):
                continue
            if not params or params[0].name != "home":
                continue
            args: list[object] = [FAKE_HOME]
            ok = True
            for p in params[1:]:
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                if p.name in PLACEHOLDER:
                    args.append(PLACEHOLDER[p.name])
                elif p.default is not p.empty:
                    break
                else:
                    ok = False
                    break
            if not ok:
                continue
            try:
                result = fn(*args)
            except Exception:
                continue
            if not isinstance(result, Path):
                continue
            try:
                rel = result.relative_to(FAKE_HOME)
            except ValueError:
                continue
            add(str(rel), f"{short}:{fname}()")

    add(qhome.CONFIG_NAME, "home.py:DEFAULT_CONFIG")
    for sub in qhome.SUBDIRS:
        add(sub + "/", "home.py:SUBDIRS")
    add(f"prompts/{qhome.SEEDED_RECORD}", "home.py:SEEDED_RECORD")

    comp_re = re.compile(r"f?\"([^\"]+)\"|([A-Za-z_][A-Za-z_0-9.]*)")
    fallbacks = (qhome, qtasks, qprune, qconfig_mod, qprompts)

    def resolve_components(chunk: str, mod) -> list[str]:
        comps: list[str] = []
        for lit, var in comp_re.findall(chunk):
            if lit:

                def _slot(m3):
                    nm = m3.group(1).split(".")[-1]
                    if nm.isupper():
                        for m2 in (mod, *fallbacks):
                            cand = getattr(m2, nm, None) if m2 is not None else None
                            if isinstance(cand, str):
                                return cand
                    return "<x>"

                comps.append(re.sub(r"\{([A-Za-z_][A-Za-z_0-9.]*)[^}]*\}", _slot, lit))
                continue
            leaf = var.split(".")[-1]
            value = None
            if mod is not None and leaf.isupper():
                cand = getattr(mod, leaf, None)
                value = cand if isinstance(cand, str) else None
            if value is None and leaf.isupper():
                for m2 in fallbacks:
                    cand = getattr(m2, leaf, None)
                    if isinstance(cand, str):
                        value = cand
                        break
            comps.append(value if value else f"<{leaf}>")
        return comps

    chain = re.compile(
        r"(?:Path\(\s*)?(?:self\.)?\b(?:home|_home)\b\s*\)?\s*"
        r"((?:/\s*(?:f?\"[^\"]+\"|[A-Za-z_][A-Za-z_0-9.]*)\s*)+)"
    )
    helper_chain = re.compile(
        r"\b(\w+_(?:dir|path))\(\s*home[^()]*\)\s*"
        r"((?:/\s*(?:f?\"[^\"]+\"|[A-Za-z_][A-Za-z_0-9.]*)\s*)+)"
    )
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "__main__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        modname = "quorum." + str(path.relative_to(SRC).with_suffix("")).replace("/", ".")
        modname = modname.removesuffix(".__init__")
        try:
            mod = importlib.import_module(modname)
        except Exception:
            mod = None
        for m in chain.finditer(text):
            add("/".join(resolve_components(m.group(1), mod)), path.name)
        for m in helper_chain.finditer(text):
            prefix = HELPER_PREFIX.get(m.group(1))
            if prefix:
                comps = resolve_components(m.group(2), mod)
                add("/".join([prefix, *comps]), f"{path.name}:{m.group(1)}()")

    for pattern, src in DYNAMIC_LEAVES:
        add(pattern, src)

    def canon(pat: str) -> str:
        out: list[str] = []
        for seg in pat.rstrip("/").split("/"):
            if "<" in seg:
                slot = SLOT.get(out[-1] if out else "", "<name>")
                suffix = re.search(r"(\.[A-Za-z][A-Za-z0-9.]*)$", seg)
                out.append(slot + (suffix.group(1) if suffix else ""))
            else:
                out.append(seg)
        return "/".join(out)

    canoned: dict[str, set[str]] = {}
    for pat, srcs in entries.items():
        if pat.split("/")[0] in TOP:
            canoned.setdefault(canon(pat), set()).update(srcs)
    entries = canoned

    def is_bare_prefix(cand: str, require_helper: bool) -> bool:
        srcs = entries[cand]
        if any(s.endswith("SUBDIRS") or (require_helper and "()" in s) for s in srcs):
            return False
        c = cand.rstrip("/")
        return any(other.rstrip("/").startswith(c + "/") for other in entries if other != cand)

    for cand in [c for c in list(entries) if is_bare_prefix(c, False)]:
        del entries[cand]
    for cand in [c for c in list(entries) if is_bare_prefix(c, True)]:
        del entries[cand]
    for cand in [c for c in list(entries) if re.fullmatch(r"<[^>]+>", c.rstrip("/"))]:
        del entries[cand]
    for cand in [c for c in list(entries) if c.endswith("/") and c.rstrip("/") in entries]:
        entries[cand.rstrip("/")] |= entries.pop(cand)

    # The doc side: the layout code block under architecture.md's "## QUORUM_HOME".
    doc_entries: list[str] = []
    arch = (DOCS / "architecture.md").read_text(encoding="utf-8")
    m = re.search(r"## QUORUM_HOME\n(.*?)\n## ", arch, re.S)
    block = ""
    if m:
        fences = re.findall(r"```\n(.*?)```", m.group(1), re.S)
        block = fences[0] if fences else ""
    for line in block.splitlines():
        if not line or line.startswith(" "):
            continue
        head = line.split()[0]
        listed = re.match(r"^([^ ]+(?:,\s*[^ ]+)*)", line.strip())
        if listed and "," in listed.group(1):
            base = head.rstrip(",").rsplit("/", 1)[0]
            for piece in listed.group(1).split(","):
                piece = piece.strip()
                if piece:
                    doc_entries.append(piece if "/" in piece else f"{base}/{piece}")
        else:
            doc_entries.append(head.rstrip(","))
    doc_entries = [d for d in doc_entries if d and not d.startswith("(")]

    def norm(p: str) -> str:
        p = re.sub(r"<[^>]+>", "<x>", p.strip().rstrip("/"))
        p = re.sub(r"\*|\{[^}]*\}", "<x>", p)
        p = p.replace("YYYY-MM.jsonl.gz", "<x>").replace("new|cur", "<x>")
        p = re.sub(r"<x>[.-]?[A-Za-z0-9.]*", "<x>", p)
        return re.sub(r"/<x>(/<x>)+", "/<x>", p)

    doc_norm = {norm(d) for d in doc_entries}
    code_norm = {norm(c) for c in entries}
    only_code = sorted(c for c in entries if norm(c) not in doc_norm)
    only_doc = sorted(d for d in doc_entries if norm(d) not in code_norm)
    rows = [
        [pat, ", ".join(sorted(srcs)[:2]), "yes" if norm(pat) in doc_norm else "NO"]
        for pat, srcs in sorted(entries.items())
    ]
    return rows, doc_entries, only_code, only_doc


# --------------------------------------------------------------------------
# 5. TUI bindings
# --------------------------------------------------------------------------


def tui_bindings() -> list[list[str]]:
    tree = ast.parse((SRC / "tui" / "app.py").read_text(encoding="utf-8"))
    rows: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if "BINDINGS" not in [t.id for t in stmt.targets if isinstance(t, ast.Name)]:
                continue
            for elt in getattr(stmt.value, "elts", []):
                try:
                    key, action, desc = ast.literal_eval(elt)
                except Exception:
                    continue
                rows.append([node.name, key, action, desc])
    return rows


# --------------------------------------------------------------------------
# 6. Prompt placeholders
# --------------------------------------------------------------------------


def prompt_placeholders() -> tuple[list[list[str]], list[list[str]]]:
    per_file: dict[str, set[str]] = {}
    for path in sorted((SRC / "default_prompts").glob("*.md")):
        # {{escaped}} is documentation, not a slot.
        stripped = re.sub(r"\{\{[^}]*\}\}", "", path.read_text(encoding="utf-8"))
        per_file[path.name] = set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", stripped))
    names = sorted({n for s in per_file.values() for n in s})
    rows = [[n, ", ".join(sorted(f for f, s in per_file.items() if n in s))] for n in names]

    supplied: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if label not in ("render", "prompt"):
                continue
            const = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if not const and not node.keywords:
                continue
            template = const[0] if const else "<runtime name> (prompt agents)"
            supplied.setdefault(template, set())
            for kw in node.keywords:
                if kw.arg:
                    supplied[template].add(kw.arg)
    supplied_rows = [[t, ", ".join(sorted(k)) or "(none)"] for t, k in sorted(supplied.items())]
    return rows, supplied_rows


# --------------------------------------------------------------------------
# 7. Doctor checks and digest markers
# --------------------------------------------------------------------------


def doctor_checks() -> list[list[str]]:
    tree = ast.parse((SRC / "doctor.py").read_text(encoding="utf-8"))
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname not in ("ok", "problem", "na", "Check"):
            continue
        arg = node.args[0] if node.args else None
        for kw in node.keywords:
            if kw.arg == "name":
                arg = kw.value
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            label = arg.value
        elif isinstance(arg, ast.JoinedStr):
            label = "".join(
                str(v.value) if isinstance(v, ast.Constant) else "<var>" for v in arg.values
            )
        else:
            continue
        names.setdefault(label, set()).add(fname)
    rows = [[n, ", ".join(sorted(s))] for n, s in sorted(names.items())]
    try:
        from quorum import dials

        rows += [[f"dial.{d.key}", "na (informational)"] for d in dials.DIALS]
    except Exception:  # pragma: no cover - dials is always importable
        pass
    return rows


def digest_markers() -> list[list[str]]:
    tree = ast.parse((SRC / "agents" / "manager.py").read_text(encoding="utf-8"))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            literals.append(
                "".join(v.value if isinstance(v, ast.Constant) else "<v>" for v in node.values)
            )
    upper: Counter[str] = Counter()
    kv: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    for lit in literals:
        for m in re.findall(r"\b([A-Z][A-Z]+(?:-[A-Z]+)+)\b", lit):
            upper[m] += 1
        for m in re.findall(r"\b([a-z][a-z_]*)=(?:<v>|[a-z0-9]+)", lit):
            kv[m + "="] += 1
        for m in re.findall(r"^\s{2}([A-Za-z][A-Za-z-]*):", lit, re.M):
            kinds[m + ":"] += 1
    return (
        [[k, "flag/marker", str(v)] for k, v in sorted(upper.items())]
        + [[k, "field", str(v)] for k, v in sorted(kv.items())]
        + [[k, "detail line", str(v)] for k, v in sorted(kinds.items())]
    )


# --------------------------------------------------------------------------
# 8. Guide and architecture headings
# --------------------------------------------------------------------------


def doc_sections(path: Path) -> tuple[list[list[str]], int]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_fence = False
    heads: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            heads.append((len(m.group(1)), m.group(2).strip(), i))
    rows: list[list[str]] = []
    for idx, (level, title, start) in enumerate(heads):
        if level == 2:
            end = len(lines)
            for h in heads[idx + 1 :]:
                if h[0] <= 2:
                    end = h[2]
                    break
        else:
            end = heads[idx + 1][2] if idx + 1 < len(heads) else len(lines)
        words = len(re.findall(r"\S+", "\n".join(lines[start:end])))
        rows.append(["#" * level, "  " * (level - 1) + title, str(words)])
    return rows, len(re.findall(r"\S+", text))


def main() -> None:
    commands, params = surfaces.cli_tree()
    cfg = surfaces.config_keys()
    counts = surfaces.counts()
    opts = [p for p in params if p["kind"] == "option"]
    args = [p for p in params if p["kind"] == "argument"]

    print("# Surface inventory — quorum (issue #102)\n")
    print(
        "Generated by `scripts/surfaces.py` from the code. Re-run it after any change\n"
        "that adds or removes a surface, and record the counts in issue #102."
    )

    section(
        "1. CLI commands",
        ["command", "kind", "params", "opts", "args", "help panel", "first help line"],
        [
            [c["path"], c["kind"], c["params"], c["opts"], c["args"], c["panel"], c["help"]]
            for c in commands
        ],
        "commands and groups",
    )
    print(f"commands: {counts['commands']}; groups: {counts['groups']}")
    home_on = sum(1 for o in opts if "--home" in o["decl"])
    json_on = sum(1 for o in opts if "--json" in o["decl"])
    print(f"`--home` is declared on {home_on} commands; `--json` on {json_on}.")

    section(
        "2. CLI options and arguments",
        ["command", "kind", "declaration", "type", "required", "default", "help"],
        [
            [p["command"], p["kind"], p["decl"], p["type"], p["required"], p["default"], p["help"]]
            for p in params
        ],
        "parameter declarations",
    )
    print(
        f"options: {len(opts)} declarations, {len({o['decl'] for o in opts})} distinct "
        f"spellings; positional arguments: {len(args)}"
    )

    section(
        "3. Config keys",
        ["key", "type", "default", "required", "description"],
        [[k["key"], k["type"], k["default"], k["required"], k["description"]] for k in cfg],
        "rows",
    )
    print(f"leaf settings: {counts['config_keys']}; tables: {counts['config_tables']}")

    layout_rows, doc_entries, only_code, only_doc = home_layout()
    section(
        "4. Home layout",
        ["path pattern", "written/read by", "in architecture.md?"],
        layout_rows,
        "layout entries (code)",
    )
    print(f"layout entries in architecture.md: {len(doc_entries)}")
    print("\nIn the code, not in architecture.md's layout block:")
    print("\n".join(f"- `{p}`" for p in only_code) or "- (none)")
    print("\nIn architecture.md's layout block, not matched in the code scan:")
    print("\n".join(f"- `{p}`" for p in only_doc) or "- (none)")

    section(
        "5. TUI key bindings",
        ["screen/class", "key", "action", "description"],
        tui_bindings(),
        "bindings",
    )

    ph_rows, supplied_rows = prompt_placeholders()
    section(
        "6. Prompt placeholders",
        ["placeholder", "templates that use it"],
        ph_rows,
        "distinct placeholders",
    )
    print()
    print(md_table(["template", "keys supplied by the render call site"], supplied_rows))

    section("7a. Doctor checks", ["check name", "statuses it can return"], doctor_checks(), "checks")
    section(
        "7b. Digest markers, fields and detail lines",
        ["token", "kind", "occurrences in manager.py"],
        digest_markers(),
        "tokens",
    )

    guide_rows, guide_words = doc_sections(DOCS / "guide.md")
    arch_rows, arch_words = doc_sections(DOCS / "architecture.md")
    print("\n## 8. Guide and architecture headings\n")
    print(md_table(["level", "heading", "words"], [r for r in guide_rows if r[0] == "##"]))
    guide_h2 = sum(1 for r in guide_rows if r[0] == "##")
    arch_h2 = sum(1 for r in arch_rows if r[0] == "##")
    print(f"\nguide H2 sections: {guide_h2} ({guide_words:,} words total)")
    print(f"architecture H2 sections: {arch_h2} ({arch_words:,} words total)")

    summary = [
        ["CLI commands", str(counts["commands"]), BASELINE["CLI commands"]],
        ["CLI groups", str(counts["groups"]), "8 (implied)"],
        ["CLI options (declarations)", str(len(opts)), BASELINE["CLI options (declarations)"]],
        ["CLI options (distinct spellings)", str(len({o["decl"] for o in opts})), "—"],
        ["CLI positional arguments", str(len(args)), "—"],
        ["config keys (leaf settings)", str(counts["config_keys"]), "~48"],
        ["config tables", str(counts["config_tables"]), "—"],
        ["home layout entries (code)", str(len(layout_rows)), BASELINE["home layout entries"]],
        ["home layout entries (architecture.md)", str(len(doc_entries)), "—"],
        ["TUI key bindings", str(len(tui_bindings())), "—"],
        ["prompt placeholders (distinct)", str(len(ph_rows)), BASELINE["prompt placeholders"]],
        ["doctor checks (names)", str(len(doctor_checks())), "—"],
        ["digest markers/fields/lines", str(len(digest_markers())), "—"],
        ["guide H2 sections", str(guide_h2), "—"],
        ["guide words", f"{guide_words:,}", BASELINE["guide words"]],
        ["architecture words", f"{arch_words:,}", BASELINE["architecture words"]],
    ]
    print("\n## Summary counts vs the #102 baseline\n")
    print(md_table(["surface class", "now", "#102 baseline"], summary))
    print(f"\ndoctor reports: surfaces: {surfaces.summary_line()}")


if __name__ == "__main__":
    main()
