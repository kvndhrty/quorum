from __future__ import annotations

import pytest

from quorum.config import LLMConfig
from quorum.llm import LLMClient


def make_client(fake_llm: list[str], mode: str = "ok", monkeypatch=None, **overrides) -> LLMClient:
    cfg = LLMConfig(
        executable=fake_llm[0],
        args=[*fake_llm[1:], *overrides.pop("extra_args", [])],
        input=overrides.pop("input", "stdin"),
        timeout_seconds=overrides.pop("timeout_seconds", 10),
        env={"FAKE_LLM_MODE": mode, **overrides.pop("env", {})},
        **overrides,
    )
    return LLMClient.from_config(cfg)


def test_stdin_completion(fake_llm):
    client = make_client(fake_llm, env={"FAKE_LLM_OUTPUT": "hello world"})
    assert client.complete("prompt text") == "hello world"


def test_argv_mode_appends_prompt(fake_llm):
    client = make_client(fake_llm, mode="echo", input="argv")
    assert client.complete("the prompt") == "the prompt"


def test_argv_placeholder_substitution(fake_llm):
    client = make_client(
        fake_llm, mode="echo", input="argv", extra_args=["prefix {prompt} suffix"]
    )
    assert client.complete("MID") == "prefix MID suffix"


def test_failure_returns_none(fake_llm):
    assert make_client(fake_llm, mode="fail").complete("x") is None


def test_timeout_returns_none(fake_llm):
    client = make_client(fake_llm, mode="timeout", timeout_seconds=1)
    assert client.complete("x") is None


def test_missing_executable_returns_none():
    cfg = LLMConfig(executable="/nonexistent/llm-cli")
    assert LLMClient.from_config(cfg).complete("x") is None


def test_unconfigured_is_disabled():
    client = LLMClient.from_config(None)
    assert not client.enabled
    assert client.complete("x") is None


def test_agent_opt_out_and_override(fake_llm):
    cfg = LLMConfig(executable=fake_llm[0], args=fake_llm[1:], env={"FAKE_LLM_MODE": "ok"})
    assert not LLMClient.from_config(cfg, agent_settings={"llm": False}).enabled
    # per-agent override without a global [llm] section
    client = LLMClient.from_config(
        None,
        agent_settings={
            "llm": {
                "executable": fake_llm[0],
                "args": fake_llm[1:],
                "env": {"FAKE_LLM_MODE": "ok", "FAKE_LLM_OUTPUT": "per-agent"},
            }
        },
    )
    assert client.complete("x") == "per-agent"


def test_oversized_prompt_truncated_head_and_tail(fake_llm):
    client = make_client(fake_llm, mode="echo", max_prompt_chars=200)
    out = client.complete("A" * 300 + "ZTAIL")
    assert out is not None and len(out) < 320
    assert "truncated" in out and out.startswith("A") and out.rstrip().endswith("ZTAIL")


def test_proxy_backend_reserved(fake_llm):
    cfg = LLMConfig(backend="proxy", executable="anything")
    with pytest.raises(Exception, match="proxy"):
        LLMClient.from_config(cfg)
