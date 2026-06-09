from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.tools.research_tools import (
    get_atr,
    get_candidates,
    get_float_short_interest,
    get_intraday_signals,
    get_news,
    get_position_history,
    get_premarket_snapshot,
    get_premarket_volume,
    get_prev_day_levels,
    get_ticker_fundamentals,
    get_ticker_market_data,
)
from tests.conftest import make_query


def _make_bar(close, open_=None, high=None, low=None, volume=500_000):
    b = MagicMock()
    b.close  = close
    b.open   = open_ if open_ is not None else close * 0.998
    b.high   = high  if high  is not None else close * 1.005
    b.low    = low   if low   is not None else close * 0.995
    b.volume = volume
    return b


def _alpaca_dclient(bars_by_symbol: dict):
    resp = MagicMock()
    resp.data = bars_by_symbol
    client = MagicMock()
    client.get_stock_bars.return_value = resp
    return MagicMock(return_value=client)


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

def _news_item(headline: str, days_ago: float = 1.0) -> MagicMock:
    item = MagicMock()
    item.headline   = headline
    item.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return item


def _news_client_mock(items: list) -> patch:
    news_set      = MagicMock()
    news_set.data = {"news": items}
    client        = MagicMock()
    client.get_news.return_value = news_set
    return patch("core.alpaca._nclient", MagicMock(return_value=client))


class TestGetNews:
    def test_no_blackout_with_normal_headlines(self):
        items = [_news_item("Apple launches new product"), _news_item("CEO interview")]
        with _news_client_mock(items):
            result = get_news("AAPL")
        assert result["blackout"] is False
        assert result["reason"] is None

    def test_blackout_on_earnings_keyword_recent(self):
        items = [_news_item("Apple reports earnings beat", days_ago=1)]
        with _news_client_mock(items):
            result = get_news("AAPL")
        assert result["blackout"] is True
        assert "earnings" in result["reason"]

    def test_blackout_on_eps_keyword(self):
        items = [_news_item("AAPL beats EPS estimates for Q2", days_ago=0)]
        with _news_client_mock(items):
            result = get_news("AAPL")
        assert result["blackout"] is True

    def test_no_blackout_when_earnings_headline_too_old(self):
        items = [_news_item("Company reports quarterly results", days_ago=5)]
        with _news_client_mock(items):
            result = get_news("AAPL")
        assert result["blackout"] is False

    def test_headlines_limited_to_3(self):
        items = [_news_item(f"Headline {i}") for i in range(5)]
        with _news_client_mock(items):
            result = get_news("AAPL")
        assert len(result["headlines"]) == 3

    def test_empty_news_returns_no_blackout(self):
        with _news_client_mock([]):
            result = get_news("AAPL")
        assert result["blackout"] is False
        assert result["headlines"] == []

    def test_returns_error_on_exception(self):
        with patch("core.alpaca._nclient", MagicMock(side_effect=Exception("network"))):
            result = get_news("AAPL")
        assert "error" in result



# ── get_intraday_signals ───────────────────────────────────────────────────────

_MARKET_SESSION = patch("agents.tools.research_tools._is_premarket", return_value=False, create=True)


class TestGetIntradaySignals:
    def _bars_map(self, stock_closes, spy_closes=None):
        stock = [_make_bar(c, open_=stock_closes[0]) for c in stock_closes]
        spy   = [_make_bar(c, open_=spy_closes[0] if spy_closes else 400.0)
                 for c in (spy_closes or [400.0] * len(stock_closes))]
        return {"AAPL": stock, "SPY": spy}

    def test_premarket_returns_unavailable(self):
        with patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_intraday_signals("AAPL")
        assert result["available"] is False
        assert result["reason"] == "pre-market"
        assert result["above_vwap"] is None
        assert result["rs_vs_spy"] is None

    def test_above_vwap_when_price_above_average(self):
        closes = [100.0] * 30 + [105.0] * 30
        with _MARKET_SESSION, patch("core.alpaca._dclient", _alpaca_dclient(self._bars_map(closes))):
            result = get_intraday_signals("AAPL")
        assert result["above_vwap"] is True

    def test_today_pct_change_calculated(self):
        closes = [100.0] + [102.0] * 59
        with _MARKET_SESSION, patch("core.alpaca._dclient", _alpaca_dclient(self._bars_map(closes))):
            result = get_intraday_signals("AAPL")
        assert result["today_pct_change"] == pytest.approx(2.0, rel=1e-2)

    def test_rs_vs_spy_is_none_when_spy_flat(self):
        closes = [100.0] * 60
        spy_c  = [400.0] * 60
        with _MARKET_SESSION, patch("core.alpaca._dclient", _alpaca_dclient(self._bars_map(closes, spy_c))):
            result = get_intraday_signals("AAPL")
        assert result["rs_vs_spy"] is None

    def test_returns_error_when_no_bars(self):
        with _MARKET_SESSION, patch("core.alpaca._dclient", _alpaca_dclient({"AAPL": [], "SPY": []})):
            result = get_intraday_signals("AAPL")
        assert "error" in result


