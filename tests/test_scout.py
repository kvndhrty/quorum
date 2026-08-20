from __future__ import annotations

from pathlib import Path

from quorum.agent import AgentContext
from quorum.agents import scout as scout_mod
from quorum.agents.scout import Scout, load_candidates
from quorum.config import Config, ProjectsConfig
from quorum.messages import MessageBus
from quorum.projects import ProjectRegistry


def make_scout(home: Path, clock, roots: list[str]) -> Scout:
    config = Config(projects=ProjectsConfig(workspace_roots=roots))
    return Scout(AgentContext(home=home, name="scout", config=config, now=clock))


def test_scout_proposes_git_and_marker_dirs_once(home: Path, clock, tmp_path: Path):
    root = tmp_path / "work"
    (root / "gitproj" / ".git").mkdir(parents=True)
    (root / "marked").mkdir(parents=True)
    (root / "marked" / ".quorum.toml").write_text('name = "Marked Project"\n')
    (root / "plain").mkdir()  # neither marker nor git: ignored

    s = make_scout(home, clock, [str(root)])
    s.tick()
    s.tick()  # second run: no duplicate announcements

    msgs = MessageBus(home).read_topic("projects")
    assert len(msgs) == 2
    slugs = {m.payload["slug"] for m in msgs}
    assert slugs == {"gitproj", "marked-project"}
    cands = load_candidates(home)
    assert set(cands) == {"gitproj", "marked-project"}


def test_adopted_candidate_drops_out(home: Path, clock, tmp_path: Path):
    root = tmp_path / "work"
    (root / "proj" / ".git").mkdir(parents=True)
    s = make_scout(home, clock, [str(root)])
    s.tick()
    path = load_candidates(home)["proj"]
    ProjectRegistry(home).add(path)  # what `quorum project adopt` does
    s.tick()
    assert "proj" not in load_candidates(home)


def test_declined_candidate_suppressed(home: Path, clock, tmp_path: Path):
    root = tmp_path / "work"
    (root / "noisy" / ".git").mkdir(parents=True)
    s = make_scout(home, clock, [str(root)])
    s.tick()
    scout_mod.decline(home, "noisy")
    assert "noisy" not in load_candidates(home)
    s.tick()
    # still suppressed, and no re-announcement
    assert "noisy" not in load_candidates(home)
    msgs = MessageBus(home).read_topic("projects")
    assert len(msgs) == 1


def test_registered_projects_not_proposed(home: Path, clock, tmp_path: Path):
    root = tmp_path / "work"
    pdir = root / "known"
    (pdir / ".git").mkdir(parents=True)
    ProjectRegistry(home).add(pdir)
    make_scout(home, clock, [str(root)]).tick()
    assert load_candidates(home) == {}
    assert MessageBus(home).read_topic("projects") == []
