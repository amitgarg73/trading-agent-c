"""argus#1735: every agent step says which instructions, or which code, it ran with.

On production 0 of 10,614 spans carried a `prompt_version`, so "what changed before it broke" could not be answered for anyone, and that history cannot be
backfilled. These tests pin the labels. They are only labels: no prompt, model or order changes.
"""
import re

import pytest

from trace import versions
from tests.trace import session_shapes_1519 as sh


@pytest.fixture(autouse=True)
def _fixed_code(monkeypatch):
    monkeypatch.setenv("PROVY_CODE_VERSION", "code-abc1234")
    monkeypatch.setattr(versions, "_CODE_READ", False)
    monkeypatch.setattr(versions, "_CODE", None)


def _labels(spans, agent, step_type):
    return [dict(s.attributes or {}).get("argus.prompt_version") for s in spans
            if dict(s.attributes or {}).get("argus.agent") == agent and dict(s.attributes or {}).get("argus.step_type") == step_type]


def test_the_hash_is_stable_and_ignores_whitespace():
    a = versions.prompt_hash("You are  a risk manager.\nApprove or reject.")
    assert a == versions.prompt_hash("You are a risk manager. Approve or reject.")
    assert re.fullmatch(r"p-[0-9a-f]{10}", a)
    assert a != versions.prompt_hash("You are a risk manager. Approve everything.")


def test_every_model_agent_registers_its_prompt():
    import agents.market_agent, agents.risk_agent, agents.learning_agent, agents.orchestrator, agents.research_agent, agents.scanner_agent  # noqa: F401
    for agent in ("market", "risk", "learner", "orchestrator", "research", "scanner"):
        assert re.fullmatch(r"p-[0-9a-f]{10}", versions._PROMPTS[agent]), agent


def test_a_model_step_is_labelled_with_its_prompt_and_a_code_step_with_the_commit(tracer, mock_argus_exporter):
    risk = versions.register_prompt("risk", "You are a risk manager.")
    tracer.log_agent_message("risk", "approved", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
    tracer.log_agent_message("orchestrator", "Entered NOW at $10", "entered")                      # plain code: no model, no tokens
    assert _labels(mock_argus_exporter.spans, "risk", "agent_message") == [risk]
    assert _labels(mock_argus_exporter.spans, "orchestrator", "agent_message") == ["code-abc1234"]


def test_a_fan_out_step_finds_its_base_agents_prompt(tracer, mock_argus_exporter):
    v = versions.register_prompt("research", "Investigate the ticker.")
    tracer.log_agent_message("research_GILD", "r", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
    assert _labels(mock_argus_exporter.spans, "research_GILD", "agent_message") == [v]


def test_a_tool_call_carries_no_version(tracer, mock_argus_exporter):
    tracer.log_tool_call("orchestrator", "submit_bracket_order", {"ticker": "NOW"}, {"outcome": "filled"})
    assert all("argus.prompt_version" not in dict(s.attributes or {}) for s in mock_argus_exporter.spans
               if dict(s.attributes or {}).get("argus.step_type") == "tool_call")


def test_no_commit_and_no_prompt_sends_no_label_and_does_not_raise(tracer, mock_argus_exporter, monkeypatch):
    monkeypatch.delenv("PROVY_CODE_VERSION", raising=False)
    monkeypatch.setattr(versions, "code_version", lambda: None)
    tracer.log_agent_message("orchestrator", "code step", "done")
    assert _labels(mock_argus_exporter.spans, "orchestrator", "agent_message") == [None]


def test_a_failing_label_never_breaks_a_step(tracer, mock_argus_exporter, monkeypatch):
    monkeypatch.setattr(versions, "version_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    span_id = tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
    assert isinstance(span_id, str) and len(span_id) == 16
