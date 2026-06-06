from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.orchestrator import _build_synthesis_message, _empty_session_output, run_premarket_pipeline
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block

_MARKET_REPORT = {
    "decision": "GO", "max_positions": 5, "bias": "BULLISH",
    "skip_reason": None, "summary": "Clean open.",
}
_MARKET_SKIP = {
    "decision": "SKIP", "max_positions": 0, "bias": "NEUTRAL",
    "skip_reason": "futures -2.5%", "summary": "Strong selloff.",
}
_MARKET_CAUTION = {
    "decision": "CAUTION", "max_positions": 2, "bias": "NEUTRAL",
    "skip_reason": None, "summary": "Mixed signals, reduced size.",
}

_SCANNER_RESULT = {
    "candidates": [
        {"ticker": "AAPL", "technical_score": 8, "premarket_change_pct": 1.5,
         "price": 185.0, "sector": "Technology"},
    ],
    "n_returned": 1, "scan_rationale": "Strong setup.", "regime": "elevated_vix",
    "signals_used": ["technical_score"], "dropped_count": 5,
}
_SCANNER_EMPTY = {
    "candidates": [], "n_returned": 0, "scan_rationale": "All below threshold.",
    "regime": "high_vix", "signals_used": [], "dropped_count": 20,
}

_PROPOSALS = {
    "proposals": [
        {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.4,
         "stop_loss": 183.78, "position_size": 3500, "confidence": "HIGH",
         "evidence": ["above_vwap"]},
    ],
    "skipped": [], "summary": "One strong setup.",
}
_PROPOSALS_TWO = {
    "proposals": [
        {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.4,
         "stop_loss": 183.78, "position_size": 3500, "confidence": "HIGH",
         "evidence": ["above_vwap"]},
        {"ticker": "MSFT", "entry_price": 420.0, "target_price": 453.6,
         "stop_loss": 417.19, "position_size": 3000, "confidence": "MEDIUM",
         "evidence": ["rs_vs_spy"]},
    ],
    "skipped": [], "summary": "Two setups.",
}
_VERDICTS_APPROVED = {
    "verdicts": [{"ticker": "AAPL", "verdict": "APPROVED", "reason": "all constraints passed"}],
    "portfolio_state": {"buying_power": 46500.0, "positions_open": 0, "today_pnl": 0.0, "limit_hit": False},
}
_VERDICTS_REJECTED = {
    "verdicts": [{"ticker": "AAPL", "verdict": "REJECTED", "reason": "sector concentration"}],
    "portfolio_state": {"buying_power": 50000.0, "positions_open": 0, "today_pnl": 0.0, "limit_hit": False},
}
_VERDICTS_PARTIAL_REJECT = {
    "verdicts": [
        {"ticker": "AAPL", "verdict": "REJECTED", "reason": "sector concentration"},
        {"ticker": "MSFT", "verdict": "APPROVED",  "reason": "all constraints passed"},
    ],
    "portfolio_state": {"buying_power": 50000.0, "positions_open": 0, "today_pnl": 0.0, "limit_hit": False},
}
_VERDICTS_ALL_REJECTED = {
    "verdicts": [
        {"ticker": "AAPL", "verdict": "REJECTED", "reason": "sector concentration"},
        {"ticker": "MSFT", "verdict": "REJECTED", "reason": "count limit exceeded"},
    ],
    "portfolio_state": {"buying_power": 50000.0, "positions_open": 0, "today_pnl": 0.0, "limit_hit": False},
}
_VERDICTS_STRUCTURAL = {
    "verdicts": [{"ticker": "AAPL", "verdict": "REJECTED", "reason": "daily loss limit hit"}],
    "portfolio_state": {"buying_power": 50000.0, "positions_open": 0, "today_pnl": -500.0, "limit_hit": True},
}

_FINAL_CONVERGED = {
    "date": "2026-05-27",
    "market_context": "Clean open.",
    "trades": [
        {"ticker": "AAPL", "action": "BUY", "entry_price": 185.0, "target_price": 192.4,
         "stop_loss": 183.78, "position_size": 3500, "shares": 18,
         "confidence": "HIGH", "estimated_profit": 133.2, "max_loss": 21.96,
         "reward_risk": 6.07, "reasoning": "Strong setup."},
    ],
    "total_estimated_profit": 133.2,
    "total_max_loss": 21.96,
    "risk_note": "Approved 1/1.",
    "retry_needed": False,
    "session_meta": {"loop_iterations": 1, "retry_triggered": False,
                     "retry_reason": None, "terminal_reason": "converged"},
}
_FINAL_STRUCTURAL = {
    **_FINAL_CONVERGED,
    "trades": [],
    "retry_needed": False,
    "session_meta": {**_FINAL_CONVERGED["session_meta"], "terminal_reason": "structural_block"},
}
_FINAL_RETRY_NEEDED = {
    **_FINAL_CONVERGED,
    "trades": [],
    "retry_needed": True,
    "session_meta": {**_FINAL_CONVERGED["session_meta"], "terminal_reason": "retry_needed"},
}


