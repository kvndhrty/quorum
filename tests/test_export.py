"""`quorum task export`: one archive of a task's directory.

A pure reader, so every test here checks two things — the archive holds
exactly what it should, and nothing under the home changed to make it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quorum import export, fsio
from quorum.cli import app
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry
from quorum.tasks import TaskStore, inbox_name, runner_lock_path, task_dir, worktree_path
from test_tasks import make_repo, repo_git

runner = CliRunner()


# -- fixtures and helpers -------------------------------------------------


@pytest.fixture
def repo(home: Path, tmp_path: Path) -> Path:
    path = make_repo(tmp_path, "proj")
    ProjectRegistry(home).add(path, name="proj")
    return path


@pytest.fixture
def outdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the default output lands: a cwd that is not the home."""
    d = tmp_path / "out"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def add_task(home: Path, prompt: str = "do a thing", **kwargs):
    return TaskStore(home).add("proj", prompt, "fake", **kwargs)


def add_worktree(home: Path, repo: Path, prompt: str = "edit things"):
    """A task with a real worktree, forked the way the runner does it."""
    store = TaskStore(home)
    task = store.add("proj", prompt, "fake")
    workdir = worktree_path(home, task.id)
    repo_git(repo, "worktree", "add", str(workdir), "-b", f"quorum/{task.short_id}")
    return store.update(task.id, workdir=str(workdir), status="executing")


def write_transcript(home: Path, task_id: str, events: list[dict]) -> None:
    for event in events:
        fsio.append_jsonl(task_dir(home, task_id) / "transcript.jsonl", {"at": "t", **event})


TOOL_CALL = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "reading the config"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat x"}},
        ],
    },
}
TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "SECRET=hunter2",
                "is_error": False,
            }
        ],
    },
    "tool_use_result": {"stdout": "SECRET=hunter2", "stderr": ""},
}


