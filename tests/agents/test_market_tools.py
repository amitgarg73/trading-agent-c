from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.tools.market_tools import (
    _SECTOR_ETFS,
    _vix_level,
    get_economic_calendar,
    get_fear_greed,
    get_futures,
    get_sector_rotation,
    get_treasury_yields,
    get_vix,
)


def _make_alpaca_bar(close, prev_close=None):
    b = MagicMock()
    b.close  = close
    b.high   = close * 1.005
    b.low    = close * 0.995
    b.open   = close * 0.998
    b.volume = 1_000_000
    return b


def _alpaca_dclient(bars_by_symbol: dict):
    """Return a patched _dclient whose get_stock_bars().data == bars_by_symbol."""
    resp = MagicMock()
    resp.data = bars_by_symbol
    client = MagicMock()
    client.get_stock_bars.return_value = resp
    return MagicMock(return_value=client)


def _make_hist(*closes, open_val=None):
    """Build a minimal DataFrame that looks like yfinance .history() output."""
    df = pd.DataFrame({
        "Close": list(closes),
        "Open":  [open_val or closes[0]] * len(closes),
        "High":  [c * 1.01 for c in closes],
        "Low":   [c * 0.99 for c in closes],
        "Volume": [1_000_000] * len(closes),
    })
    return df


# ── _vix_level ─────────────────────────────────────────────────────────────────

class TestVixLevel:
    def test_low_below_15(self):        assert _vix_level(12.0)  == "LOW"
    def test_elevated_15_to_20(self):   assert _vix_level(17.5)  == "ELEVATED"
    def test_high_20_to_25(self):       assert _vix_level(22.0)  == "HIGH"
    def test_crisis_25_to_30(self):     assert _vix_level(27.0)  == "CRISIS"
    def test_extreme_above_30(self):    assert _vix_level(35.0)  == "EXTREME"
    def test_boundary_at_15(self):      assert _vix_level(15.0)  == "ELEVATED"
    def test_boundary_at_20(self):      assert _vix_level(20.0)  == "HIGH"


# ── get_vix ────────────────────────────────────────────────────────────────────

class TestGetVix:
    def test_returns_value_and_level(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_hist(18.5)
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_vix()
        assert result["value"] == 18.5
        assert result["level"] == "ELEVATED"

    def test_returns_error_on_empty_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_vix()
        assert "error" in result

    def test_returns_error_on_exception(self):
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=Exception("network")):
            result = get_vix()
        assert "error" in result


# ── get_futures ────────────────────────────────────────────────────────────────

class TestGetFutures:
    def _mock_ticker_factory(self, closes_map: dict):
        """Return a factory that yields different DataFrames by symbol."""
        def factory(symbol):
            mock = MagicMock()
            mock.history.return_value = _make_hist(*closes_map.get(symbol, [100, 100]))
            return mock
        return factory

    def test_bullish_when_avg_change_positive(self):
        closes = {"ES=F": [4000, 4040], "NQ=F": [14000, 14140], "YM=F": [33000, 33330]}
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=self._mock_ticker_factory(closes)):
            result = get_futures()
        assert result["bias"] == "BULLISH"
        assert result["S&P500"]["change_pct"] == pytest.approx(1.0, rel=1e-2)

    def test_bearish_when_avg_change_negative(self):
        closes = {"ES=F": [4000, 3960], "NQ=F": [14000, 13860], "YM=F": [33000, 32670]}
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=self._mock_ticker_factory(closes)):
            result = get_futures()
        assert result["bias"] == "BEARISH"

    def test_neutral_when_change_near_zero(self):
        closes = {"ES=F": [4000, 4001], "NQ=F": [14000, 14001], "YM=F": [33000, 33001]}
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=self._mock_ticker_factory(closes)):
            result = get_futures()
        assert result["bias"] == "NEUTRAL"

    def test_all_three_indices_present(self):
        closes = {"ES=F": [100, 101], "NQ=F": [100, 101], "YM=F": [100, 101]}
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=self._mock_ticker_factory(closes)):
            result = get_futures()
        assert "S&P500" in result and "Nasdaq" in result and "Dow" in result

    def test_returns_error_on_exception(self):
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=Exception("timeout")):
            result = get_futures()
        assert "error" in result


# ── get_fear_greed ─────────────────────────────────────────────────────────────