# ── get_atr ────────────────────────────────────────────────────────────────────

class TestGetAtr:
    def _daily_bars(self, prices):
        return [_make_bar(p, open_=p * 0.99, high=p * 1.01, low=p * 0.98) for p in prices]

    def test_atr_pct_is_positive(self):
        prices = [100.0 + i * 0.5 for i in range(20)]
        bars_map = {"AAPL": self._daily_bars(prices)}
        # Second _dclient call (ORB) returns empty
        resp1 = MagicMock(); resp1.data = bars_map
        resp2 = MagicMock(); resp2.data = {"AAPL": []}
        client = MagicMock()
        client.get_stock_bars.side_effect = [resp1, resp2]
        with patch("core.alpaca._dclient", MagicMock(return_value=client)):
            result = get_atr("AAPL")
        assert result["atr_pct"] > 0

    def test_returns_error_on_insufficient_history(self):
        bars_map = {"AAPL": self._daily_bars([100.0])}
        with patch("core.alpaca._dclient", _alpaca_dclient(bars_map)):
            result = get_atr("AAPL")
        assert "error" in result

    def test_returns_error_on_exception(self):
        mock_dc = MagicMock(side_effect=Exception("network"))
        with patch("core.alpaca._dclient", mock_dc):
            result = get_atr("AAPL")
        assert "error" in result


# ── get_premarket_snapshot ─────────────────────────────────────────────────────

def _mock_quote(ask=0.0, bid=0.0):
    q = MagicMock()
    q.ask_price = str(ask)
    q.bid_price = str(bid)
    return q


class TestGetPremarketSnapshot:
    def _setup(self, mock_supabase, rows, quotes_by_ticker):
        mock_supabase.table.return_value = make_query(rows)
        resp = MagicMock()
        resp_data = {t: _mock_quote(**kw) for t, kw in quotes_by_ticker.items()}
        client = MagicMock()
        client.get_stock_latest_quote.return_value = resp_data
        return patch("core.alpaca._dclient", MagicMock(return_value=client))

    def test_change_pct_calculated_correctly(self, mock_supabase):
        rows = [{"ticker": "AAPL", "score": 8, "price": 185.0}]
        dc = self._setup(mock_supabase, rows, {"AAPL": {"ask": 187.035}})
        with dc:
            result = get_premarket_snapshot(["AAPL"])
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["premarket_change_pct"] == pytest.approx(1.1, abs=0.05)

    def test_sorted_best_to_worst(self, mock_supabase):
        rows = [
            {"ticker": "AAPL", "score": 8, "price": 185.0},
            {"ticker": "MSFT", "score": 7, "price": 420.0},
        ]
        dc = self._setup(mock_supabase, rows, {
            "AAPL": {"ask": 183.0},   # -1.1% (loser)
            "MSFT": {"ask": 424.2},   # +1.0% (winner)
        })
        with dc:
            result = get_premarket_snapshot(["AAPL", "MSFT"])
        assert result[0]["ticker"] == "MSFT"
        assert result[1]["ticker"] == "AAPL"

    def test_falls_back_to_bid_when_ask_zero(self, mock_supabase):
        rows = [{"ticker": "AAPL", "score": 8, "price": 185.0}]
        dc = self._setup(mock_supabase, rows, {"AAPL": {"ask": 0.0, "bid": 186.0}})
        with dc:
            result = get_premarket_snapshot(["AAPL"])
        assert result[0]["premarket_price"] == pytest.approx(186.0)

    def test_no_price_when_ticker_not_in_quotes(self, mock_supabase):
        rows = [{"ticker": "AAPL", "score": 8, "price": 185.0}]
        mock_supabase.table.return_value = make_query(rows)
        resp = MagicMock()
        client = MagicMock()
        client.get_stock_latest_quote.return_value = {}   # empty — no quotes
        with patch("core.alpaca._dclient", MagicMock(return_value=client)):
            result = get_premarket_snapshot(["AAPL"])
        assert result[0]["premarket_price"] is None
        assert result[0]["premarket_change_pct"] is None

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db down")
        result = get_premarket_snapshot(["AAPL"])
        assert "error" in result[0]


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


