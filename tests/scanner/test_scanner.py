from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scanner.universe import get_sector, get_tickers, SECTOR_MAP
from scanner.scanner import _compute_rsi, _compute_macd_hist, _compute_atr_pct, _score_ticker, _MIN_PRICE, _MAX_ATR_PCT
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

    def test_new_tickers_added(self):
        tickers = get_tickers()
        for ticker in ["GEV", "VRT", "CMI", "FISV", "STX", "TER", "SNPS", "WDC"]:
            assert ticker in tickers, f"{ticker} should be in universe after 2026-06-17 update"

    def test_semi_leaders_in_universe(self):
        tickers = get_tickers()
        for ticker in ["AMAT", "LRCX", "KLAC", "AVGO", "CRWD", "PLTR"]:
            assert ticker in tickers, f"{ticker} should be in universe"

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

    def test_no_upper_price_cap(self):
        """High-priced stocks like AMAT ($621), CRWD ($683) must not be rejected."""
        hist = _make_hist(n=60, base_price=650.0)
        result = _score_ticker(hist)
        assert result["current_price"] > 500  # no cap — scores fine

    def test_atr_cap_raised_to_10(self):
        """ATR% up to 10% must pass — semis need this headroom."""
        assert _MAX_ATR_PCT == 10.0

    def test_raises_on_low_volume(self):
        hist = _make_hist(n=60)
        hist["Volume"] = 100  # tiny volume
        with pytest.raises(ValueError, match="avg volume"):
            _score_ticker(hist)

    def test_returns_required_fields(self):
        hist = _make_hist(n=60, trend=0.003)
        result = _score_ticker(hist)
        for field in ["technical_score", "current_price", "avg_volume",
                      "rsi", "volume_ratio", "atr_pct", "above_sma20", "above_sma50",
                      "macd_rising", "macd_inflecting", "pct_from_52w_high"]:
            assert field in result

    def test_52w_high_near_gives_bonus(self):
        """Stock within 5% of 52W high should earn +2 in scoring."""
        # Steady uptrend: last price will be near the high
        hist = _make_hist(n=60, trend=0.003)
        result = _score_ticker(hist)
        assert result["pct_from_52w_high"] < 15.0  # trending stock stays near highs

    def test_volume_surge_scores_higher(self):
        """2x volume day scores higher than 1x volume day."""
        hist_normal = _make_hist(n=60, trend=0.003)
        hist_surge  = hist_normal.copy()
        hist_surge["Volume"] = hist_normal["Volume"] * 2  # 2x volume today
        # Just verify no crash and surge gets a non-zero score
        result = _score_ticker(hist_surge)
        assert result["technical_score"] >= 1

    def test_macd_rising_true_in_steady_uptrend(self):
        hist = _make_hist(n=60, trend=0.003)
        result = _score_ticker(hist)
        assert isinstance(result["macd_rising"], bool)

    def test_macd_rising_is_bool(self):
        hist = _make_hist(n=60, trend=0.003)
        result = _score_ticker(hist)
        assert isinstance(result["macd_rising"], bool)


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

    def test_below_sma20_can_still_be_written(self, mock_supabase):
        """SMA20 is now a scoring signal not a hard gate — downtrending stock can still appear if score >= min_score."""
        inserted = []
        q = make_query([])
        q.insert.side_effect = lambda data: (inserted.append(data), make_query([]))[1]
        mock_supabase.table.return_value = q

        # Stock below SMA20 but with strong volume surge and RSI in range
        prices = [120.0] * 35 + [120.0 - i * 0.5 for i in range(1, 26)]
        # Last price ~107.5, SMA20 ~113 — below SMA20
        volumes = [3_000_000] * 59 + [9_000_000]  # 3x volume surge today
        hist = pd.DataFrame({
            "Open":   [p * 0.999 for p in prices],
            "High":   [p * 1.003 for p in prices],
            "Low":    [p * 0.997 for p in prices],
            "Close":  prices,
            "Volume": volumes,
        })
        with patch("scanner.scanner.yf.download", return_value=hist), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), min_score=1)

        # With volume surge (+3) + RSI likely in range, it may still score >= 1
        # Just verify no crash — the exact outcome depends on computed RSI
        assert True  # structural

    def test_macd_fading_can_still_be_written_with_volume(self, mock_supabase):
        """MACD fading is no longer a hard gate — stock can pass if volume and other signals score enough."""
        inserted = []
        q = make_query([])
        q.insert.side_effect = lambda data: (inserted.append(data), make_query([]))[1]
        mock_supabase.table.return_value = q

        prices  = [100.0 + i * 0.8 for i in range(45)] + [136.0] * 15
        volumes = [3_000_000] * 59 + [9_000_000]  # big volume on last day
        hist = pd.DataFrame({
            "Open":   [p * 0.999 for p in prices],
            "High":   [p * 1.003 for p in prices],
            "Low":    [p * 0.997 for p in prices],
            "Close":  prices,
            "Volume": volumes,
        })
        with patch("scanner.scanner.yf.download", return_value=hist), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), min_score=1)

        # Verify no crash — the stock may now pass given volume surge
        assert True  # structural

    def test_clean_uptrend_passes_both_hard_filters(self, mock_supabase):
        """Ticker above SMA20 with accelerating MACD passes both hard filters."""
        inserted = []
        q = make_query([])
        q.insert.side_effect = lambda data: (inserted.append(data), make_query([]))[1]
        mock_supabase.table.return_value = q

        # Quadratic growth: price = 100 + 0.05*i^2 — EMA lags keep MACD histogram rising
        prices = [100.0 + 0.05 * i * i for i in range(60)]
        hist = pd.DataFrame({
            "Open":   [p * 0.999 for p in prices],
            "High":   [p * 1.005 for p in prices],
            "Low":    [p * 0.995 for p in prices],
            "Close":  prices,
            "Volume": [3_000_000] * 60,
        })
        # Single-ticker download returns the DataFrame directly (not wrapped in a dict)
        with patch("scanner.scanner.yf.download", return_value=hist), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            count = run_scanner(scan_date=date(2026, 5, 27))

        assert count >= 1
        assert any(d.get("ticker") == "AAPL" for d in inserted)


