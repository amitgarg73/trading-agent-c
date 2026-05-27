from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.learning_agent import run_learning_agent
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block, tool_block

_SESSION_ID = "sess-test-001"

_SUMMARY = {
    "session_date": "2026-05-27",
    "trades_analyzed": 3,
    "win_rate": 0.67,
    "total_pnl": 120.0,
    "learnings_written": 2,
    "params_adjusted": 0,
    "goal_recommended": False,
    "top_finding": "bid entries outperform mid by 12%",
    "context_for_tomorrow": "Technology sector strong. Prefer high RS tickers.",
}

_TRADES = [
    {"ticker": "AAPL", "realized_pnl": 100.0, "exit_reason": "TARGET",
     "entry_price": 185.0, "exit_price": 187.5},
]
_SESSION = {"id": _SESSION_ID, "terminal_reason": "converged", "total_steps": 20}
_PARAMS  = [{"param_key": "strategy_min_score", "current_value": 5,
             "min_value": 3, "max_value": 9, "cooldown_until": None}]
_LEARNINGS = []
_WRITE_OK = {"status": "written", "id": "abc-123"}


def _setup_client():
    tool_calls = [
        tool_block("read_today_trades",    {},                          "t1"),
        tool_block("read_session_context", {"session_id": _SESSION_ID}, "t2"),
        tool_block("read_strategy_params", {},                          "t3"),
        tool_block("read_recent_learnings",{},                          "t4"),
    ]
    client = MagicMock()
    client.messages.create.side_effect = [
        make_api_response("tool_use", tool_calls),
        make_api_response("end_turn", [text_block(json.dumps(_SUMMARY))]),
    ]
    return client


def _run(tracer, mock_client=None):
    client = mock_client or _setup_client()
    with patch("agents.learning_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.learning_agent.read_today_trades",    return_value=_TRADES), \
         patch("agents.learning_agent.read_session_context", return_value=_SESSION), \
         patch("agents.learning_agent.read_strategy_params", return_value=_PARAMS), \
         patch("agents.learning_agent.read_recent_learnings",return_value=_LEARNINGS), \
         patch("agents.learning_agent.write_learning",        return_value=_WRITE_OK), \
         patch("agents.learning_agent.adjust_param",         return_value={"status": "applied"}), \
         patch("agents.learning_agent.recommend_goal",       return_value=_WRITE_OK):
        return run_learning_agent(tracer, _SESSION_ID, StrategyParams())


class TestRunLearningAgent:
    def test_returns_summary_dict(self, tracer):
        result = _run(tracer)
        assert result["session_date"] == "2026-05-27"
        assert result["trades_analyzed"] == 3
        assert "context_for_tomorrow" in result

    def test_span_started(self, tracer):
        _run(tracer)
        assert tracer.get_agent_span("learning") is not None

    def test_tokens_accumulated(self, tracer):
        _run(tracer)
        assert tracer._tokens.get("learning", {}).get("input", 0) > 0

    def test_session_id_in_initial_message(self, tracer):
        client = _setup_client()
        with patch("agents.learning_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.learning_agent.read_today_trades",    return_value=_TRADES), \
             patch("agents.learning_agent.read_session_context", return_value=_SESSION), \
             patch("agents.learning_agent.read_strategy_params", return_value=_PARAMS), \
             patch("agents.learning_agent.read_recent_learnings",return_value=_LEARNINGS), \
             patch("agents.learning_agent.write_learning",        return_value=_WRITE_OK), \
             patch("agents.learning_agent.adjust_param",         return_value={"status": "applied"}), \
             patch("agents.learning_agent.recommend_goal",       return_value=_WRITE_OK):
            run_learning_agent(tracer, _SESSION_ID, StrategyParams())
        first_call = client.messages.create.call_args_list[0]
        messages = first_call.kwargs.get("messages") or first_call[1].get("messages", [])
        assert _SESSION_ID in str(messages)

    def test_write_learning_tool_dispatched(self, tracer):
        write_block = tool_block(
            "write_learning",
            {"learning_type": "observation", "dimension": "entry_quality",
             "finding": "bid entries win more"},
            "t-wl",
        )
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [write_block]),
            make_api_response("end_turn", [text_block(json.dumps(_SUMMARY))]),
        ]
        with patch("agents.learning_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.learning_agent.read_today_trades",    return_value=_TRADES), \
             patch("agents.learning_agent.read_session_context", return_value=_SESSION), \
             patch("agents.learning_agent.read_strategy_params", return_value=_PARAMS), \
             patch("agents.learning_agent.read_recent_learnings",return_value=_LEARNINGS), \
             patch("agents.learning_agent.write_learning",        return_value=_WRITE_OK) as mock_write, \
             patch("agents.learning_agent.adjust_param",         return_value={"status": "applied"}), \
             patch("agents.learning_agent.recommend_goal",       return_value=_WRITE_OK):
            run_learning_agent(tracer, _SESSION_ID, StrategyParams())
        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["session_id"] == _SESSION_ID

    def test_adjust_param_dispatched_with_session_id(self, tracer):
        adjust_block = tool_block(
            "adjust_param",
            {"param_name": "strategy_min_score", "new_value": 6.0, "reason": "win rate up"},
            "t-ap",
        )
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [adjust_block]),
            make_api_response("end_turn", [text_block(json.dumps(_SUMMARY))]),
        ]
        with patch("agents.learning_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.learning_agent.read_today_trades",    return_value=_TRADES), \
             patch("agents.learning_agent.read_session_context", return_value=_SESSION), \
             patch("agents.learning_agent.read_strategy_params", return_value=_PARAMS), \
             patch("agents.learning_agent.read_recent_learnings",return_value=_LEARNINGS), \
             patch("agents.learning_agent.write_learning",        return_value=_WRITE_OK), \
             patch("agents.learning_agent.adjust_param", return_value={"status": "applied"}) as mock_adj, \
             patch("agents.learning_agent.recommend_goal",       return_value=_WRITE_OK):
            run_learning_agent(tracer, _SESSION_ID, StrategyParams())
        mock_adj.assert_called_once()
        call_kwargs = mock_adj.call_args[1]
        assert call_kwargs["session_id"] == _SESSION_ID
