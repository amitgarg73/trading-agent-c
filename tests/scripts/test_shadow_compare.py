from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from scripts.shadow_compare import (
    _avg_planned_rr,
    _trading_dates_before,
    compare_day,
    load_a_day,
    load_c_day,
    print_day_report,
    print_multi_day_table,
)

_DATE = "2026-05-27"

_C_CANDIDATES = [
    {"ticker": "AAPL", "score": 9, "price": 185.0},
    {"ticker": "MSFT", "score": 7, "price": 420.0},
    {"ticker": "TSLA", "score": 6, "price": 175.0},
]

_C_POSITIONS = [
    {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.4, "stop_loss": 183.78,
     "position_size": 3500, "confidence": "HIGH", "realized_pnl": 133.0, "status": "closed"},
    {"ticker": "MSFT", "entry_price": 420.0, "target_price": 436.8, "stop_loss": 417.21,
     "position_size": 3500, "confidence": "MEDIUM", "realized_pnl": -45.0, "status": "closed"},
]

_A_POSITIONS_RAW = [
    {"ticker": "AAPL", "entry_price": 184.8, "fill_price": 184.90, "target_price": 192.2,
     "stop_loss": 183.6, "position_size": 3500, "realized_pnl": 110.0,
     "close_reason": "TARGET", "status": "CLOSED", "opened_at": f"{_DATE}T09:30:00"},
    {"ticker": "NVDA", "entry_price": 920.0, "fill_price": None, "target_price": 956.8,
     "stop_loss": 913.86, "position_size": 3000, "realized_pnl": -55.0,
     "close_reason": "STOP", "status": "CLOSED", "opened_at": f"{_DATE}T09:32:00"},
]


# ── _avg_planned_rr ────────────────────────────────────────────────────────────

class TestAvgPlannedRR:
    def test_standard_rr_calculation(self):
        positions = [{"entry_price": 100.0, "target_price": 104.0, "stop_loss": 99.33}]
        assert _avg_planned_rr(positions) == pytest.approx(4.0 / 0.67, rel=0.01)

    def test_averages_across_multiple_positions(self):
        positions = [
            {"entry_price": 100.0, "target_price": 104.0, "stop_loss": 98.0},   # R:R = 2.0
            {"entry_price": 100.0, "target_price": 106.0, "stop_loss": 98.0},   # R:R = 3.0
        ]
        assert _avg_planned_rr(positions) == pytest.approx(2.5)

    def test_skips_positions_with_missing_prices(self):
        positions = [
            {"entry_price": None, "target_price": 104.0, "stop_loss": 98.0},
            {"entry_price": 100.0, "target_price": 104.0, "stop_loss": 98.0},
        ]
        assert _avg_planned_rr(positions) == pytest.approx(2.0)

    def test_returns_zero_when_no_valid_positions(self):
        assert _avg_planned_rr([]) == 0.0
        assert _avg_planned_rr([{"entry_price": None}]) == 0.0


# ── _trading_dates_before ──────────────────────────────────────────────────────

class TestTradingDatesBefore:
    def test_returns_n_weekdays(self):
        from datetime import date
        end = date(2026, 5, 27)   # Wednesday
        dates = _trading_dates_before(end, 5)
        assert len(dates) == 5

    def test_skips_weekends(self):
        from datetime import date
        end = date(2026, 5, 25)   # Monday
        dates = _trading_dates_before(end, 3)
        for d in dates:
            from datetime import date as date_cls
            assert date_cls.fromisoformat(d).weekday() < 5


# ── compare_day ────────────────────────────────────────────────────────────────

class TestCompareDay:
    def _c_data(self, positions=None, candidates=None):
        return {
            "session":    {"id": "sess-1", "terminal_reason": "converged"},
            "candidates": candidates if candidates is not None else _C_CANDIDATES,
            "positions":  positions  if positions  is not None else _C_POSITIONS,
        }

    def _a_data(self, positions=None):
        normalized = [
            {"ticker": p["ticker"], "entry_price": p.get("fill_price") or p.get("entry_price"),
             "target_price": p.get("target_price"), "stop_loss": p.get("stop_loss"),
             "position_size": p.get("position_size"), "realized_pnl": p.get("realized_pnl"),
             "exit_reason": p.get("close_reason"), "status": (p.get("status") or "").lower()}
            for p in (positions or _A_POSITIONS_RAW)
        ]
        return {"positions": normalized}

    def test_c_pnl_sums_closed_positions(self):
        comp = compare_day(_DATE, self._c_data(), None)
        assert comp["c_pnl"] == pytest.approx(88.0)   # 133 - 45

    def test_c_wins_and_losses(self):
        comp = compare_day(_DATE, self._c_data(), None)
        assert comp["c_wins"]   == 1
        assert comp["c_losses"] == 1

    def test_c_planned_rr_positive(self):
        comp = compare_day(_DATE, self._c_data(), None)
        assert comp["c_planned_rr"] > 0

    def test_candidate_count_and_score_range(self):
        comp = compare_day(_DATE, self._c_data(), None)
        assert comp["c_candidates"]    == 3
        assert comp["c_score_range"]   == (6, 9)

    def test_a_data_none_when_not_provided(self):
        comp = compare_day(_DATE, self._c_data(), None)
        assert comp["a_data"] is None

    def test_overlap_identified(self):
        comp = compare_day(_DATE, self._c_data(), self._a_data())
        assert "AAPL" in comp["a_data"]["overlap"]

    def test_c_only_picks_identified(self):
        comp = compare_day(_DATE, self._c_data(), self._a_data())
        assert "MSFT" in comp["a_data"]["c_only"]

    def test_a_only_in_pool_identified(self):
        # NVDA is in A's trades but NOT in C's scan candidates
        comp = compare_day(_DATE, self._c_data(), self._a_data())
        a = comp["a_data"]
        assert "NVDA" in a["a_only"]
        assert "NVDA" not in a["a_only_in_c_pool"]

    def test_a_only_in_pool_when_ticker_was_scanned(self):
        # Add TSLA to A's trades; TSLA IS in C's scan pool
        a_positions = [
            *_A_POSITIONS_RAW,
            {"ticker": "TSLA", "entry_price": 175.0, "fill_price": None,
             "target_price": 182.0, "stop_loss": 173.83, "position_size": 3000,
             "realized_pnl": 70.0, "close_reason": "TARGET",
             "status": "CLOSED", "opened_at": f"{_DATE}T09:35:00"},
        ]
        comp = compare_day(_DATE, self._c_data(), self._a_data(a_positions))
        assert "TSLA" in comp["a_data"]["a_only_in_c_pool"]