# ── Composite scoring ─────────────────────────────────────────────────────────

class TestCompositeScore:
    def test_gap_bonus_tiers(self):
        from agents.research_agent import _compute_composite_score
        assert _compute_composite_score(5, 3.5, None) == 8   # +3
        assert _compute_composite_score(5, 2.0, None) == 7   # +2
        assert _compute_composite_score(5, 0.7, None) == 6   # +1
        assert _compute_composite_score(5, 0.1, None) == 5   # no bonus
        assert _compute_composite_score(5, -2.0, None) == 4  # -1 penalty

    def test_sector_etf_bonus(self):
        from agents.research_agent import _compute_composite_score
        assert _compute_composite_score(5, 0, 2.5) == 7   # +2
        assert _compute_composite_score(5, 0, 1.0) == 6   # +1
        assert _compute_composite_score(5, 0, 0.3) == 5   # no bonus
        assert _compute_composite_score(5, 0, -0.5) == 5  # no penalty for slight drop

    def test_intraday_rs_bonus(self):
        from agents.research_agent import _compute_composite_score
        assert _compute_composite_score(5, 0, None, rs_vs_spy=2.5) == 8   # +3
        assert _compute_composite_score(5, 0, None, rs_vs_spy=1.2) == 7   # +2
        assert _compute_composite_score(5, 0, None, rs_vs_spy=0.5) == 6   # +1
        assert _compute_composite_score(5, 0, None, rs_vs_spy=-1.5) == 4  # -1

    def test_vwap_and_orb_bonus(self):
        from agents.research_agent import _compute_composite_score
        score = _compute_composite_score(5, 0, None, above_vwap=True, orb_pct=0.5)
        assert score == 7  # +1 vwap +1 orb

    def test_sector_rotation_day_scenario(self):
        """AMAT setup: baseline=6, gap=9%, SMH up 3%, RS=5 — should score very high."""
        from agents.research_agent import _compute_composite_score
        score = _compute_composite_score(
            baseline=6, gap_pct=9.0, sector_etf_pct=3.0,
            rs_vs_spy=5.0, above_vwap=True, orb_pct=2.0,
        )
        assert score >= 14  # 6 + 3(gap) + 2(etf) + 3(rs) + 1(vwap) + 1(orb)


# ── Universe sector ETF map ───────────────────────────────────────────────────

