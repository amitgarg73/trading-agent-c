"""argus#1444: a step declares which spans' output it read, so a downstream failure can name an upstream edge instead of a roster position."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.base import run_tool_loop


def _client():
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text="{}")],
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0),
                           model="m")
    c = MagicMock()
    c.messages.create.return_value = resp
    return c


def _run(inputs):
    tracer = MagicMock()
    run_tool_loop(client=_client(), model="m", system="s", tools=[], initial_message="x",
                  dispatch=lambda n, i: None, tracer=tracer, agent_name="risk", inputs=inputs)
    return tracer.log_agent_message.call_args.kwargs


def test_declared_inputs_reach_the_final_message():
    assert _run(["0123456789abcdef"])["inputs"] == ["0123456789abcdef"]


def test_nothing_declared_sends_none_not_an_empty_edge():
    assert _run(None)["inputs"] is None


def test_the_call_sites_declare_what_they_read():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "agents"
    for f, needle in [("research_agent.py", 'last_span_for("market")'), ("risk_agent.py", 'last_span_for("research")'),
                      ("scanner_agent.py", 'last_span_for("market")'), ("orchestrator.py", 'last_span_for("risk")')]:
        assert needle in (root / f).read_text(), f