class TestBuildSynthesisMessage:
    def test_contains_all_three_reports(self):
        msg = _build_synthesis_message(
            _MARKET_REPORT, _PROPOSALS, _VERDICTS_APPROVED, loop_iteration=1
        )
        assert "MARKET AGENT" in msg
        assert "RESEARCH AGENT" in msg
        assert "RISK AGENT" in msg

    def test_loop_iteration_in_message(self):
        msg = _build_synthesis_message(_MARKET_REPORT, _PROPOSALS, _VERDICTS_APPROVED, 2)
        assert "2" in msg


class TestEmptySessionOutput:
    def test_returns_empty_trades(self):
        out = _empty_session_output(_MARKET_REPORT, "skip_propagated")
        assert out["trades"] == []

    def test_terminal_reason_set(self):
        out = _empty_session_output(_MARKET_REPORT, "structural_block")
        assert out["session_meta"]["terminal_reason"] == "structural_block"

    def test_no_retry_needed(self):
        out = _empty_session_output(_MARKET_REPORT, "skip_propagated")
        assert out["retry_needed"] is False


class TestRunPremarketPipeline:
    @pytest.fixture(autouse=True)
    def _patch_scanner(self):
        # Default: Scanner Agent returns one candidate.
        # Override in individual tests to simulate empty scanner or scanner error.
        with patch("agents.orchestrator.run_scanner_agent", return_value=_SCANNER_RESULT):
            yield

    def _mock_synthesis(self, result_json):
        client = MagicMock()
        client.messages.create.return_value = make_api_response(
            "end_turn", [text_block(json.dumps(result_json))]
        )
        return client

    def test_skip_propagated_when_market_says_skip(self, tracer):
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_SKIP), \
             patch("agents.orchestrator.run_research_agent"), \
             patch("agents.orchestrator.run_risk_agent"), \
             patch("agents.orchestrator.anthropic.Anthropic"):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert result["trades"] == []
        assert result["session_meta"]["terminal_reason"] == "skip_propagated"

    def test_scanner_not_called_on_market_skip(self, tracer):
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_SKIP), \
             patch("agents.orchestrator.run_scanner_agent") as mock_scanner, \
             patch("agents.orchestrator.run_research_agent"), \
             patch("agents.orchestrator.run_risk_agent"), \
             patch("agents.orchestrator.anthropic.Anthropic"):
            run_premarket_pipeline(tracer, StrategyParams())
        mock_scanner.assert_not_called()

    def test_research_not_called_on_skip(self, tracer):
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_SKIP), \
             patch("agents.orchestrator.run_research_agent") as mock_res, \
             patch("agents.orchestrator.run_risk_agent"), \
             patch("agents.orchestrator.anthropic.Anthropic"):
            run_premarket_pipeline(tracer, StrategyParams())
        mock_res.assert_not_called()

    def test_returns_trades_on_converged(self, tracer):
        client = self._mock_synthesis(_FINAL_CONVERGED)
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS), \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_APPROVED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert len(result["trades"]) == 1
        assert result["trades"][0]["ticker"] == "AAPL"
        assert result["session_meta"]["terminal_reason"] == "converged"

    def test_research_receives_scanner_candidates(self, tracer):
        client = self._mock_synthesis(_FINAL_CONVERGED)
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_APPROVED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            run_premarket_pipeline(tracer, StrategyParams())
        call_kwargs = mock_res.call_args[1]
        assert call_kwargs.get("candidates") == _SCANNER_RESULT["candidates"]

    def test_retry_needed_stripped_from_final_output(self, tracer):
        client = self._mock_synthesis(_FINAL_CONVERGED)
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS), \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_APPROVED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert "retry_needed" not in result

    def test_retry_triggered_on_fixable_rejections(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_RETRY_NEEDED))]),
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_CONVERGED))]),
        ]
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_REJECTED) as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert mock_res.call_count == 2
        assert mock_risk.call_count == 2
        assert result["session_meta"]["retry_triggered"] is True

    def test_retry_passes_scanner_candidates_and_rejection_context(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_RETRY_NEEDED))]),
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_CONVERGED))]),
        ]
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_REJECTED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            run_premarket_pipeline(tracer, StrategyParams())
        second_kwargs = mock_res.call_args_list[1][1]
        assert second_kwargs.get("candidates") is not None
        assert second_kwargs.get("rejected_context") is not None

    def test_retry_skips_research_when_proposals_remain(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_RETRY_NEEDED))]),
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_CONVERGED))]),
        ]
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS_TWO) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_PARTIAL_REJECT) as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert mock_res.call_count == 1
        assert mock_risk.call_count == 2
        assert result["session_meta"]["retry_triggered"] is True

    def test_retry_filtered_proposals_exclude_rejected_tickers(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_RETRY_NEEDED))]),
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_CONVERGED))]),
        ]
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS_TWO), \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_PARTIAL_REJECT) as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            run_premarket_pipeline(tracer, StrategyParams())
        retry_proposals = mock_risk.call_args_list[1][0][1]
        tickers = [p["ticker"] for p in retry_proposals["proposals"]]
        assert "AAPL" not in tickers
        assert "MSFT" in tickers

    def test_retry_reruns_research_when_all_proposals_rejected(self, tracer):
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_RETRY_NEEDED))]),
            make_api_response("end_turn", [text_block(json.dumps(_FINAL_CONVERGED))]),
        ]
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS_TWO) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_ALL_REJECTED) as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert mock_res.call_count == 2
        assert mock_risk.call_count == 2
        assert result["session_meta"]["retry_triggered"] is True

    def test_caution_no_retry_on_fixable_rejections(self, tracer):
        client = self._mock_synthesis(_FINAL_RETRY_NEEDED)
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_CAUTION), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS) as mock_res, \
             patch("agents.orchestrator.run_risk_agent", return_value=_VERDICTS_APPROVED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        mock_res.assert_called_once()
        assert result["session_meta"]["terminal_reason"] == "caution_no_retry"
        assert "retry_needed" not in result

    def test_structural_block_no_retry(self, tracer):
        client = self._mock_synthesis(_FINAL_STRUCTURAL)
        with patch("agents.orchestrator.run_market_agent",   return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS) as mock_res, \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_STRUCTURAL), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            result = run_premarket_pipeline(tracer, StrategyParams())
        assert mock_res.call_count == 1
        assert result["session_meta"]["terminal_reason"] == "structural_block"

    def test_no_viable_proposals_skips_risk_and_synthesis(self, tracer):
        empty_proposals = {"proposals": [], "skipped": [], "summary": "No candidates."}
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_CAUTION), \
             patch("agents.orchestrator.run_research_agent", return_value=empty_proposals), \
             patch("agents.orchestrator.run_risk_agent") as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic") as mock_ant:
            result = run_premarket_pipeline(tracer, StrategyParams())
        mock_risk.assert_not_called()
        mock_ant.return_value.messages.create.assert_not_called()
        assert result["trades"] == []
        assert result["session_meta"]["terminal_reason"] == "no_viable_proposals"

    def test_no_viable_candidates_exits_before_research(self, tracer):
        with patch("agents.orchestrator.run_scanner_agent", return_value=_SCANNER_EMPTY), \
             patch("agents.orchestrator.run_market_agent", return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_research_agent") as mock_res, \
             patch("agents.orchestrator.run_risk_agent") as mock_risk, \
             patch("agents.orchestrator.anthropic.Anthropic"):
            result = run_premarket_pipeline(tracer, StrategyParams())
        mock_res.assert_not_called()
        mock_risk.assert_not_called()
        assert result["trades"] == []
        assert result["session_meta"]["terminal_reason"] == "no_viable_candidates"

    def test_scanner_called_with_market_report_and_params(self, tracer):
        client = self._mock_synthesis(_FINAL_CONVERGED)
        params = StrategyParams()
        with patch("agents.orchestrator.run_market_agent", return_value=_MARKET_REPORT), \
             patch("agents.orchestrator.run_scanner_agent") as mock_scanner, \
             patch("agents.orchestrator.run_research_agent", return_value=_PROPOSALS), \
             patch("agents.orchestrator.run_risk_agent",     return_value=_VERDICTS_APPROVED), \
             patch("agents.orchestrator.anthropic.Anthropic", return_value=client):
            mock_scanner.return_value = _SCANNER_RESULT
            run_premarket_pipeline(tracer, params)
        mock_scanner.assert_called_once()
        call_args = mock_scanner.call_args
        assert call_args[0][1] == _MARKET_REPORT
        assert call_args[0][2] == params
