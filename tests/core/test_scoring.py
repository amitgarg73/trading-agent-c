from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── _entry_timing_pct ─────────────────────────────────────────────────────────

class TestEntryTimingPct:
    def _call(self, entry, high, low):
        from core.scoring import _entry_timing_pct
        return _entry_timing_pct(entry, high, low)

    def test_bought_the_low_returns_100(self):
        assert self._call(10.0, 12.0, 10.0) == 100.0

    def test_bought_the_high_returns_0(self):
        assert self._call(12.0, 12.0, 10.0) == 0.0

    def test_midpoint_returns_50(self):
        assert self._call(11.0, 12.0, 10.0) == pytest.approx(50.0)

    def test_zero_range_returns_none(self):
        assert self._call(10.0, 10.0, 10.0) is None

    def test_entry_above_high_clamps_to_0(self):
        assert self._call(15.0, 12.0, 10.0) == 0.0

    def test_entry_below_low_clamps_to_100(self):
        assert self._call(8.0, 12.0, 10.0) == 100.0


# ── _r_multiple ───────────────────────────────────────────────────────────────

class TestRMultiple:
    def _call(self, pnl, entry, stop, shares):
        from core.scoring import _r_multiple
        return _r_multiple(pnl, entry, stop, shares)

    def test_2r_win(self):
        # risk = (185 - 183) * 10 = $20, pnl = $40 → 2.0R
        assert self._call(40.0, 185.0, 183.0, 10) == pytest.approx(2.0)

    def test_1r_loss(self):
        # risk = $20, pnl = -$20 → -1.0R
        assert self._call(-20.0, 185.0, 183.0, 10) == pytest.approx(-1.0)

    def test_zero_risk_returns_none(self):
        # entry == stop → no risk defined
        assert self._call(10.0, 185.0, 185.0, 10) is None

    def test_zero_shares_returns_none(self):
        assert self._call(10.0, 185.0, 183.0, 0) is None

    def test_zero_entry_returns_none(self):
        assert self._call(10.0, 0.0, 183.0, 10) is None

    def test_partial_r(self):
        # risk = $20, pnl = $10 → 0.5R
        assert self._call(10.0, 185.0, 183.0, 10) == pytest.approx(0.5)


# ── score_trades ──────────────────────────────────────────────────────────────

def _make_position(ticker="AAPL", entry=185.0, stop=183.0, target=192.0,
                   shares=10, pnl=40.0, confidence="HIGH"):
    return {
        "id":           f"pos-{ticker}",
        "ticker":       ticker,
        "entry_price":  entry,
        "stop_loss":    stop,
        "target_price": target,
        "shares":       shares,
        "realized_pnl": pnl,
        "exit_reason":  "NATIVE_TRAIL",
        "confidence":   confidence,
    }


def _mock_db(positions):
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = positions
    db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()
    return db


class TestScoreTrades:
    def _ohlc(self, ticker, open_=180.0, high=192.0, low=179.0, close=189.0):
        pct = round((close - open_) / open_ * 100, 2)
        return {ticker: {"open": open_, "high": high, "low": low, "close": close, "pct_move": pct}}

    def test_returns_trades_scored_count(self):
        pos = _make_position("AAPL", entry=185.0, stop=183.0, shares=10, pnl=40.0)
        ohlc = self._ohlc("AAPL", open_=180.0, high=192.0, low=179.0, close=189.0)
        with patch("core.scoring.get_client", return_value=_mock_db([pos])), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=[]):
            from core.scoring import score_trades
            result = score_trades("sess-001", "2026-06-17")
        assert result["trades_scored"] == 1

    def test_directional_hit_true_when_close_above_entry(self):
        # entry=185, close=189 → directional_hit=True
        pos = _make_position("AAPL", entry=185.0, pnl=40.0)
        ohlc = self._ohlc("AAPL", open_=180.0, high=192.0, low=179.0, close=189.0)
        captured = {}
        db = _mock_db([pos])
        def capture_update(updates):
            captured.update(updates)
            return db.table.return_value.update.return_value
        db.table.return_value.update.side_effect = lambda u: capture_update(u)
        with patch("core.scoring.get_client", return_value=db), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=[]):
            from core.scoring import score_trades
            score_trades("sess-001", "2026-06-17")
        assert captured.get("directional_hit") is True

    def test_directional_hit_false_when_close_below_entry(self):
        # entry=185, close=178 → directional_hit=False
        pos = _make_position("AAPL", entry=185.0, pnl=-20.0)
        ohlc = self._ohlc("AAPL", open_=180.0, high=186.0, low=177.0, close=178.0)
        captured = {}
        db = _mock_db([pos])
        db.table.return_value.update.side_effect = lambda u: (captured.update(u), db.table.return_value.update.return_value)[1]
        with patch("core.scoring.get_client", return_value=db), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=[]):
            from core.scoring import score_trades
            score_trades("sess-001", "2026-06-17")
        assert captured.get("directional_hit") is False

    def test_returns_zero_when_no_trades(self):
        db = _mock_db([])
        with patch("core.scoring.get_client", return_value=db):
            from core.scoring import score_trades
            result = score_trades("sess-001", "2026-06-17")
        assert result["trades_scored"] == 0

    def test_r_multiple_in_summary(self):
        # risk = (185-183)*10=$20, pnl=$40 → 2.0R
        pos = _make_position("AAPL", entry=185.0, stop=183.0, shares=10, pnl=40.0)
        ohlc = self._ohlc("AAPL", close=189.0)
        with patch("core.scoring.get_client", return_value=_mock_db([pos])), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=[]):
            from core.scoring import score_trades
            result = score_trades("sess-001", "2026-06-17")
        assert result["avg_r_multiple"] == pytest.approx(2.0)

    def test_opportunity_cost_json_written(self):
        pos = _make_position("AAPL", entry=185.0, pnl=40.0)
        ohlc = self._ohlc("AAPL", close=189.0)
        top  = [{"ticker": "NVDA", "pct_move": 9.5}]
        written = {}
        db = _mock_db([pos])
        db.table.return_value.update.side_effect = lambda u: (written.update(u), db.table.return_value.update.return_value)[1]
        with patch("core.scoring.get_client", return_value=db), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=top):
            from core.scoring import score_trades
            score_trades("sess-001", "2026-06-17")
        opp = json.loads(written.get("opportunity_cost", "{}"))
        assert opp["top_movers"][0]["ticker"] == "NVDA"

    def test_migration_needed_returns_error_gracefully(self):
        pos = _make_position("AAPL", pnl=40.0)
        ohlc = self._ohlc("AAPL", close=189.0)
        db = _mock_db([pos])
        db.table.return_value.update.return_value.eq.return_value.execute.side_effect = \
            Exception("column c_positions.r_multiple does not exist")
        with patch("core.scoring.get_client", return_value=db), \
             patch("core.scoring._fetch_day_ohlc", return_value=ohlc), \
             patch("core.scoring._fetch_top_movers", return_value=[]):
            from core.scoring import score_trades
            result = score_trades("sess-001", "2026-06-17")
        assert result.get("error") == "migration_needed"