# ── get_premarket_volume ───────────────────────────────────────────────────────

def _two_call_dclient(pm_bars, daily_bars):
    resp1 = MagicMock(); resp1.data = {"AAPL": pm_bars}
    resp2 = MagicMock(); resp2.data = {"AAPL": daily_bars}
    client = MagicMock()
    client.get_stock_bars.side_effect = [resp1, resp2]
    return MagicMock(return_value=client)


class TestGetPremarketVolume:
    def test_high_conviction_above_15pct(self):
        pm_bars    = [_make_bar(185.0, volume=300_000)] * 5   # 1.5M total
        daily_bars = [_make_bar(185.0, volume=10_000_000)] * 20  # 10M avg
        with patch("core.alpaca._dclient", _two_call_dclient(pm_bars, daily_bars)):
            result = get_premarket_volume("AAPL")
        assert result["conviction"] == "HIGH"
        assert result["premarket_volume"] == 1_500_000

    def test_low_conviction_below_5pct(self):
        pm_bars    = [_make_bar(185.0, volume=10_000)] * 3    # 30K total
        daily_bars = [_make_bar(185.0, volume=5_000_000)] * 20  # 5M avg
        with patch("core.alpaca._dclient", _two_call_dclient(pm_bars, daily_bars)):
            result = get_premarket_volume("AAPL")
        assert result["conviction"] == "LOW"

    def test_moderate_conviction_between_5_and_15pct(self):
        pm_bars    = [_make_bar(185.0, volume=100_000)] * 5   # 500K total
        daily_bars = [_make_bar(185.0, volume=5_000_000)] * 20  # 5M avg → 10%
        with patch("core.alpaca._dclient", _two_call_dclient(pm_bars, daily_bars)):
            result = get_premarket_volume("AAPL")
        assert result["conviction"] == "MODERATE"

    def test_returns_error_on_exception(self):
        with patch("core.alpaca._dclient", MagicMock(side_effect=Exception("network"))):
            result = get_premarket_volume("AAPL")
        assert "error" in result


# ── get_float_short_interest ───────────────────────────────────────────────────

class TestGetFloatShortInterest:
    def _mock_info(self, float_shares=None, short_pct=None, short_ratio=None):
        info = {}
        if float_shares is not None: info["floatShares"] = float_shares
        if short_pct    is not None: info["shortPercentOfFloat"] = short_pct
        if short_ratio  is not None: info["shortRatio"] = short_ratio
        mock_t = MagicMock()
        mock_t.info = info
        return patch("yfinance.Ticker", return_value=mock_t)

    def test_squeeze_potential_on_low_float_high_short(self):
        with self._mock_info(float_shares=5_000_000, short_pct=0.25, short_ratio=3.5):
            result = get_float_short_interest("AAPL")
        assert result["squeeze_potential"] is True
        assert result["low_float"] is True
        assert result["float_shares_m"] == pytest.approx(5.0)
        assert result["short_pct_float"] == pytest.approx(25.0)

    def test_no_squeeze_on_large_float(self):
        with self._mock_info(float_shares=200_000_000, short_pct=0.25):
            result = get_float_short_interest("AAPL")
        assert result["squeeze_potential"] is False
        assert result["low_float"] is False

    def test_no_squeeze_on_low_short_interest(self):
        with self._mock_info(float_shares=5_000_000, short_pct=0.05):
            result = get_float_short_interest("AAPL")
        assert result["squeeze_potential"] is False

    def test_returns_error_on_exception(self):
        with patch("yfinance.Ticker", side_effect=Exception("network")):
            result = get_float_short_interest("AAPL")
        assert "error" in result


# ── get_prev_day_levels ────────────────────────────────────────────────────────

