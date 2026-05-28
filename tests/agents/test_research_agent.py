from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agents.research_agent import _build_user_message, run_research_agent
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block, tool_block

_MARKET_REPORT = {
    "decision": "GO", "max_positions": 5, "bias": "BULLISH",
    "skip_reason": None, "summary": "Clean open.",
}

_PROPOSALS = {
    "proposals": [
        {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.4,
         "stop_loss": 183.78, "position_size": 3500, "confidence": "HIGH",
         "evidence": ["above_vwap", "rs=1.8"]},
    ],
    "skipped": [],
    "summary": "One strong setup.",
}

_CANDIDATES = [
    {"ticker": "AAPL", "technical_score": 8, "current_price": 185.0, "avg_volume": 50_000_000},
]
_NEWS = {"blackout": False, "reason": None, "headlines": ["Strong earnings"]}
_PRICE = {"price": 185.0, "source": "alpaca", "stale_minutes": 0}
_SIGNALS = {"above_vwap": True, "vwap": 183.5, "rs_vs_spy": 1.8, "today_pct_change": 0.6}
_ATR = {"atr_pct": 1.2, "orb_pct": 0.4}
_HIST = {"trades": 5, "wins": 4, "win_rate_pct": 80.0, "avg_pnl": 45.0, "last_exit": "TARGET"}


def _setup_client():
    tool_calls = [
        tool_block("get_candidates",        {"min_score": 5}, "t1"),
        tool_block("get_news",              {"ticker": "AAPL"}, "t2"),
        tool_block("get_intraday_signals",  {"ticker": "AAPL"}, "t3"),
        tool_block("get_live_price",        {"ticker": "AAPL"}, "t4"),
        tool_block("get_atr",               {"ticker": "AAPL"}, "t5"),
        tool_block("get_position_history",  {"ticker": "AAPL"}, "t6"),
    ]
    from unittest.mock import MagicMock
    client = MagicMock()
    client.messages.create.side_effect = [
        make_api_response("tool_use", tool_calls),
        make_api_response("end_turn", [text_block(json.dumps(_PROPOSALS))]),
    ]
    return client


def _run(tracer, mock_client=None, market_report=None, rejected_context=None):
    client = mock_client or _setup_client()
    with patch("agents.research_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.research_agent.get_candidates",       return_value=_CANDIDATES), \
         patch("agents.research_agent.get_news",             return_value=_NEWS), \
         patch("agents.research_agent.get_live_price",       return_value=_PRICE), \
         patch("agents.research_agent.get_intraday_signals", return_value=_SIGNALS), \
         patch("agents.research_agent.get_atr",              return_value=_ATR), \
         patch("agents.research_agent.get_position_history", return_value=_HIST):
        return run_research_agent(
            tracer, market_report or _MARKET_REPORT,
            StrategyParams(), rejected_context=rejected_context,
        )


class TestRunResearchAgent:
    def test_returns_proposals_dict(self, tracer):
        result = _run(tracer)
        assert "proposals" in result
        assert result["proposals"][0]["ticker"] == "AAPL"

    def test_span_started(self, tracer):
        _run(tracer)
        assert tracer.get_agent_span("research") is not None

    def test_tokens_accumulated(self, tracer):
        _run(tracer)
        assert tracer._tokens.get("research", {}).get("input", 0) > 0

    def test_parses_json_from_code_block(self, tracer):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [tool_block("get_candidates", {}, "t1")]),
            make_api_response("end_turn", [text_block(f"```json\n{json.dumps(_PROPOSALS)}\n```")]),
        ]
        result = _run(tracer, mock_client=client)
        assert "proposals" in result

    def test_no_candidates_returns_empty_proposals(self, tracer):
        from unittest.mock import MagicMock
        with patch("agents.research_agent.get_candidates", return_value=[]), \
             patch("agents.research_agent.anthropic.Anthropic"):
            result = run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        assert result["proposals"] == []
        assert result["skipped"] == []

    def test_no_candidates_does_not_call_claude(self, tracer):
        from unittest.mock import MagicMock
        mock_anthropic = MagicMock()
        with patch("agents.research_agent.get_candidates", return_value=[]), \
             patch("agents.research_agent.anthropic.Anthropic", return_value=mock_anthropic):
            run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        mock_anthropic.messages.create.assert_not_called()

    def test_db_error_on_candidates_returns_empty_proposals(self, tracer):
        with patch("agents.research_agent.get_candidates", return_value=[{"error": "db timeout"}]), \
             patch("agents.research_agent.anthropic.Anthropic"):
            result = run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        assert result["proposals"] == []


class TestBuildUserMessage:
    def test_includes_market_report_json(self):
        msg = _build_user_message(_MARKET_REPORT)
        assert "max_positions" in msg
        assert "market conditions" in msg.lower()

    def test_no_rejected_context_by_default(self):
        msg = _build_user_message(_MARKET_REPORT)
        assert "rejected" not in msg.lower()

    def test_rejected_context_included_on_retry(self):
        rejected = [{"ticker": "AAPL", "reason": "sector concentration"}]
        msg = _build_user_message(_MARKET_REPORT, rejected_context=rejected)
        assert "AAPL" in msg
        assert "sector concentration" in msg
        assert "Avoid" in msg

    def test_caution_market_report_passed_through(self):
        caution_report = {**_MARKET_REPORT, "decision": "CAUTION"}
        msg = _build_user_message(caution_report)
        assert "CAUTION" in msg