# ── load_c_day ─────────────────────────────────────────────────────────────────

class TestLoadCDay:
    def _mock_db(self, session_rows, candidates, positions):
        db = MagicMock()
        q  = MagicMock()
        q.select.return_value = q
        q.eq.return_value     = q
        q.gte.return_value    = q
        q.lt.return_value     = q
        q.order.return_value  = q
        q.limit.return_value  = q

        call_count = [0]
        results    = [
            MagicMock(data=session_rows),
            MagicMock(data=candidates),
            MagicMock(data=positions),
        ]

        def execute():
            i = min(call_count[0], len(results) - 1)
            call_count[0] += 1
            return results[i]

        q.execute.side_effect = execute
        db.table.return_value = q
        return db

    def test_returns_none_session_when_no_rows(self):
        db = self._mock_db([], [], [])
        result = load_c_day(db, _DATE)
        assert result["session"] is None
        assert result["candidates"] == []
        assert result["positions"]  == []

    def test_returns_candidates_and_positions(self):
        session_row = [{"id": "sess-1", "terminal_reason": "converged", "trades_executed": 2}]
        db = self._mock_db(session_row, _C_CANDIDATES, _C_POSITIONS)
        result = load_c_day(db, _DATE)
        assert result["session"]["id"] == "sess-1"
        assert len(result["candidates"]) == 3
        assert len(result["positions"])  == 2


# ── load_a_day ─────────────────────────────────────────────────────────────────

class TestLoadADay:
    def _mock_db(self, rows):
        db = MagicMock()
        q  = MagicMock()
        q.select.return_value = q
        q.gte.return_value    = q
        q.lt.return_value     = q
        q.execute.return_value = MagicMock(data=rows)
        db.table.return_value  = q
        return db

    def test_normalizes_status_to_lowercase(self):
        db     = self._mock_db(_A_POSITIONS_RAW)
        result = load_a_day(db, _DATE)
        for p in result["positions"]:
            assert p["status"] == p["status"].lower()

    def test_uses_fill_price_over_entry_price(self):
        row    = {**_A_POSITIONS_RAW[0], "fill_price": 184.99}
        db     = self._mock_db([row])
        result = load_a_day(db, _DATE)
        assert result["positions"][0]["entry_price"] == pytest.approx(184.99)

    def test_falls_back_to_entry_price_when_no_fill(self):
        row    = {**_A_POSITIONS_RAW[1], "fill_price": None}
        db     = self._mock_db([row])
        result = load_a_day(db, _DATE)
        assert result["positions"][0]["entry_price"] == pytest.approx(920.0)

    def test_maps_close_reason_to_exit_reason(self):
        db     = self._mock_db(_A_POSITIONS_RAW)
        result = load_a_day(db, _DATE)
        assert result["positions"][0]["exit_reason"] == "TARGET"


# ── print_day_report ───────────────────────────────────────────────────────────

class TestPrintDayReport:
    def _comp_c_only(self):
        c_data = {
            "session":    {"id": "s1"},
            "candidates": _C_CANDIDATES,
            "positions":  _C_POSITIONS,
        }
        return compare_day(_DATE, c_data, None)

    def test_date_in_output(self, capsys):
        print_day_report(self._comp_c_only())
        out = capsys.readouterr().out
        assert _DATE in out

    def test_tickers_in_output(self, capsys):
        print_day_report(self._comp_c_only())
        out = capsys.readouterr().out
        assert "AAPL" in out
        assert "MSFT" in out

    def test_no_strategy_a_prompt_when_a_missing(self, capsys):
        print_day_report(self._comp_c_only())
        out = capsys.readouterr().out
        assert "SUPABASE_URL_A" in out


# ── print_multi_day_table ──────────────────────────────────────────────────────

class TestPrintMultiDayTable:
    def _comparisons(self):
        c_data = {
            "session":    {"id": "s1"},
            "candidates": _C_CANDIDATES,
            "positions":  _C_POSITIONS,
        }
        return [
            compare_day("2026-05-26", c_data, None),
            compare_day("2026-05-27", c_data, None),
        ]

    def test_total_row_present(self, capsys):
        print_multi_day_table(self._comparisons())
        out = capsys.readouterr().out
        assert "Total" in out

    def test_both_dates_appear(self, capsys):
        print_multi_day_table(self._comparisons())
        out = capsys.readouterr().out
        assert "2026-05-26" in out
        assert "2026-05-27" in out
