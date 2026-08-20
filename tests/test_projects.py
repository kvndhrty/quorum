from __future__ import annotations

from pathlib import Path

import pytest

from quorum.projects import ProjectRegistry


@pytest.fixture
def registry(home: Path) -> ProjectRegistry:
    return ProjectRegistry(home)


def test_add_list_update_remove(registry: ProjectRegistry, tmp_path: Path):
    pdir = tmp_path / "NeurIPS Rebuttal"
    pdir.mkdir()
    p = registry.add(pdir, deadline="2026-09-01", tags=["ml"])
    assert p.slug == "neurips-rebuttal"
    assert registry.get("neurips-rebuttal").deadline == "2026-09-01"

    with pytest.raises(ValueError):
        registry.add(pdir)  # duplicate slug

    registry.update("neurips-rebuttal", deadline="2026-09-15", notes="pushed back")
    got = registry.get("neurips-rebuttal")
    assert got.deadline == "2026-09-15" and got.notes == "pushed back"

    assert registry.remove("neurips-rebuttal")
    assert registry.get("neurips-rebuttal") is None


def test_invalid_deadline_rejected(registry: ProjectRegistry, tmp_path: Path):
    with pytest.raises(ValueError):
        registry.add(tmp_path, name="x", deadline="soon")


def test_marker_merges_over_registry(registry: ProjectRegistry, tmp_path: Path):
    pdir = tmp_path / "paper"
    pdir.mkdir()
    registry.add(pdir, deadline="2026-09-01")
    (pdir / ".quorum.toml").write_text('deadline = "2026-10-01"\ntags = ["synced"]\n')
    got = registry.get("paper")
    assert got.deadline == "2026-10-01"
    assert got.tags == ["synced"]
    # registry file itself is untouched (canonical record preserved)
    import json

    raw = json.loads((registry.dir / "paper.json").read_text())
    assert raw["deadline"] == "2026-09-01"


def test_marker_garbage_ignored(registry: ProjectRegistry, tmp_path: Path):
    pdir = tmp_path / "p2"
    pdir.mkdir()
    registry.add(pdir, deadline="2026-09-01")
    (pdir / ".quorum.toml").write_text('deadline = "whenever"\ntags = "not-a-list"\n')
    got = registry.get("p2")
    assert got.deadline == "2026-09-01" and got.tags == []


def test_write_marker(registry: ProjectRegistry, tmp_path: Path):
    pdir = tmp_path / "p3"
    pdir.mkdir()
    registry.add(pdir, deadline="2026-12-01", tags=["a", "b"], write_marker=True)
    text = (pdir / ".quorum.toml").read_text()
    assert 'deadline = "2026-12-01"' in text and '"a", "b"' in text
