from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.research_agent import run_research_agent, _screen_candidates
from core.params import StrategyParams

_MARKET_REPORT = {
    "decision": "GO", "max_positions": 2, "bias": "BULLISH",
    "skip_reason": None, "summary": "Clean open.",
}

_CAUTION_REPORT = {**_MARKET_REPORT, "decision": "CAUTION"}

_CANDIDATES = [
    {"ticker": "AAPL", "technical_score": 8, "current_price": 185.0,
     "avg_volume": 50_000_000, "sector": "Technology"},
    {"ticker": "MSFT", "technical_score": 7, "current_price": 420.0,
     "avg_volume": 30_000_000, "sector": "Technology"},
]

_SNAPSHOT = [
    {"ticker": "AAPL", "scanner_price": 185.0, "premarket_price": 186.85,
     "premarket_change_pct": 1.0},
    {"ticker": "MSFT", "scanner_price": 420.0, "premarket_price": 422.0,
     "premarket_change_pct": 0.5},
]

_SECTOR = [
    {"etf": "XLK", "name": "Technology", "change_pct": 1.2},
    {"etf": "XLF", "name": "Financials", "change_pct": 0.4},
    {"etf": "XLU", "name": "Utilities",  "change_pct": -0.8},
]

_PROPOSE_AAPL = {
    "action": "PROPOSE",
    "ticker": "AAPL",
    "entry_price": 185.0,
    "target_price": 199.8,
    "stop_loss": 183.77,
    "position_size": 3500,
    "confidence": "HIGH",
    "evidence": ["above_vwap", "rs=1.8"],
    "skip_reason": None,
}

_SKIP_MSFT = {
    "action": "SKIP",
    "ticker": "MSFT",
    "entry_price": None,
    "target_price": None,
    "stop_loss": None,
    "position_size": None,
    "confidence": None,
    "evidence": [],
    "skip_reason": "atr_pct > 5",
}


def _run(tracer, market_report=None, rejected_context=None,
         investigate_side_effect=None):
    investigate_side_effect = investigate_side_effect or (lambda *a, **kw: _PROPOSE_AAPL)
    with patch("agents.research_agent.get_candidates",         return_value=_CANDIDATES), \
         patch("agents.research_agent.get_premarket_snapshot", return_value=_SNAPSHOT), \
         patch("agents.research_agent.get_sector_rotation",    return_value=_SECTOR), \
         patch("agents.research_agent._investigate_ticker",    side_effect=investigate_side_effect):
        return run_research_agent(
            tracer, market_report or _MARKET_REPORT,
            StrategyParams(), rejected_context=rejected_context,
        )


class TestRunResearchAgent:
    def test_returns_proposals_dict(self, tracer):
        result = _run(tracer)
        assert "proposals" in result
        assert "skipped" in result
        assert "summary" in result

    def test_propose_result_included(self, tracer):
        result = _run(tracer)
        tickers = [p["ticker"] for p in result["proposals"]]
        assert "AAPL" in tickers

    def test_span_started(self, tracer):
        _run(tracer)
        assert tracer.get_agent_span("research") is not None

    def test_skip_result_recorded(self, tracer):
        def side_effect(ticker, *args, **kwargs):
            return _SKIP_MSFT if ticker == "MSFT" else _PROPOSE_AAPL

        result = _run(tracer, investigate_side_effect=side_effect)
        skipped_tickers = [s["ticker"] for s in result["skipped"]]
        assert "MSFT" in skipped_tickers

    def test_max_positions_respected(self, tracer):
        report = {**_MARKET_REPORT, "max_positions": 1}
        result = _run(tracer, market_report=report)
        assert len(result["proposals"]) <= 1

    def test_no_candidates_returns_empty_proposals(self, tracer):
        with patch("agents.research_agent.get_candidates",      return_value=[]), \
             patch("agents.research_agent.get_sector_rotation", return_value=_SECTOR), \
             patch("agents.research_agent.anthropic.Anthropic"):
            result = run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        assert result["proposals"] == []
        assert result["skipped"] == []

    def test_no_candidates_does_not_call_claude(self, tracer):
        mock_anthropic = MagicMock()
        with patch("agents.research_agent.get_candidates",      return_value=[]), \
             patch("agents.research_agent.get_sector_rotation", return_value=_SECTOR), \
             patch("agents.research_agent.anthropic.Anthropic", return_value=mock_anthropic):
            run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        mock_anthropic.messages.create.assert_not_called()

    def test_db_error_on_candidates_returns_empty_proposals(self, tracer):
        with patch("agents.research_agent.get_candidates",      return_value=[{"error": "db timeout"}]), \
             patch("agents.research_agent.get_sector_rotation", return_value=_SECTOR), \
             patch("agents.research_agent.anthropic.Anthropic"):
            result = run_research_agent(tracer, _MARKET_REPORT, StrategyParams())
        assert result["proposals"] == []

    def test_rejected_tickers_excluded(self, tracer):
        rejected = [{"ticker": "AAPL", "reason": "sector concentration"}]
        investigate_calls = []

        def side_effect(ticker, *args, **kwargs):
            investigate_calls.append(ticker)
            return _PROPOSE_AAPL

        _run(tracer, rejected_context=rejected, investigate_side_effect=side_effect)
        assert "AAPL" not in investigate_calls

    def test_investigation_error_recorded_as_skip(self, tracer):
        def side_effect(ticker, *args, **kwargs):
            raise RuntimeError("network error")

        result = _run(tracer, investigate_side_effect=side_effect)
        assert len(result["skipped"]) > 0
        assert any("investigation error" in s["reason"] for s in result["skipped"])

    def test_proposals_sorted_by_confidence(self, tracer):
        candidates = [
            {"ticker": "AAPL", "technical_score": 8, "current_price": 185.0,
             "avg_volume": 50_000_000, "sector": "Technology"},
            {"ticker": "MSFT", "technical_score": 7, "current_price": 420.0,
             "avg_volume": 30_000_000, "sector": "Technology"},
        ]
        snapshot = [
            {"ticker": "AAPL", "scanner_price": 185.0, "premarket_price": 186.85,
             "premarket_change_pct": 1.0},
            {"ticker": "MSFT", "scanner_price": 420.0, "premarket_price": 422.0,
             "premarket_change_pct": 0.5},
        ]
        propose_low  = {**_PROPOSE_AAPL, "ticker": "AAPL", "confidence": "LOW"}
        propose_high = {**_PROPOSE_AAPL, "ticker": "MSFT", "confidence": "HIGH"}

        def side_effect(ticker, *args, **kwargs):
            return propose_low if ticker == "AAPL" else propose_high

        report = {**_MARKET_REPORT, "max_positions": 2}
        with patch("agents.research_agent.get_candidates",         return_value=candidates), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=snapshot), \
             patch("agents.research_agent.get_sector_rotation",    return_value=_SECTOR), \
             patch("agents.research_agent._investigate_ticker",    side_effect=side_effect):
            result = run_research_agent(tracer, report, StrategyParams())

        if len(result["proposals"]) >= 2:
            assert result["proposals"][0]["confidence"] == "HIGH"


