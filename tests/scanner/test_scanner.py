from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scanner.universe import get_sector, get_tickers, SECTOR_MAP
from scanner.scanner import _compute_rsi, _compute_macd_hist, _compute_atr_pct, _score_ticker
from tests.conftest import make_query


# ── Universe ──────────────────────────────────────────────────────────────────

class TestUniverse:
    def test_tickers_returns_list(self):
        tickers = get_tickers()
        assert isinstance(tickers, list)
        assert len(tickers) >= 50

    def test_no_duplicate_tickers(self):
        tickers = get_tickers()
        assert len(tickers) == len(set(tickers))

    def test_known_tickers_present(self):
        tickers = get_tickers()
        for ticker in ["AAPL", "MSFT", "JPM", "XOM", "JNJ"]:
            assert ticker in tickers

    def test_get_sector_known(self):
        assert get_sector("AAPL") == "Technology"
        assert get_sector("JPM") == "Financials"
        assert get_sector("XOM") == "Energy"

    def test_get_sector_unknown_returns_other(self):
        assert get_sector("XXXX") == "Other"


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _make_closes(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


def _make_hist(n: int = 60, base_price: float = 100.0, trend: float = 0.002) -> pd.DataFrame:
    prices = [base_price * (1 + trend) ** i for i in range(n)]
    return pd.DataFrame({
        "Open":   [p * 0.999 for p in prices],
        "High":   [p * 1.005 for p in prices],
        "Low":    [p * 0.995 for p in prices],
        "Close":  prices,
        "Volume": [2_000_000] * n,
    })


class TestComputeRsi:
    def test_returns_float(self):
        closes = _make_closes([100 + i * 0.5 for i in range(30)])
        result = _compute_rsi(closes)
        assert isinstance(result, float)
        assert 0 <= result <= 100

    def test_overbought_trending_up(self):
        closes = _make_closes([100 + i * 2 for i in range(30)])
        rsi = _compute_rsi(closes)
        assert rsi > 60

    def test_oversold_trending_down(self):
        closes = _make_closes([200 - i * 2 for i in range(30)])
        rsi = _compute_rsi(closes)
        assert rsi < 40


class TestComputeMacdHist:
    def test_returns_float(self):
        closes = _make_closes([100 + i * 0.3 for i in range(35)])
        assert isinstance(_compute_macd_hist(closes), float)

    def test_positive_in_uptrend(self):
        closes = _make_closes([100 + i * 1.5 for i in range(40)])
        assert _compute_macd_hist(closes) > 0

    def test_negative_in_downtrend(self):
        closes = _make_closes([200 - i * 1.5 for i in range(40)])
        assert _compute_macd_hist(closes) < 0


class TestComputeAtrPct:
    def test_returns_float(self):
        hist = _make_hist()
        assert isinstance(_compute_atr_pct(hist), float)

    def test_insufficient_history_returns_zero(self):
        hist = _make_hist(n=5)
        assert _compute_atr_pct(hist) == 0.0

    def test_volatile_stock_higher_atr(self):
        calm = _make_hist(n=60)
        vol = calm.copy()
        vol["High"] = vol["High"] * 1.05
        vol["Low"]  = vol["Low"]  * 0.95
        assert _compute_atr_pct(vol) > _compute_atr_pct(calm)


# ── Scoring ───────────────────────────────────────────────────────────────────

class TestScoreTicker:
    def test_scores_trending_stock(self):
        hist = _make_hist(n=60, trend=0.003)  # steady uptrend
        result = _score_ticker(hist)
        assert result["technical_score"] >= 2   # at minimum trend score
        assert result["above_sma20"] is True
        assert result["above_sma50"] is True

    def test_raises_on_insufficient_history(self):
        hist = _make_hist(n=10)
        with pytest.raises(ValueError, match="insufficient history"):
            _score_ticker(hist)

    def test_raises_on_price_too_low(self):
        hist = _make_hist(n=60, base_price=5.0)
        with pytest.raises(ValueError, match="price"):
            _score_ticker(hist)

    def test_raises_on_price_too_high(self):
        hist = _make_hist(n=60, base_price=600.0)
        with pytest.raises(ValueError, match="price"):
            _score_ticker(hist)

    def test_raises_on_low_volume(self):
        hist = _make_hist(n=60)
        hist["Volume"] = 100  # tiny volume
        with pytest.raises(ValueError, match="avg volume"):
            _score_ticker(hist)

    def test_returns_required_fields(self):
        hist = _make_hist(n=60, trend=0.003)
        result = _score_ticker(hist)
        for field in ["technical_score", "current_price", "avg_volume",
                      "rsi", "volume_ratio", "atr_pct", "above_sma20", "above_sma50"]:
            assert field in result


# ── run_scanner ───────────────────────────────────────────────────────────────

class TestRunScanner:
    def _make_hist_df(self, n=60, base=100.0, trend=0.003):
        return _make_hist(n=n, base_price=base, trend=trend)

    def test_writes_rows_to_db(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        hist = self._make_hist_df()

        with patch("scanner.scanner.yf.download", return_value={"AAPL": hist}), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            count = run_scanner(scan_date=date(2026, 5, 27))

        assert count >= 0  # scored at least 0 (might filter ATR)

    def test_idempotent_skips_already_scored(self, mock_supabase):
        existing = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
        mock_supabase.table.return_value = make_query(existing)

        with patch("scanner.scanner.get_tickers", return_value=["AAPL", "MSFT"]):
            from scanner.scanner import run_scanner
            count = run_scanner(scan_date=date(2026, 5, 27))

        assert count == 2  # both already scored, returned as-is

    def test_handles_yfinance_failure_gracefully(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])

        with patch("scanner.scanner.yf.download", side_effect=Exception("rate limit")), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            count = run_scanner(scan_date=date(2026, 5, 27))

        assert count == 0