class TestGetPrevDayLevels:
    def _mock_history(self, rows):
        import pandas as pd
        df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"])
        mock_t = MagicMock()
        mock_t.history.return_value = df
        return patch("yfinance.Ticker", return_value=mock_t)

    def test_returns_pdh_pdl_pdc(self):
        rows = [
            [183.0, 186.0, 182.5, 185.0, 1_000_000],
            [185.0, 188.0, 184.0, 187.0, 1_200_000],
        ]
        with self._mock_history(rows):
            result = get_prev_day_levels("AAPL")
        assert result["prev_day_high"]  == pytest.approx(188.0)
        assert result["prev_day_low"]   == pytest.approx(184.0)
        assert result["prev_day_close"] == pytest.approx(187.0)

    def test_range_pct_calculated(self):
        rows = [
            [183.0, 186.0, 182.5, 185.0, 1_000_000],
            [185.0, 190.0, 180.0, 185.0, 1_000_000],  # 10pt range on 185 close
        ]
        with self._mock_history(rows):
            result = get_prev_day_levels("AAPL")
        assert result["prev_day_range_pct"] == pytest.approx(5.41, abs=0.1)

    def test_returns_error_on_insufficient_history(self):
        rows = [[185.0, 188.0, 184.0, 187.0, 1_000_000]]
        with self._mock_history(rows):
            result = get_prev_day_levels("AAPL")
        assert "error" in result

    def test_returns_error_on_exception(self):
        with patch("yfinance.Ticker", side_effect=Exception("timeout")):
            result = get_prev_day_levels("AAPL")
        assert "error" in result


# ── get_ticker_fundamentals tests ─────────────────────────────────────────────

def _snapshot_dclient(high=188.0, low=184.0, close=187.0, ticker="AAPL") -> patch:
    prev_bar       = MagicMock()
    prev_bar.high  = high
    prev_bar.low   = low
    prev_bar.close = close
    snap           = MagicMock()
    snap.previous_daily_bar = prev_bar
    client         = MagicMock()
    client.get_stock_snapshot.return_value = {ticker: snap}
    return patch("core.alpaca._dclient", MagicMock(return_value=client))


class TestGetTickerFundamentals:
    def test_prev_day_levels_from_snapshot(self):
        with _snapshot_dclient(high=188.0, low=184.0, close=187.0):
            result = get_ticker_fundamentals("AAPL")
        assert result["prev_day_high"]  == pytest.approx(188.0)
        assert result["prev_day_low"]   == pytest.approx(184.0)
        assert result["prev_day_close"] == pytest.approx(187.0)

    def test_range_pct_calculated(self):
        # range = 10, close = 185 → 10/185*100
        with _snapshot_dclient(high=190.0, low=180.0, close=185.0):
            result = get_ticker_fundamentals("AAPL")
        assert result["prev_day_range_pct"] == pytest.approx(10 / 185 * 100, rel=1e-2)

    def test_float_fields_all_none(self):
        with _snapshot_dclient():
            result = get_ticker_fundamentals("AAPL")
        assert result["float_shares_m"]    is None
        assert result["short_pct_float"]   is None
        assert result["short_ratio_days"]  is None
        assert result["low_float"]         is None
        assert result["squeeze_potential"] is None

    def test_prev_day_error_when_ticker_not_in_snapshot(self):
        client = MagicMock()
        client.get_stock_snapshot.return_value = {}
        with patch("core.alpaca._dclient", MagicMock(return_value=client)):
            result = get_ticker_fundamentals("AAPL")
        assert "prev_day_error" in result

    def test_prev_day_error_when_no_previous_bar(self):
        snap = MagicMock()
        snap.previous_daily_bar = None
        client = MagicMock()
        client.get_stock_snapshot.return_value = {"AAPL": snap}
        with patch("core.alpaca._dclient", MagicMock(return_value=client)):
            result = get_ticker_fundamentals("AAPL")
        assert "prev_day_error" in result

    def test_prev_day_error_on_alpaca_exception(self):
        with patch("core.alpaca._dclient", MagicMock(side_effect=Exception("timeout"))):
            result = get_ticker_fundamentals("AAPL")
        assert "prev_day_error" in result


# ── get_ticker_market_data tests ──────────────────────────────────────────────