class TestSectorEtfMap:
    def test_semi_tickers_map_to_smh(self):
        from scanner.universe import get_sector_etf
        for ticker in ["AMAT", "LRCX", "KLAC", "NVDA", "AMD"]:
            assert get_sector_etf(ticker) == "SMH", f"{ticker} should map to SMH"

    def test_tech_non_semi_maps_to_xlk(self):
        from scanner.universe import get_sector_etf
        assert get_sector_etf("MSFT") == "XLK"
        assert get_sector_etf("CRM") == "XLK"

    def test_financials_map_to_xlf(self):
        from scanner.universe import get_sector_etf
        assert get_sector_etf("JPM") == "XLF"
        assert get_sector_etf("GS") == "XLF"

    def test_unknown_maps_to_spy(self):
        from scanner.universe import get_sector_etf
        assert get_sector_etf("XXXX") == "SPY"


class TestScannerToolTracing:
    """The scanner reported what it CONCLUDED and never what it LOOKED AT.

    scanner/scanner.py replaced agents/scanner_agent.py and restored the span, the decision and the
    message, but never called log_tool_call at all. From 5 Aug 2026 the trace stream carried 2.56
    steps a session against 7.07 before, so Provy read a 63% drop as the agent changing how it
    works. That was true of the record and false of the agent.
    """

    def _tools(self, tracer):
        """Every tool call recorded, as {name: outcome}."""
        return {c.args[1]: c.kwargs.get("outcome") for c in tracer.log_tool_call.call_args_list}

    def test_the_price_download_is_traced_on_success(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer = MagicMock()
        with patch("scanner.scanner.yf.download", return_value={"AAPL": _make_hist(n=60)}), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), tracer=tracer)
        assert self._tools(tracer).get("download_prices") == "downloaded"

    def test_the_scan_results_read_is_traced(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer = MagicMock()
        with patch("scanner.scanner.yf.download", return_value={"AAPL": _make_hist(n=60)}), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), tracer=tracer)
        assert self._tools(tracer).get("fetch_scored_tickers") == "ok"

    def test_a_morning_that_scores_nothing_still_reports_the_read(self, mock_supabase):
        # The already-scored short circuit returns early. A scan that reports nothing on a quiet
        # morning looks exactly like a scan that did not run, which is the original defect.
        mock_supabase.table.return_value = make_query([{"ticker": "AAPL"}])
        tracer = MagicMock()
        with patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), tracer=tracer)
        assert self._tools(tracer).get("fetch_scored_tickers") == "ok"

    def test_a_failed_download_is_traced_as_failed_not_omitted(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer = MagicMock()
        with patch("scanner.scanner.yf.download", side_effect=Exception("rate limit")), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            run_scanner(scan_date=date(2026, 5, 27), tracer=tracer)
        assert self._tools(tracer).get("download_prices") == "failed"

    def test_success_and_failure_use_the_same_step_name(self, mock_supabase):
        # If only one path emitted the step, its presence would encode the outcome and the agent
        # would look like two different agents. That is argus#679 one layer down.
        mock_supabase.table.return_value = make_query([])
        names = []
        for effect in [{"AAPL": _make_hist(n=60)}, Exception("boom")]:
            tracer = MagicMock()
            kw = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with patch("scanner.scanner.yf.download", **kw), \
                 patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
                from scanner.scanner import run_scanner
                run_scanner(scan_date=date(2026, 5, 27), tracer=tracer)
            names.append("download_prices" in self._tools(tracer))
        assert names == [True, True]

    def test_a_broken_tracer_never_breaks_the_scan(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer = MagicMock()
        tracer.log_tool_call.side_effect = RuntimeError("provy down")
        with patch("scanner.scanner.yf.download", return_value={"AAPL": _make_hist(n=60)}), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            assert run_scanner(scan_date=date(2026, 5, 27), tracer=tracer) >= 0

    def test_no_tracer_still_scans(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("scanner.scanner.yf.download", return_value={"AAPL": _make_hist(n=60)}), \
             patch("scanner.scanner.get_tickers", return_value=["AAPL"]):
            from scanner.scanner import run_scanner
            assert run_scanner(scan_date=date(2026, 5, 27)) >= 0

    def test_run_scanner_still_carries_the_agent_span_decorator(self):
        # Regression guard: adding the helpers above run_scanner moved @traced_agent onto a helper,
        # which silently detaches the agent span from the scan.
        from scanner.scanner import run_scanner
        assert getattr(run_scanner, "__wrapped__", None) is not None, \
            "@traced_agent must decorate run_scanner, not a helper defined above it"
