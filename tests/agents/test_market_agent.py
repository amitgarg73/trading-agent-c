from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.market_agent import run_market_agent
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block, tool_block

_MARKET_REPORT = {
    "decision": "GO",
    "max_positions": 10,
    "bias": "BULLISH",
    "skip_reason": None,
    "summary": "VIX 18, futures +0.4%, calm open.",
}

_VIX_RESULT     = {"value": 18.5, "level": "ELEVATED"}
_FUTURES_RESULT = {"S&P500": {"change_pct": 0.4}, "Nasdaq": {"change_pct": 0.5},
                   "Dow": {"change_pct": 0.3}, "avg_change_pct": 0.4, "bias": "BULLISH"}
_FG_RESULT      = {"value": 52, "classification": "Neutral"}
_SR_RESULT      = [{"etf": "XLK", "change_pct": 0.8}]


def _setup_client(tool_responses=None, final_json=None):
    """Build a mock Anthropic client that returns tool calls then end_turn."""
    if tool_responses is None:
        tool_responses = [
            tool_block("get_vix",             {}, "t1"),
            tool_block("get_futures",         {}, "t2"),
            tool_block("get_fear_greed",      {}, "t3"),
            tool_block("get_sector_rotation", {}, "t4"),
        ]
    if final_json is None:
        final_json = json.dumps(_MARKET_REPORT)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        make_api_response("tool_use", tool_responses),
        make_api_response("end_turn", [text_block(final_json)]),
    ]
    return mock_client


class TestRunMarketAgent:
    def _run(self, tracer, mock_client=None, final_json=None):
        client = mock_client or _setup_client(final_json=final_json)
        with patch("agents.market_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.market_agent.get_vix",             return_value=_VIX_RESULT), \
             patch("agents.market_agent.get_futures",         return_value=_FUTURES_RESULT), \
             patch("agents.market_agent.get_fear_greed",      return_value=_FG_RESULT), \
             patch("agents.market_agent.get_sector_rotation", return_value=_SR_RESULT):
            return run_market_agent(tracer, StrategyParams())

    def test_returns_market_report_dict(self, tracer):
        result = self._run(tracer)
        assert result["decision"] == "GO"
        assert result["max_positions"] == 10
        assert result["bias"] == "BULLISH"

    def test_dispatches_all_four_tools(self, tracer):
        with patch("agents.market_agent.anthropic.Anthropic") as mock_cls, \
             patch("agents.market_agent.get_vix",             return_value=_VIX_RESULT) as m_vix, \
             patch("agents.market_agent.get_futures",         return_value=_FUTURES_RESULT) as m_fut, \
             patch("agents.market_agent.get_fear_greed",      return_value=_FG_RESULT) as m_fg, \
             patch("agents.market_agent.get_sector_rotation", return_value=_SR_RESULT) as m_sr:
            mock_cls.return_value = _setup_client()
            run_market_agent(tracer, StrategyParams())
        m_vix.assert_called_once()
        m_fut.assert_called_once()
        m_fg.assert_called_once()
        m_sr.assert_called_once()

    def test_parses_json_in_markdown_code_block(self, tracer):
        wrapped = f"```json\n{json.dumps(_MARKET_REPORT)}\n```"
        result = self._run(tracer, final_json=wrapped)
        assert result["decision"] == "GO"

    def test_skip_decision_preserved(self, tracer):
        skip_report = {**_MARKET_REPORT, "decision": "SKIP", "skip_reason": "futures -2.1%"}
        result = self._run(tracer, final_json=json.dumps(skip_report))
        assert result["decision"] == "SKIP"
        assert "futures" in result["skip_reason"]

    def test_caution_decision_preserved(self, tracer):
        caution = {**_MARKET_REPORT, "decision": "CAUTION", "max_positions": 5}
        result = self._run(tracer, final_json=json.dumps(caution))
        assert result["decision"] == "CAUTION"
        assert result["max_positions"] == 5

    def test_tracer_span_started(self, tracer):
        self._run(tracer)
        assert tracer.get_agent_span("market") is not None

    def test_tracer_tokens_accumulated(self, tracer):
        self._run(tracer)
        assert tracer._tokens.get("market", {}).get("input", 0) > 0

    def test_unknown_tool_returns_error_and_continues(self, tracer):
        client = _setup_client(
            tool_responses=[tool_block("unknown_tool", {}, "t-x")],
            final_json=json.dumps(_MARKET_REPORT),
        )
        result = self._run(tracer, mock_client=client)
        assert result["decision"] == "GO"
