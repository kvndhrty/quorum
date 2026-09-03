"""The prompt layer: templates, and the never-seeded local overlay (#37)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quorum import prompts


def write(home: Path, name: str, text: str) -> Path:
    target = home / "prompts" / name
    target.write_text(text, encoding="utf-8")
    return target


def test_overlay_lands_in_the_local_slot(home: Path):
    write(home, "policy.md", "before\n\n{local}\n\nafter {who}\n")
    write(home, "policy.local.md", "house rules: one task at a time\n")

    assert prompts.render(home, "policy", who="you") == (
        "before\n\nhouse rules: one task at a time\n\nafter you\n"
    )


def test_overlay_is_prepended_when_the_template_has_no_slot(home: Path):
    """An edited template from before the slot existed cannot substitute a
    key it never mentions — the overlay still has to reach the harness."""
    write(home, "policy.md", "my own rewritten policy\n")
    write(home, "policy.local.md", "house rules: one task at a time\n")

    assert prompts.render(home, "policy") == (
        "house rules: one task at a time\n\nmy own rewritten policy\n"
    )


@pytest.mark.parametrize("overlay", [None, "", "   \n\n"])
def test_absent_or_empty_overlay_renders_to_nothing(home: Path, overlay: str | None):
    write(home, "policy.md", "before\n\n{local}\n\nafter\n")
    if overlay is not None:
        write(home, "policy.local.md", overlay)

    # no hole where the slot was, and no literal {local} left behind
    assert prompts.render(home, "policy") == "before\n\nafter\n"


def test_an_edited_template_still_wins_and_still_takes_its_overlay(home: Path):
    """The override path is unchanged: a home that rewrote <name>.md gets its
    own text (the packaged default is not consulted), with the overlay on
    top — so migrating to the overlay can be done one prompt at a time."""
    write(home, "manager.md", "MY MANAGER\n\n{local}\n\n{digest}\n")
    write(home, "manager.local.md", "always open draft PRs\n")

    rendered = prompts.render(home, "manager", digest="DIGEST")
    assert rendered == "MY MANAGER\n\nalways open draft PRs\n\nDIGEST\n"
    assert "You are the manager of a quorum home" not in rendered


def test_overlay_applies_to_the_packaged_default_without_a_home_copy(home: Path):
    (home / "prompts" / "manager.md").unlink()
    write(home, "manager.local.md", "house rule: two tasks at a time\n")

    rendered = prompts.render(home, "manager", digest="DIGEST")
    assert "house rule: two tasks at a time" in rendered
    assert "You are the manager of a quorum home" in rendered  # the packaged default


def test_unknown_placeholders_and_literal_braces_survive(home: Path):
    """Prompts contain literal braces (JSON shapes, {{escaped}} keys); the
    missing-key-preserving format_map must keep behaving."""
    write(home, "policy.md", "{local}\nkeep {unknown} and {{escaped}} and {task_id}\n")
    write(home, "policy.local.md", "overlay with {unknown_too} braces\n")

    assert prompts.render(home, "policy", task_id="abc") == (
        "overlay with {unknown_too} braces\nkeep {unknown} and {escaped} and abc\n"
    )


def test_escaped_local_in_a_header_comment_is_not_a_slot(home: Path):
    """The packaged prompts document the key as {{local}}; that must not
    count as the slot, or the overlay would land inside the comment."""
    write(home, "policy.md", "<!-- {{local}} is the overlay -->\nbody\n")
    write(home, "policy.local.md", "house rules\n")

    assert prompts.render(home, "policy") == (
        "house rules\n\n<!-- {local} is the overlay -->\nbody\n"
    )


def test_an_explicit_local_placeholder_beats_the_overlay_file(home: Path):
    write(home, "policy.md", "{local}\n")
    write(home, "policy.local.md", "from the file\n")

    assert prompts.render(home, "policy", local="passed in") == "passed in\n"


def test_an_undecodable_overlay_renders_as_no_overlay(home: Path):
    """render() is on the manager tick and every task run: one stray byte in
    a user-owned overlay must not fail supervision forever (review of #37)."""
    write(home, "policy.md", "before\n\n{local}\n\nafter\n")
    (home / "prompts" / "policy.local.md").write_bytes(b"house \xff\xfe rules\n")

    assert prompts.load_local(home, "policy") == ""
    assert prompts.render(home, "policy") == "before\n\nafter\n"

    # the same for the prompts that actually run
    (home / "prompts" / "manager.local.md").write_bytes(b"\xff\xfe\x00 policy")
    rendered = prompts.render(home, "manager", digest="DIGEST")
    assert "DIGEST" in rendered
    assert "You are the manager of a quorum home" in rendered


def test_an_unreadable_template_still_raises(home: Path):
    """The overlay fails soft; the template itself does not — falling back to
    the packaged default would silently hide the home's own prompt."""
    (home / "prompts" / "manager.md").write_bytes(b"\xff\xfe not utf-8\n")
    with pytest.raises(UnicodeDecodeError):
        prompts.render(home, "manager", digest="DIGEST")


def test_packaged_manager_and_preamble_carry_the_slot():
    for name in ("manager", "task-preamble", "task-perpetual"):
        text = prompts.packaged(name)
        assert text is not None
        assert prompts.has_slot(text), f"{name}.md lost its {{local}} slot"


def test_the_packaged_manager_prompt_reads_a_merged_pull_request():
    """Supervision policy is prompt text, not Python, so the reading of
    `state=merged` / `state=closed` is only real if the shipped template
    actually says it (#57)."""
    text = prompts.packaged("manager")
    assert "state=merged" in text and "state=closed" in text


def test_a_missing_template_still_raises(home: Path):
    with pytest.raises(KeyError):
        prompts.load(home, "no-such-template")
    assert prompts.packaged("no-such-template") is None


def test_render_is_reached_through_every_agent_seam(home: Path, clock):
    """ctx.prompt() is how plugin agents render; the overlay must not be a
    runner-only feature."""
    from quorum.agent import AgentContext

    write(home, "standup.md", "do the standup\n\n{local}\n")
    write(home, "standup.local.md", "in this home, post to #ops\n")
    ctx = AgentContext(name="standup", home=home, now=clock)
    assert ctx.prompt("standup") == "do the standup\n\nin this home, post to #ops\n"


# -- the per-project block (#63) --------------------------------------------


def test_project_block_lands_in_the_project_slot(home: Path, tmp_path: Path):
    write(home, "policy.md", "before\n\n{project}\n\nafter\n")
    block = prompts.project_block(tmp_path, notes="base on main")

    assert prompts.render(home, "policy", project=block) == (
        "before\n\nbase on main\n\nafter\n"
    )


def test_project_block_is_notes_then_the_project_file(home: Path, tmp_path: Path):
    """Two sources, in that order: short metadata stays in the registry,
    longer conventions live in the repo, and a project may use either."""
    (tmp_path / ".quorum").mkdir()
    (tmp_path / ".quorum" / "task-preamble.local.md").write_text(
        "Run `just check` before pushing.\n", encoding="utf-8"
    )

    assert prompts.project_block(tmp_path, notes="base on main") == (
        "base on main\n\nRun `just check` before pushing."
    )
    assert prompts.project_block(tmp_path) == "Run `just check` before pushing."
    assert prompts.project_block(tmp_path / "elsewhere", notes="base on main") == "base on main"


@pytest.mark.parametrize("notes", ["", "   \n\n"])
def test_an_empty_project_block_takes_its_line_with_it(home: Path, tmp_path: Path, notes: str):
    write(home, "policy.md", "before\n\n{project}\n\nafter\n")

    assert prompts.project_block(tmp_path, notes=notes) == ""
    # no hole where the slot was, and no literal {project} left behind
    assert prompts.render(home, "policy", project=notes) == "before\n\nafter\n"
    assert prompts.render(home, "policy") == "before\n\nafter\n"


def test_an_undecodable_project_file_renders_as_no_block(home: Path, tmp_path: Path):
    """The read is on every task run and the file belongs to whoever owns the
    repo: one stray byte must cost the block, not the run (`load_local`'s
    contract, aimed at a project directory)."""
    write(home, "policy.md", "before\n\n{project}\n\nafter\n")
    (tmp_path / ".quorum").mkdir()
    (tmp_path / ".quorum" / "task-preamble.local.md").write_bytes(b"convent\xff\xfeions\n")

    assert prompts.load_project_local(tmp_path, "task-preamble") == ""
    assert prompts.project_block(tmp_path, notes="base on main") == "base on main"
    assert prompts.render(home, "policy", project="") == "before\n\nafter\n"


def test_a_project_block_needs_a_slot_and_is_never_prepended(home: Path):
    """Unlike the home overlay: an overlay is policy the home already had, so
    prepending it is a rescue; a project block is new, and guessing where it
    goes in a rewritten template would be worse than `quorum prompt list`
    saying it is not rendered."""
    write(home, "policy.md", "my own rewritten preamble\n")

    assert prompts.render(home, "policy", project="base on main") == (
        "my own rewritten preamble\n"
    )
    assert not prompts.has_slot("my own rewritten preamble\n", "project")


def test_the_packaged_preamble_carries_the_project_slot():
    text = prompts.packaged("task-preamble")
    assert text is not None
    assert prompts.has_slot(text, "project"), "task-preamble.md lost its {project} slot"
    # the header documents the key as {{project}}; that is not the slot
    assert "{{project}}" in text
    # house policy first, then this project's — the home rule frames the repo's
    assert text.index("\n{local}\n") < text.index("\n{project}\n")


def test_an_escaped_project_key_is_not_a_slot(home: Path):
    write(home, "policy.md", "<!-- {{project}} is the per-project block -->\nbody\n")

    assert not prompts.has_slot(prompts.load(home, "policy"), "project")
    assert prompts.render(home, "policy", project="base on main") == (
        "<!-- {project} is the per-project block -->\nbody\n"
    )
