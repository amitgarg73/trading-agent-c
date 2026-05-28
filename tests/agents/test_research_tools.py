from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.tools.research_tools import (
    get_atr,
    get_candidates,
    get_intraday_signals,
    get_live_price,
    get_news,
    get_position_history,
)
from tests.conftest import make_query


def _hist_1m(prices, volumes=None):
    n = len(prices)
    if volumes is None:
        volumes = [500_000] * n
    return pd.DataFrame({
        "Open":   [prices[0]] * n,
        "High":   [p * 1.005 for p in prices],
        "Low":    [p * 0.995 for p in prices],
        "Close":  prices,
        "Volume": volumes,
    })


def _hist_daily(prices):
    n = len(prices)
    return pd.DataFrame({
        "Open":   [p * 0.99 for p in prices],
        "High":   [p * 1.01 for p in prices],
        "Low":    [p * 0.98 for p in prices],
        "Close":  prices,
        "Volume": [1_000_000] * n,
    })


# ── get_candidates ─────────────────────────────────────────────────────────────

class TestGetCandidates:
    def test_returns_db_rows(self, mock_supabase):
        rows = [
            {"ticker": "AAPL", "score": 8, "price": 185.0, "sector": "Technology"},
            {"ticker": "MSFT", "score": 7, "price": 420.0, "sector": "Technology"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = get_candidates(min_score=5)
        assert len(result) == 2
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["technical_score"] == 8
        assert result[0]["current_price"] == 185.0

    def test_returns_empty_when_no_results(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_candidates() == []

    def test_returns_error_on_db_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = get_candidates()
        assert "error" in result[0]


# ── get_news ───────────────────────────────────────────────────────────────────

class TestGetNews:
    def test_no_blackout_when_no_calendar(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = None
        mock_ticker.news = []
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_news("AAPL")
        assert result["blackout"] is False
        assert result["reason"] is None

    def test_blackout_when_earnings_today(self):
        mock_ticker = MagicMock()
        today_ts = pd.Timestamp(date.today())
        cal = pd.DataFrame({"Earnings Date": [today_ts]}, index=["Earnings Date"])
        mock_ticker.calendar = cal
        mock_ticker.news = []
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_news("AAPL")
        assert result["blackout"] is True
        assert "earnings" in result["reason"]

    def test_blackout_when_earnings_tomorrow(self):
        mock_ticker = MagicMock()
        tomorrow_ts = pd.Timestamp(date.today() + timedelta(days=1))
        cal = pd.DataFrame({"Earnings Date": [tomorrow_ts]}, index=["Earnings Date"])
        mock_ticker.calendar = cal
        mock_ticker.news = []
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_news("AAPL")
        assert result["blackout"] is True

    def test_no_blackout_when_earnings_far_future(self):
        mock_ticker = MagicMock()
        future_ts = pd.Timestamp(date.today() + timedelta(days=30))
        cal = pd.DataFrame({"Earnings Date": [future_ts]}, index=["Earnings Date"])
        mock_ticker.calendar = cal
        mock_ticker.news = []
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_news("AAPL")
        assert result["blackout"] is False

    def test_headlines_extracted(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = None
        mock_ticker.news = [
            {"title": "Apple Q2 strong"},
            {"title": "iPhone sales up"},
            {"title": "Analyst upgrade"},
            {"title": "Fourth headline"},
        ]
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_news("AAPL")
        assert len(result["headlines"]) == 3

    def test_returns_error_on_exception(self):
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=Exception("err")):
            result = get_news("AAPL")
        assert "error" in result


# ── get_live_price ─────────────────────────────────────────────────────────────

class TestGetLivePrice:
    def test_returns_price_from_1min_bar(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _hist_1m([185.50, 185.75, 186.00])
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_live_price("AAPL")
        assert result["price"] == pytest.approx(186.00)
        assert result["source"] == "yfinance"

    def test_returns_error_on_empty_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_live_price("AAPL")
        assert "error" in result

    def test_returns_error_on_exception(self):
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=Exception("err")):
            result = get_live_price("AAPL")
        assert "error" in result


# ── get_intraday_signals ───────────────────────────────────────────────────────

class TestGetIntradaySignals:
    def _setup_mock(self, stock_prices, spy_prices=None):
        def factory(symbol):
            mock = MagicMock()
            prices = stock_prices if symbol != "SPY" else (spy_prices or [100] * len(stock_prices))
            mock.history.return_value = _hist_1m(prices)
            return mock
        return factory

    def test_above_vwap_when_price_above_average(self):
        prices = [100.0] * 30 + [105.0] * 30
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=self._setup_mock(prices)):
            result = get_intraday_signals("AAPL")
        assert result["above_vwap"] is True

    def test_today_pct_change_calculated(self):
        prices = [100.0] + [102.0] * 59
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=self._setup_mock(prices)):
            result = get_intraday_signals("AAPL")
        assert result["today_pct_change"] == pytest.approx(2.0, rel=1e-2)

    def test_rs_vs_spy_is_none_when_spy_flat(self):
        stock_prices = [100.0] * 60
        spy_prices   = [400.0] * 60
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=self._setup_mock(stock_prices, spy_prices)):
            result = get_intraday_signals("AAPL")
        assert result["rs_vs_spy"] is None

    def test_returns_error_on_empty_data(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_intraday_signals("AAPL")
        assert "error" in result


# ── get_atr ────────────────────────────────────────────────────────────────────

class TestGetAtr:
    def test_atr_pct_is_positive(self):
        prices = [100.0 + i * 0.5 for i in range(20)]
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = [_hist_daily(prices), pd.DataFrame()]
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_atr("AAPL")
        assert result["atr_pct"] > 0

    def test_returns_error_on_insufficient_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _hist_daily([100.0])
        with patch("agents.tools.research_tools.yf.Ticker", return_value=mock_ticker):
            result = get_atr("AAPL")
        assert "error" in result

    def test_returns_error_on_exception(self):
        with patch("agents.tools.research_tools.yf.Ticker", side_effect=Exception("err")):
            result = get_atr("AAPL")
        assert "error" in result


# ── get_position_history ───────────────────────────────────────────────────────

class TestGetPositionHistory:
    def test_win_rate_calculated(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 100.0, "exit_reason": "TARGET", "status": "closed"},
            {"realized_pnl":  50.0, "exit_reason": "TARGET", "status": "closed"},
            {"realized_pnl": -30.0, "exit_reason": "STOP",   "status": "closed"},
            {"realized_pnl": -20.0, "exit_reason": "STOP",   "status": "closed"},
        ])
        result = get_position_history("AAPL")
        assert result["trades"] == 4
        assert result["wins"] == 2
        assert result["win_rate_pct"] == pytest.approx(50.0)

    def test_avg_pnl_calculated(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 100.0, "exit_reason": "TARGET", "status": "closed"},
            {"realized_pnl": -60.0, "exit_reason": "STOP",   "status": "closed"},
        ])
        result = get_position_history("AAPL")
        assert result["avg_pnl"] == pytest.approx(20.0)

    def test_empty_history_returns_zeros(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = get_position_history("AAPL")
        assert result["trades"] == 0
        assert result["win_rate_pct"] == 0.0
        assert result["last_exit"] is None

    def test_last_exit_reason_returned(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 50.0, "exit_reason": "TARGET", "status": "closed"},
        ])
        result = get_position_history("AAPL")
        assert result["last_exit"] == "TARGET"

    def test_returns_error_on_db_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = get_position_history("AAPL")
        assert "error" in result