class TestScreenCandidates:
    def test_selects_up_to_max_positions(self):
        report = {**_MARKET_REPORT, "max_positions": 1}
        with patch("agents.research_agent.get_candidates",         return_value=_CANDIDATES), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=_SNAPSHOT):
            selected = _screen_candidates(report, _SECTOR, [])
        assert len(selected) <= 1

    def test_excludes_rejected_tickers(self):
        with patch("agents.research_agent.get_candidates",         return_value=_CANDIDATES), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=_SNAPSHOT):
            selected = _screen_candidates(_MARKET_REPORT, _SECTOR, ["AAPL"])
        tickers = [s["ticker"] for s in selected]
        assert "AAPL" not in tickers

    def test_caution_filters_low_score(self):
        low_candidates = [
            {"ticker": "WEAK", "technical_score": 6, "current_price": 50.0,
             "avg_volume": 1_000_000, "sector": "Utilities"},
        ]
        low_snapshot = [
            {"ticker": "WEAK", "scanner_price": 50.0, "premarket_price": 50.1,
             "premarket_change_pct": 0.2},
        ]
        with patch("agents.research_agent.get_candidates",         return_value=low_candidates), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=low_snapshot):
            selected = _screen_candidates(_CAUTION_REPORT, _SECTOR, [])
        assert len(selected) == 0

    def test_caution_passes_high_score(self):
        strong_candidates = [
            {"ticker": "STRONG", "technical_score": 9, "current_price": 100.0,
             "avg_volume": 10_000_000, "sector": "Technology"},
        ]
        strong_snapshot = [
            {"ticker": "STRONG", "scanner_price": 100.0, "premarket_price": 100.5,
             "premarket_change_pct": 0.5},
        ]
        with patch("agents.research_agent.get_candidates",         return_value=strong_candidates), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=strong_snapshot):
            selected = _screen_candidates(_CAUTION_REPORT, _SECTOR, [])
        assert len(selected) == 1
        assert selected[0]["ticker"] == "STRONG"

    def test_sorted_by_score_then_momentum(self):
        candidates = [
            {"ticker": "A", "technical_score": 7, "current_price": 100.0,
             "avg_volume": 5_000_000, "sector": "Technology"},
            {"ticker": "B", "technical_score": 8, "current_price": 50.0,
             "avg_volume": 5_000_000, "sector": "Technology"},
        ]
        snapshot = [
            {"ticker": "A", "scanner_price": 100.0, "premarket_price": 101.0,
             "premarket_change_pct": 1.0},
            {"ticker": "B", "scanner_price": 50.0, "premarket_price": 50.5,
             "premarket_change_pct": 1.0},
        ]
        with patch("agents.research_agent.get_candidates",         return_value=candidates), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=snapshot):
            selected = _screen_candidates({**_MARKET_REPORT, "max_positions": 2}, _SECTOR, [])
        assert selected[0]["ticker"] == "B"  # higher score first

    def test_error_candidates_excluded(self):
        candidates_with_error = [
            {"error": "timeout"},
            {"ticker": "AAPL", "technical_score": 8, "current_price": 185.0,
             "avg_volume": 50_000_000, "sector": "Technology"},
        ]
        with patch("agents.research_agent.get_candidates",         return_value=candidates_with_error), \
             patch("agents.research_agent.get_premarket_snapshot", return_value=_SNAPSHOT):
            selected = _screen_candidates(_MARKET_REPORT, _SECTOR, [])
        tickers = [s["ticker"] for s in selected]
        assert "AAPL" in tickers
        assert len(selected) == 1