def members(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under a tree with its size and mtime: the "nothing
    changed" check."""
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


# -- what goes in ---------------------------------------------------------


def test_archive_lists_exactly_the_task_directory_and_inbox(home: Path, repo: Path, outdir: Path):
    task = add_task(home)
    write_transcript(home, task.id, [{"event": TOOL_CALL}])
    fsio.append_jsonl(task_dir(home, task.id) / "reports.jsonl", {"status": "planning"})
    (task_dir(home, task.id) / "runner.log").write_text("boot\n")
    # a future notebook/artifacts directory rides along; a tmp file does not
    (task_dir(home, task.id) / "artifacts").mkdir()
    (task_dir(home, task.id) / "artifacts" / "plan.md").write_text("# plan\n")
    (task_dir(home, task.id) / ".task.json.tmp").write_text("{")
    # the lock is a pid on this machine, not part of the record
    runner_lock_path(home, task.id).write_text("1")
    MessageBus(home).send("user", inbox_name(task.id), text="waiting guidance")
    before = snapshot(home)

    result = runner.invoke(app, ["task", "export", task.short_id])

    assert result.exit_code == 0, result.output
    archive = outdir / f"quorum-task-{task.short_id}.tar.gz"
    assert archive.exists()
    root = f"quorum-task-{task.short_id}"
    got = members(archive)
    new_name = fsio.sorted_entries(home / "messages" / "inbox" / inbox_name(task.id) / "new")[0].name
    assert set(got) == {
        f"{root}/export.json",
        f"{root}/task.json",
        f"{root}/reports.jsonl",
        f"{root}/transcript.jsonl",
        f"{root}/runner.log",
        f"{root}/artifacts/plan.md",
        f"{root}/inbox/new/{new_name}",
    }
    assert json.loads(got[f"{root}/task.json"])["id"] == task.id
    assert b"SECRET" not in got[f"{root}/transcript.jsonl"]  # nothing to redact yet
    manifest = json.loads(got[f"{root}/export.json"])
    assert manifest["task"] == task.id
    assert manifest["redacted"] is False and manifest["worktree_diff"] is False
    assert "task.json" in manifest["entries"]
    # a pure reader: the home is byte-for-byte what it was
    assert snapshot(home) == before
    assert not list(outdir.glob(".*.tmp"))


def test_export_carries_claimed_and_delivered_guidance(home: Path, repo: Path, outdir: Path):
    """Guidance a run already consumed lives only in the compacted
    archive; the export digs it back out, addressed to this task only."""
    task = add_task(home)
    other = add_task(home, "another")
    bus = MessageBus(home)
    bus.send("user", inbox_name(task.id), text="first, delivered")
    bus.send("manager", inbox_name(other.id), text="not for this task")
    for claimed in bus.claim(inbox_name(task.id)):
        claimed.ack()
    for claimed in bus.claim(inbox_name(other.id)):
        claimed.ack()
    bus.send("user", inbox_name(task.id), text="second, claimed mid-run")
    claimed_paths = [c.path for c in bus.claim(inbox_name(task.id))]  # claimed, never acked

    result = runner.invoke(app, ["task", "export", task.short_id])

    assert result.exit_code == 0, result.output
    root = f"quorum-task-{task.short_id}"
    got = members(outdir / f"quorum-task-{task.short_id}.tar.gz")
    assert f"{root}/inbox/cur/{claimed_paths[0].name}" in got
    delivered = [json.loads(line) for line in got[f"{root}/inbox/delivered.jsonl"].splitlines()]
    assert [d["payload"]["text"] for d in delivered] == ["first, delivered"]
    assert all(d["to"] == inbox_name(task.id) for d in delivered)


def test_delivered_guidance_skips_months_before_the_task_existed(home: Path, repo: Path):
    task = add_task(home)
    archive = home / "messages" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    import gzip

    with gzip.open(archive / "1999-01.jsonl.gz", "wt") as f:
        f.write(json.dumps({"to": inbox_name(task.id), "payload": {"text": "impossible"}}) + "\n")
    assert export.delivered_guidance(home, task) == []


# -- output path ----------------------------------------------------------


def test_export_honours_out_and_refuses_the_home_and_an_existing_file(
    home: Path, repo: Path, tmp_path: Path
):
    task = add_task(home)
    out = tmp_path / "share" / "run.tgz"
    out.parent.mkdir()

    ok = runner.invoke(app, ["task", "export", task.short_id, "--out", str(out)])
    assert ok.exit_code == 0, ok.output
    assert out.exists()

    again = runner.invoke(app, ["task", "export", task.short_id, "--out", str(out)])
    assert again.exit_code == 1
    assert "already exists" in again.output
    assert out.stat().st_size > 0  # untouched

    inside = runner.invoke(
        app, ["task", "export", task.short_id, "--out", str(home / "tasks" / "x.tar.gz")]
    )
    assert inside.exit_code == 1
    assert "inside the quorum home" in inside.output
    assert not (home / "tasks" / "x.tar.gz").exists()


def test_export_refuses_an_ambiguous_or_unknown_id(home: Path, repo: Path, outdir: Path):
    a = add_task(home, "one")
    b = add_task(home, "two")
    # ULIDs minted seconds apart share their timestamp head, so a short
    # prefix matches both
    shared = a.id[:6]
    assert b.id.startswith(shared)

    ambiguous = runner.invoke(app, ["task", "export", shared])
    assert ambiguous.exit_code == 1
    assert "ambiguous" in ambiguous.output
    unknown = runner.invoke(app, ["task", "export", "zzzzzz"])
    assert unknown.exit_code == 1
    assert "no task matching" in unknown.output
    assert not list(outdir.iterdir())


# -- the worktree diff ----------------------------------------------------


def test_worktree_diff_covers_committed_uncommitted_and_untracked_work(
    home: Path, repo: Path, outdir: Path
):
    task = add_worktree(home, repo)
    workdir = Path(task.workdir)
    (workdir / "README.md").write_text("committed change")
    repo_git(workdir, "add", ".")
    repo_git(workdir, "commit", "-qm", "work")
    (workdir / "README.md").write_text("committed change\nplus an uncommitted line")
    (workdir / "new.txt").write_text("untracked content")
    before_repo = snapshot(repo)
    before_home = snapshot(home)

    result = runner.invoke(app, ["task", "export", task.short_id, "--with-worktree-diff"])

    assert result.exit_code == 0, result.output
    root = f"quorum-task-{task.short_id}"
    got = members(outdir / f"quorum-task-{task.short_id}.tar.gz")
    diff = got[f"{root}/worktree.diff"].decode()
    assert "+committed change" in diff
    assert "+plus an uncommitted line" in diff
    assert "+untracked content" in diff
    assert "new.txt" in diff
    assert "-hello" in diff  # the base's README line
    # read-only git: nothing in the project checkout or the home moved
    assert snapshot(repo) == before_repo
    assert snapshot(home) == before_home
    manifest = json.loads(got[f"{root}/export.json"])
    assert manifest["worktree_diff"] is True


def test_worktree_diff_is_refused_loudly_when_there_is_nothing_to_diff(
    home: Path, repo: Path, outdir: Path
):
    never_ran = add_task(home)
    result = runner.invoke(app, ["task", "export", never_ran.short_id, "--with-worktree-diff"])
    assert result.exit_code == 1
    assert "never run" in result.output

    # a task that ran in the user's checkout: the diff would be of the
    # project directory, which is exactly what an export must not contain
    in_checkout = TaskStore(home).add("proj", "x", "fake", use_worktree=False, workdir=str(repo))
    result = runner.invoke(app, ["task", "export", in_checkout.short_id, "--with-worktree-diff"])
    assert result.exit_code == 1
    assert "project checkout" in result.output

    adopted = TaskStore(home).add("proj", "x", "fake", attached=True, workdir=str(repo))
    result = runner.invoke(app, ["task", "export", adopted.short_id, "--with-worktree-diff"])
    assert result.exit_code == 1
    assert "project checkout" in result.output
    # a refusal writes nothing
    assert not list(outdir.iterdir())


def test_worktree_diff_needs_a_base(home: Path, repo: Path, tmp_path: Path):
    """A worktree with no discoverable base is refused, not diffed against a
    guess."""
    task = add_worktree(home, repo)
    # detach the main checkout so `worktree list` names no branch, with no
    # remote and no upstream to fall back on
    repo_git(repo, "checkout", "-q", "--detach")
    with pytest.raises(export.ExportError, match="no base branch"):
        export.worktree_diff(task)


# -- redaction ------------------------------------------------------------


def test_redact_drops_tool_results_and_keeps_text_and_calls(home: Path, repo: Path, outdir: Path):
    task = add_task(home)
    write_transcript(
        home,
        task.id,
        [
            {"event": TOOL_CALL},
            {"event": TOOL_RESULT},
            {"event": {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}},
            {"line": "plain text from a harness that prints prose: SECRET=hunter2"},
        ],
    )

    result = runner.invoke(app, ["task", "export", task.short_id, "--redact"])

    assert result.exit_code == 0, result.output
    root = f"quorum-task-{task.short_id}"
    got = members(outdir / f"quorum-task-{task.short_id}.tar.gz")
    lines = [json.loads(line) for line in got[f"{root}/transcript.jsonl"].splitlines()]
    assert len(lines) == 4
    call = lines[0]["event"]["message"]["content"]
    assert call[0]["text"] == "reading the config"
    assert call[1]["input"] == {"command": "cat x"}  # the call and its arguments stay
    res = lines[1]["event"]
    block = res["message"]["content"][0]
    assert block["content"] == export.REDACTED
    assert block["tool_use_id"] == "toolu_1" and block["is_error"] is False
    assert res["tool_use_result"] == export.REDACTED
    assert lines[2]["event"]["message"]["content"][0]["text"] == "done"
    assert "SECRET=hunter2" in lines[3]["line"]  # plain text is kept, and said so
    assert "redacted 2 tool result(s)" in result.output
    assert "1 plain-text line(s) kept verbatim" in result.output
    assert json.loads(got[f"{root}/export.json"])["redacted"] is True
    # the transcript on disk is untouched
    on_disk = fsio.read_jsonl(task_dir(home, task.id) / "transcript.jsonl")
    assert on_disk[1]["event"]["message"]["content"][0]["content"] == "SECRET=hunter2"


def test_redact_handles_codex_shapes_and_is_pure():
    codex_call = {
        "type": "item.completed",
        "item": {
            "id": "call_1",
            "type": "command_execution",
            "command": "cat secrets.env",
            "aggregated_output": "TOKEN=abc",
            "exit_code": 0,
        },
    }
    codex_result = {"type": "function_call_output", "call_id": "call_2", "output": "TOKEN=abc"}
    entries = [{"at": "t", "event": codex_call}, {"at": "t", "event": codex_result}]
    original = json.dumps(entries)

    redaction = export.redact_transcript(entries)

    assert redaction.results == 2
    item = redaction.entries[0]["event"]["item"]
    assert item["command"] == "cat secrets.env" and item["exit_code"] == 0
    assert item["aggregated_output"] == export.REDACTED
    out = redaction.entries[1]["event"]
    assert out["output"] == export.REDACTED and out["call_id"] == "call_2"
    assert json.dumps(entries) == original


def test_redact_drops_what_is_nested_too_deep_to_read():
    node: dict = {"type": "x"}
    for _ in range(export.REDACT_DEPTH + 2):
        node = {"child": node}
    redaction = export.redact_transcript([{"event": node}])
    assert export.REDACTED in json.dumps(redaction.entries)
    assert redaction.results == 1


def test_export_without_a_transcript_still_reports_redaction(home: Path, repo: Path, outdir: Path):
    task = add_task(home)
    result = runner.invoke(app, ["task", "export", task.short_id, "--redact"])
    assert result.exit_code == 0, result.output
    assert "redacted 0 tool result(s)" in result.output


# -- failure modes --------------------------------------------------------


def test_export_leaves_no_half_archive_when_writing_fails(
    home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    task = add_task(home)
    entries, _ = export.plan(home, task)
    out = tmp_path / "broken.tar.gz"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "addfile", boom)
    with pytest.raises(OSError):
        export.write_archive(out, task, entries)
    assert not out.exists()
    assert not list(tmp_path.glob(".broken*"))


def test_task_directory_missing_is_an_error(home: Path, repo: Path):
    task = add_task(home)
    subprocess.run(["rm", "-r", str(task_dir(home, task.id))], check=True)
    with pytest.raises(export.ExportError, match="no task directory"):
        export.task_entries(home, task)
