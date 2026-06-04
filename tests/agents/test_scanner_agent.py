from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.scanner_agent import run_scanner_agent, _build_message, _dispatch
from core.params import StrategyParams
from tests.conftest import make_api_response, make_query, text_block, tool_block


_MARKET_GO = {
    "decision": "GO", "max_positions": 5, "bias": "BULLISH",
    "vix_value": 18.5, "vix_level": "ELEVATED",
    "skip_reason": None, "summary": "Clean open.",
}
_MARKET_CAUTION = {
    "decision": "CAUTION", "max_positions": 2, "bias": "NEUTRAL",
    "vix_value": 27.0, "vix_level": "HIGH",
    "skip_reason": None, "summary": "High VIX, reduced size.",
}

_SCAN_ROWS = [
    {"ticker": "AAPL", "score": 8, "price": 185.0, "sector": "Technology"},
    {"ticker": "MSFT", "score": 7, "price": 420.0, "sector": "Technology"},
]

_SCANNER_JSON = json.dumps({
    "candidates": [
        {"ticker": "AAPL", "technical_score": 8, "premarket_change_pct": 1.5,
         "price": 185.0, "sector": "Technology"},
        {"ticker": "MSFT", "technical_score": 7, "premarket_change_pct": 0.8,
         "price": 420.0, "sector": "Technology"},
    ],
    "n_returned": 2,
    "scan_rationale": "Elevated VIX. Selected momentum setups with score >= 5.",
    "signals_used": ["technical_score", "premarket_momentum"],
    "regime": "elevated_vix",
    "dropped_count": 5,
})

_EMPTY_JSON = json.dumps({
    "candidates": [],
    "n_returned": 0,
    "scan_rationale": "All tickers below quality threshold.",
    "signals_used": [],
    "regime": "high_vix",
    "dropped_count": 20,
})


def _make_client(response_json: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = make_api_response(
        "end_turn", [text_block(response_json)]
    )
    return client


class TestRunScannerAgent:
    def test_returns_candidates_dict(self, tracer):
        client = _make_client(_SCANNER_JSON)
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            result = run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        assert "candidates" in result
        assert "n_returned" in result
        assert result["n_returned"] == 2

    def test_candidates_have_required_fields(self, tracer):
        client = _make_client(_SCANNER_JSON)
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            result = run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        for c in result["candidates"]:
            assert "ticker" in c
            assert "technical_score" in c
            assert "premarket_change_pct" in c

    def test_span_started(self, tracer):
        client = _make_client(_SCANNER_JSON)
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        assert tracer.get_agent_span("scanner") is not None

    def test_empty_candidates_returns_safely(self, tracer):
        client = _make_client(_EMPTY_JSON)
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            result = run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        assert result["n_returned"] == 0
        assert result["candidates"] == []

    def test_llm_error_returns_empty_candidates(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API timeout")
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            result = run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        assert result["candidates"] == []
        assert result["n_returned"] == 0
        assert "error" in result["scan_rationale"]

    def test_decision_logged_candidates_selected(self, tracer, mock_supabase):
        client = _make_client(_SCANNER_JSON)
        inserts = []
        mock_supabase.table.return_value.insert.side_effect = lambda row: (
            inserts.append(row) or mock_supabase.table.return_value
        )
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        outcomes = [r.get("outcome") for r in inserts if isinstance(r, dict)]
        assert "candidates_selected" in outcomes

    def test_decision_logged_low_quality_halt(self, tracer, mock_supabase):
        client = _make_client(_EMPTY_JSON)
        inserts = []
        mock_supabase.table.return_value.insert.side_effect = lambda row: (
            inserts.append(row) or mock_supabase.table.return_value
        )
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client):
            run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        outcomes = [r.get("outcome") for r in inserts if isinstance(r, dict)]
        assert "low_quality_halt" in outcomes

    def test_caution_message_includes_caution(self, tracer):
        msg = _build_message(_MARKET_CAUTION, StrategyParams())
        assert "CAUTION" in msg

    def test_go_message_includes_vix(self, tracer):
        msg = _build_message(_MARKET_GO, StrategyParams())
        assert "18.5" in msg


class TestDispatch:
    def test_unknown_tool_returns_error(self):
        result = _dispatch("nonexistent_tool", {})
        assert "error" in result

    def test_get_scan_results_delegates(self, mock_supabase):
        mock_supabase.table.return_value = make_query(_SCAN_ROWS)
        result = _dispatch("get_scan_results", {"min_score": 5})
        assert isinstance(result, list)

    def test_filter_and_rank_delegates(self):
        candidates = [
            {"ticker": "AAPL", "technical_score": 8, "premarket_change_pct": 1.5}
        ]
        result = _dispatch("filter_and_rank", {
            "candidates": candidates, "max_n": 10, "min_score": 5, "caution_mode": False
        })
        assert "candidates" in result
        assert "n_returned" in result

    def test_get_gap_ups_delegates(self):
        with patch("agents.tools.scanner_tools.get_gap_up_tickers", return_value=[]), \
             patch("agents.tools.scanner_tools._get_universe_tickers", return_value=[]):
            result = _dispatch("get_gap_ups", {"min_gap_pct": 2.0})
        assert isinstance(result, list)

    def test_get_sector_leaders_delegates(self):
        with patch("agents.tools.scanner_tools.get_sector_rotation", return_value=[]):
            result = _dispatch("get_sector_leaders", {"n": 5})
        assert isinstance(result, list)

    def test_get_premarket_snapshot_delegates(self):
        with patch("agents.tools.scanner_tools.get_premarket_snapshot", return_value=[]):
            result = _dispatch("get_premarket_snapshot", {"tickers": ["AAPL"]})
        assert isinstance(result, list)


class TestToolLoop:
    def test_tool_call_is_logged(self, tracer, mock_supabase):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [
                tool_block("get_scan_results", {"min_score": 1}, bid="t1")
            ]),
            make_api_response("end_turn", [text_block(_SCANNER_JSON)]),
        ]
        inserts = []
        mock_supabase.table.return_value.insert.side_effect = lambda row: (
            inserts.append(row) or mock_supabase.table.return_value
        )
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.scanner_agent._dispatch", return_value=[]):
            run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        tool_rows = [r for r in inserts if isinstance(r, dict)
                     and r.get("step_type") == "tool_call"
                     and r.get("agent") == "scanner"]
        assert len(tool_rows) >= 1

    def test_sequence_advances_on_tool_calls(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [
                tool_block("get_scan_results", {"min_score": 1}, bid="t1")
            ]),
            make_api_response("end_turn", [text_block(_SCANNER_JSON)]),
        ]
        before = tracer.get_sequence()
        with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.scanner_agent._dispatch", return_value=[]):
            run_scanner_agent(tracer, _MARKET_GO, StrategyParams())
        assert tracer.get_sequence() > before