class TestGetTickerMarketData:
    def _make_daily_bar(self, close, high=None, low=None, volume=1_000_000):
        b = MagicMock()
        b.close  = close
        b.high   = high  if high  is not None else close * 1.01
        b.low    = low   if low   is not None else close * 0.99
        b.volume = volume
        return b

    def _make_minute_bar(self, close, open_=None, high=None, low=None, volume=50_000):
        b = MagicMock()
        b.close  = close
        b.open   = open_  if open_  is not None else close * 0.999
        b.high   = high   if high   is not None else close * 1.001
        b.low    = low    if low    is not None else close * 0.999
        b.volume = volume
        return b

    def _mock_client(self, daily_bars, pm_bars, intraday_bars):
        mock_resp_daily = MagicMock()
        mock_resp_daily.data = {"AAPL": daily_bars}

        mock_resp_pm = MagicMock()
        mock_resp_pm.data = {"AAPL": pm_bars}

        mock_resp_intraday = MagicMock()
        mock_resp_intraday.data = {"AAPL": intraday_bars, "SPY": []}

        mock_dc = MagicMock()
        mock_dc.get_stock_bars.side_effect = [
            mock_resp_daily, mock_resp_pm, mock_resp_intraday,
        ]
        return mock_dc

    def test_atr_pct_computed_from_daily_bars(self):
        bars = [self._make_daily_bar(100.0 + i, high=101.0 + i, low=99.0 + i)
                for i in range(20)]
        mock_dc = self._mock_client(bars, [], [])
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["atr_pct"] is not None
        assert result["atr_pct"] > 0

    def test_avg_daily_volume_computed(self):
        bars = [self._make_daily_bar(100.0, volume=1_000_000) for _ in range(22)]
        mock_dc = self._mock_client(bars, [], [])
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["avg_daily_volume"] is not None
        assert result["avg_daily_volume"] == 1_000_000

    def test_conviction_high_when_premarket_volume_large(self):
        bars  = [self._make_daily_bar(100.0, volume=1_000_000) for _ in range(22)]
        pm    = [self._make_minute_bar(100.5, volume=200_000) for _ in range(5)]
        mock_dc = self._mock_client(bars, pm, [])
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["conviction"] == "HIGH"

    def test_conviction_low_when_premarket_volume_small(self):
        bars  = [self._make_daily_bar(100.0, volume=1_000_000) for _ in range(22)]
        pm    = [self._make_minute_bar(100.5, volume=1_000) for _ in range(3)]  # 3k total = 0.3%
        mock_dc = self._mock_client(bars, pm, [])
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["conviction"] == "LOW"

    def test_premarket_returns_no_intraday_fields(self):
        mock_dc = self._mock_client([], [], [])
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["available"] is False
        assert result["reason"] == "pre-market"
        assert result["live_price"] is None

    def test_intraday_live_price_returned(self):
        bars  = [self._make_daily_bar(100.0, volume=1_000_000) for _ in range(22)]
        pm    = []
        intra = [
            self._make_minute_bar(185.0 + i * 0.1, open_=185.0, volume=100_000)
            for i in range(5)
        ]
        intra[0].open = 185.0
        mock_resp_daily = MagicMock()
        mock_resp_daily.data = {"AAPL": bars}
        mock_resp_pm = MagicMock()
        mock_resp_pm.data = {"AAPL": pm}
        mock_resp_intraday = MagicMock()
        mock_resp_intraday.data = {"AAPL": intra, "SPY": []}
        mock_dc = MagicMock()
        mock_dc.get_stock_bars.side_effect = [
            mock_resp_daily, mock_resp_pm, mock_resp_intraday,
        ]
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=False, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["live_price"] is not None
        assert result["available"] is True

    def test_above_vwap_correct(self):
        bars  = [self._make_daily_bar(100.0, volume=1_000_000) for _ in range(22)]
        intra = [
            self._make_minute_bar(200.0, open_=100.0, high=201.0, low=99.0, volume=100_000)
            for _ in range(5)
        ]
        mock_resp_daily = MagicMock()
        mock_resp_daily.data = {"AAPL": bars}
        mock_resp_pm = MagicMock()
        mock_resp_pm.data = {"AAPL": []}
        mock_resp_intraday = MagicMock()
        mock_resp_intraday.data = {"AAPL": intra, "SPY": []}
        mock_dc = MagicMock()
        mock_dc.get_stock_bars.side_effect = [
            mock_resp_daily, mock_resp_pm, mock_resp_intraday,
        ]
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=False, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["above_vwap"] is True

    def test_daily_bars_error_degrades_gracefully(self):
        mock_dc = MagicMock()
        mock_dc.get_stock_bars.side_effect = Exception("alpaca timeout")
        with patch("core.alpaca._dclient", return_value=mock_dc), \
             patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            result = get_ticker_market_data("AAPL")
        assert result["atr_pct"] is None
        assert "daily_bars_error" in result