class TestGetFearGreed:
    def test_returns_value_and_classification(self):
        payload = b'{"data":[{"value":"72","value_classification":"Greed"}]}'
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = get_fear_greed()
        assert result["value"] == 72
        assert result["classification"] == "Greed"

    def test_returns_error_on_exception(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = get_fear_greed()
        assert "error" in result


# ── get_sector_rotation ────────────────────────────────────────────────────────

class TestGetSectorRotation:
    def _uniform_bars(self, prev=100.0, curr=101.0):
        return {etf: [_make_alpaca_bar(prev), _make_alpaca_bar(curr)] for etf in _SECTOR_ETFS}

    def test_returns_list_with_all_etfs(self):
        with patch("core.alpaca._dclient", _alpaca_dclient(self._uniform_bars())):
            result = get_sector_rotation()
        assert len(result) == 11
        assert all("etf" in r and "change_pct" in r for r in result)

    def test_sorted_best_to_worst(self):
        changes = [1.0, -1.0, 0.5, -0.5, 2.0, -2.0, 0.1, -0.1, 1.5, -1.5, 0.0]
        bars_map = {
            etf: [_make_alpaca_bar(100.0), _make_alpaca_bar(100.0 * (1 + changes[i] / 100))]
            for i, etf in enumerate(_SECTOR_ETFS)
        }
        with patch("core.alpaca._dclient", _alpaca_dclient(bars_map)):
            result = get_sector_rotation()
        pcts = [r["change_pct"] for r in result]
        assert pcts == sorted(pcts, reverse=True)

    def test_zero_change_when_only_one_bar(self):
        bars_map = {etf: [_make_alpaca_bar(100.0)] for etf in _SECTOR_ETFS}
        with patch("core.alpaca._dclient", _alpaca_dclient(bars_map)):
            result = get_sector_rotation()
        assert all(r["change_pct"] == 0.0 for r in result)

    def test_returns_error_list_on_exception(self):
        mock_dc = MagicMock(side_effect=Exception("network"))
        with patch("core.alpaca._dclient", mock_dc):
            result = get_sector_rotation()
        assert "error" in result[0]


# ── get_economic_calendar ──────────────────────────────────────────────────────

class TestGetEconomicCalendar:
    def _mock_urlopen(self, events: list):
        import json
        payload = json.dumps(events).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def _today(self):
        from datetime import date
        return date.today().isoformat()

    def test_high_impact_event_detected(self):
        events = [
            {"country": "USD", "date": f"{self._today()}T08:30:00-0400",
             "title": "CPI m/m", "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
        ]
        with self._mock_urlopen(events):
            result = get_economic_calendar()
        assert result["has_high_impact_events"] is True
        assert result["high_impact_count"] == 1
        assert any("CPI" in e for e in result["high_impact_events"])

    def test_non_usd_events_filtered_out(self):
        events = [
            {"country": "EUR", "date": f"{self._today()}T10:00:00-0400",
             "title": "ECB Rate", "impact": "High", "forecast": None, "previous": None},
        ]
        with self._mock_urlopen(events):
            result = get_economic_calendar()
        assert result["has_high_impact_events"] is False
        assert result["high_impact_count"] == 0

    def test_medium_impact_not_in_high_list(self):
        events = [
            {"country": "USD", "date": f"{self._today()}T10:00:00-0400",
             "title": "Housing Starts", "impact": "Medium", "forecast": None, "previous": None},
        ]
        with self._mock_urlopen(events):
            result = get_economic_calendar()
        assert result["has_high_impact_events"] is False
        assert len(result["all_events_today"]) == 1

    def test_returns_error_on_exception(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = get_economic_calendar()
        assert "error" in result


# ── get_treasury_yields ────────────────────────────────────────────────────────

class TestGetTreasuryYields:
    def test_returns_yield_and_direction_rising(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_hist(4.40, 4.52)
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_treasury_yields()
        assert result["yield_10y"] == pytest.approx(4.52)
        assert result["change_bp"] == pytest.approx(12.0, abs=0.2)
        assert result["direction"] == "rising"

    def test_returns_direction_falling(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_hist(4.60, 4.45)
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_treasury_yields()
        assert result["direction"] == "falling"
        assert result["change_bp"] < 0

    def test_flat_within_5bp(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_hist(4.50, 4.53)
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_treasury_yields()
        assert result["direction"] == "flat"

    def test_returns_error_on_insufficient_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_hist(4.50)
        with patch("agents.tools.market_tools.yf.Ticker", return_value=mock_ticker):
            result = get_treasury_yields()
        assert "error" in result

    def test_returns_error_on_exception(self):
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=Exception("network")):
            result = get_treasury_yields()
        assert "error" in result
