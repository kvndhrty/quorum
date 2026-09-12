"""The shipped example home (examples/dogfood-home/) is part of the
deliverable: these tests install it into a scaffolded QUORUM_HOME and put it
through the code a real home goes through — `load_config` for the config,
`prompts.render` for the two overlays — so an option, a prompt slot or a CLI
command cannot be renamed out from under the worked example.

The `examples/steward.py` pattern, aimed at configuration instead of code."""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest

from conftest import cli_command_names, names_a_real_command, quorum_invocations
from quorum import prompts
from quorum.cli import app
from quorum.config import (
    AgentConfig,
    CIConfig,
    Config,
    ConfigError,
    HarnessConfig,
    HerdrConfig,
    NotifyConfig,
    QuorumSection,
    SandboxConfig,
    TasksConfig,
    load_config,
    parse_schedule,
)
from quorum.home import CONFIG_NAME

EXAMPLE = Path(__file__).parent.parent / "examples" / "dogfood-home"

# Every [section] the example config may use, and the model that defines what
# its keys mean. `Config` ignores keys it does not know, so an option renamed
# since the example was written would otherwise sit there being dropped.
SECTION_MODELS = {
    "quorum": QuorumSection,
    "sandbox": SandboxConfig,
    "tasks": TasksConfig,
    "ci": CIConfig,
    "notify": NotifyConfig,
    "herdr": HerdrConfig,
}
TABLE_MODELS = {"harness": HarnessConfig, "agents": AgentConfig}

OVERLAYS = ["manager", "task-preamble"]
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def install_example(home: Path) -> Path:
    """Copy the example over a scaffolded home, the way its README says to."""
    shutil.copy(EXAMPLE / CONFIG_NAME, home / CONFIG_NAME)
    for name in OVERLAYS:
        shutil.copy(EXAMPLE / "prompts" / f"{name}.local.md", prompts.local_path(home, name))
    return home


def uncomment_table(text: str, table: str) -> str:
    """The example's commented-out `#[<table>]` block, uncommented — so the
    block a reader is invited to enable is checked like the live ones."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith(f"#[{table}]"):
            inside = True
        elif inside and not line.startswith("#"):
            inside = False
        out.append(line[1:] if inside else line)
    assert f"[{table}]" in "\n".join(out), f"no commented [{table}] block in the example config"
    return "\n".join(out) + "\n"


def overlay_body(name: str) -> str:
    """An overlay minus its leading HTML-comment header: the text that
    actually reaches the harness."""
    text = (EXAMPLE / "prompts" / f"{name}.local.md").read_text(encoding="utf-8")
    return text.split("-->", 1)[1].strip()


def test_example_config_loads(home: Path):
    config = load_config(install_example(home))
    assert config.tasks.default_harness in config.harness  # what doctor checks
    assert config.agents["manager"].type == "manager"
    assert parse_schedule(config.agents["manager"].schedule)  # quorum can translate it
    assert config.agents["manager"].auto_pause is False


def test_example_harness_template_matches_its_inject_mode(home: Path):
    """`inject = "stream-json"` is a promise about the argv next to it: the
    runner writes the prompt and every nudge to the harness's stdin, which
    only arrives if the CLI was told to read its turns from there."""
    harness = load_config(install_example(home)).harness["claude"]
    assert harness.inject == "stream-json"
    assert "--input-format" in harness.start and "stream-json" in harness.start
    assert "{session}" in harness.resume  # or a resumed run replays from nothing


@pytest.mark.parametrize("uncommented", [False, True], ids=["as-shipped", "notify-enabled"])
def test_example_config_names_only_real_options(home: Path, uncommented: bool):
    """Every key the example sets still exists on the model that reads it —
    including the ones in the commented [notify] block, which is meant to be
    uncommented and so has to be valid config rather than prose."""
    text = (EXAMPLE / CONFIG_NAME).read_text(encoding="utf-8")
    if uncommented:
        text = uncomment_table(text, "notify")
    (home / CONFIG_NAME).write_text(text, encoding="utf-8")
    config = load_config(home)
    assert (config.notify is not None) is uncommented

    raw = tomllib.loads(text)
    assert raw, "the example config parses to nothing"
    for section, value in raw.items():
        assert section in Config.model_fields, f"[{section}] is not a quorum config section"
        if section in SECTION_MODELS:
            for key in value:
                assert key in SECTION_MODELS[section].model_fields, f"[{section}].{key} is gone"
        else:
            model = TABLE_MODELS[section]
            for name, table in value.items():
                for key in table:
                    assert key in model.model_fields, f"[{section}.{name}].{key} is gone"


def test_a_broken_copy_of_the_example_fails_loudly(home: Path):
    """The example is installed by copying a file; a truncated copy must
    raise here rather than load as an all-defaults home somewhere else."""
    install_example(home)
    (home / CONFIG_NAME).write_text("[tasks\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(home)


@pytest.mark.parametrize("name", OVERLAYS)
def test_overlays_render_into_their_slot(home: Path, name: str):
    """The overlays are merged into the packaged templates at `{local}` — not
    prepended, which is what `render` falls back to when the slot is gone,
    and which would put house rules above the prompt they qualify."""
    install_example(home)
    assert prompts.has_slot(prompts.load(home, name)), f"{name}.md lost its {{local}} slot"

    placeholders = (
        {"digest": "DIGEST"}
        if name == "manager"
        else {
            "task_id": "a1b2c3",
            "project_path": "/tmp/wt",
            "issue": "",
            "perpetual": "",
            "project": "",
        }
    )
    rendered = prompts.render(home, name, **placeholders)

    body = overlay_body(name)
    assert body in rendered, f"the {name} overlay did not reach the rendered prompt"
    assert rendered.index(body) > 0, f"the {name} overlay was prepended, not slotted"
    # Nothing the example relies on is left unfilled. Header comments are
    # stripped first: they *document* placeholders, in braces, on purpose.
    for key in placeholders:
        assert "{" + key + "}" not in HTML_COMMENT.sub("", rendered)


@pytest.mark.parametrize("name", OVERLAYS)
def test_overlay_bodies_use_no_placeholders(home: Path, name: str):
    """An overlay is substituted into its template as a *value*, so a
    `{task_id}` written in one would stay literal in the prompt the harness
    reads. The header comments say so; this is what keeps them true."""
    assert "{" not in overlay_body(name)


def test_example_only_names_real_cli_commands():
    """The same rot check the packaged prompts get (test_cli), on the files a
    new user copies first."""
    known = cli_command_names(app)
    checked = 0
    for path in [EXAMPLE / "README.md", *sorted((EXAMPLE / "prompts").glob("*.md"))]:
        for invocation in quorum_invocations(path.read_text(encoding="utf-8")):
            assert names_a_real_command(invocation, known), (
                f"{path.name} names a command that does not exist: {invocation!r}"
            )
            checked += 1
    assert checked > 10  # the extractor still finds things
