from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.tools.scanner_tools import (
    filter_and_rank,
    get_gap_ups,
    get_scan_results,
    get_sector_leaders,
)
from tests.conftest import make_query


# ── fixtures ──────────────────────────────────────────────────────────────────

_DB_ROWS = [
    {"ticker": "AAPL", "score": 8, "price": 185.0, "sector": "Technology"},
    {"ticker": "MSFT", "score": 7, "price": 420.0, "sector": "Technology"},
    {"ticker": "WEAK", "score": 4, "price": 50.0,  "sector": "Utilities"},
]

_CANDIDATES = [
    {"ticker": "AAPL", "technical_score": 8, "premarket_change_pct": 1.5, "sector": "Technology"},
    {"ticker": "MSFT", "technical_score": 7, "premarket_change_pct": 0.8, "sector": "Technology"},
    {"ticker": "WEAK", "technical_score": 4, "premarket_change_pct": 0.1, "sector": "Utilities"},
    {"ticker": "JUNK", "technical_score": 2, "premarket_change_pct": 0.0, "sector": None},
]


# ── get_scan_results ──────────────────────────────────────────────────────────

class TestGetScanResults:
    def test_returns_scored_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query(_DB_ROWS)
        result = get_scan_results(min_score=1)
        assert len(result) == 3
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["technical_score"] == 8

    def test_empty_db_returns_empty_list(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = get_scan_results()
        assert result == []

    def test_db_error_propagates(self, mock_supabase):
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.gte.return_value = q
        q.order.return_value = q
        q.execute.side_effect = RuntimeError("db timeout")
        mock_supabase.table.return_value = q
        with pytest.raises(RuntimeError, match="db timeout"):
            get_scan_results()

    def test_field_mapping(self, mock_supabase):
        mock_supabase.table.return_value = make_query([_DB_ROWS[0]])
        row = get_scan_results()[0]
        assert "ticker" in row
        assert "technical_score" in row
        assert "price" in row
        assert "sector" in row


# ── get_gap_ups ───────────────────────────────────────────────────────────────

class TestGetGapUps:
    def test_filters_to_universe(self):
        movers = [
            {"ticker": "AAPL", "gap_pct": 3.0},
            {"ticker": "PENNY", "gap_pct": 5.0},  # not in universe
        ]
        with patch("agents.tools.scanner_tools.get_gap_up_tickers", return_value=movers), \
             patch("agents.tools.scanner_tools._get_universe_tickers", return_value=["AAPL", "MSFT"]):
            result = get_gap_ups(min_gap_pct=2.0)
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_empty_movers(self):
        with patch("agents.tools.scanner_tools.get_gap_up_tickers", return_value=[]), \
             patch("agents.tools.scanner_tools._get_universe_tickers", return_value=["AAPL"]):
            result = get_gap_ups()
        assert result == []

    def test_all_movers_in_universe(self):
        movers = [{"ticker": "AAPL", "gap_pct": 3.0}, {"ticker": "MSFT", "gap_pct": 2.5}]
        with patch("agents.tools.scanner_tools.get_gap_up_tickers", return_value=movers), \
             patch("agents.tools.scanner_tools._get_universe_tickers", return_value=["AAPL", "MSFT"]):
            result = get_gap_ups()
        assert len(result) == 2


# ── get_sector_leaders ────────────────────────────────────────────────────────

class TestGetSectorLeaders:
    def test_returns_top_n_sorted(self):
        rotation = [
            {"etf": "XLF", "change_pct": 0.4},
            {"etf": "XLK", "change_pct": 1.2},
            {"etf": "XLU", "change_pct": -0.8},
        ]
        with patch("agents.tools.scanner_tools.get_sector_rotation", return_value=rotation):
            result = get_sector_leaders(n=2)
        assert len(result) == 2
        assert result[0]["etf"] == "XLK"

    def test_filters_error_rows(self):
        rotation = [{"error": "alpaca timeout"}, {"etf": "XLK", "change_pct": 1.0}]
        with patch("agents.tools.scanner_tools.get_sector_rotation", return_value=rotation):
            result = get_sector_leaders(n=5)
        assert all("error" not in r for r in result)

    def test_n_larger_than_list_returns_all(self):
        rotation = [{"etf": "XLK", "change_pct": 1.0}]
        with patch("agents.tools.scanner_tools.get_sector_rotation", return_value=rotation):
            result = get_sector_leaders(n=10)
        assert len(result) == 1


# ── filter_and_rank ───────────────────────────────────────────────────────────

class TestFilterAndRank:
    def test_drops_below_min_score(self):
        result = filter_and_rank(_CANDIDATES, max_n=25, min_score=5)
        tickers = [c["ticker"] for c in result["candidates"]]
        assert "WEAK" not in tickers
        assert "JUNK" not in tickers

    def test_respects_max_n(self):
        candidates = [
            {"ticker": f"T{i}", "technical_score": 8, "premarket_change_pct": 1.0}
            for i in range(30)
        ]
        result = filter_and_rank(candidates, max_n=10, min_score=1)
        assert result["n_returned"] == 10

    def test_sorted_by_score_then_momentum(self):
        candidates = [
            {"ticker": "LOW",  "technical_score": 5, "premarket_change_pct": 3.0},
            {"ticker": "HIGH", "technical_score": 9, "premarket_change_pct": 0.5},
            {"ticker": "MID",  "technical_score": 7, "premarket_change_pct": 1.0},
        ]
        result = filter_and_rank(candidates, max_n=5, min_score=1)
        tickers = [c["ticker"] for c in result["candidates"]]
        assert tickers[0] == "HIGH"
        assert tickers[1] == "MID"

    def test_caution_mode_uses_stricter_thresholds(self):
        candidates = [
            {"ticker": "STRONG", "technical_score": 8, "premarket_change_pct": 1.0},
            {"ticker": "MIDLOW", "technical_score": 6, "premarket_change_pct": 0.5},
            {"ticker": "WEAKPM", "technical_score": 7, "premarket_change_pct": 0.1},
        ]
        result = filter_and_rank(candidates, max_n=25, min_score=5, caution_mode=True)
        tickers = [c["ticker"] for c in result["candidates"]]
        assert "STRONG" in tickers
        assert "MIDLOW" not in tickers  # score < 7
        assert "WEAKPM" not in tickers  # premarket < 0.3%

    def test_caution_mode_caps_at_15(self):
        candidates = [
            {"ticker": f"T{i}", "technical_score": 8, "premarket_change_pct": 1.0}
            for i in range(25)
        ]
        result = filter_and_rank(candidates, max_n=25, min_score=5, caution_mode=True)
        assert result["n_returned"] <= 15

    def test_dropped_count_accurate(self):
        result = filter_and_rank(_CANDIDATES, max_n=25, min_score=5)
        assert result["dropped_count"] == 2  # WEAK (score=4) and JUNK (score=2)

    def test_empty_candidates_returns_zero(self):
        result = filter_and_rank([], max_n=25, min_score=5)
        assert result["n_returned"] == 0
        assert result["candidates"] == []

    def test_output_includes_full_candidate_objects(self):
        result = filter_and_rank(_CANDIDATES[:2], max_n=5, min_score=1)
        c = result["candidates"][0]
        assert "ticker" in c
        assert "technical_score" in c
        assert "premarket_change_pct" in c

    def test_threshold_applied_in_output(self):
        result = filter_and_rank(_CANDIDATES, max_n=20, min_score=6, caution_mode=False)
        assert "threshold_applied" in result
        assert "score>=6" in result["threshold_applied"]
