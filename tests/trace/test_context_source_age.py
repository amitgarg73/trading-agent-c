"""Source age (`as_of`) on the tools whose source carries a time of its own, and a hash on every `step:*` item. Offline: no broker, no network.

Promises: a tool stamps `as_of` only from a timestamp its source really gave (newest bar, previous daily bar, newest close_date, the
provider's quote time), as ISO 8601 UTC; a missing, naive, malformed or future time leaves it absent; a read that has no time of its own
(the risk agent's account reads, a live quote with only the fetch time) stays undated; the tool's return value is unchanged; and every
upstream-step item the logger wrote carries a content hash."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.tools import market_tools as mt
from agents.tools import research_tools as rt
from tests.trace import session_shapes_1519 as sh
from trace import context_manifest as cm

NOW = datetime.now(timezone.utc)
T1 = (NOW - timedelta(hours=3)).replace(microsecond=0)
T2 = (NOW - timedelta(minutes=2)).replace(microsecond=0)
Z = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv(cm.SWITCH_ENV, "1")
    cm._STAMPS.clear()


def bar(ts, close=100.0, vol=1000):
    b = MagicMock()
    b.timestamp, b.close, b.open, b.high, b.low, b.volume = ts, close, close, close * 1.01, close * 0.99, vol
    return b


def dclient(*per_call):
    """get_stock_bars answers each call in turn with {symbol: bars}."""
    client = MagicMock()
    client.get_stock_bars.side_effect = [MagicMock(data=d) for d in per_call]
    return MagicMock(return_value=client)


def manifest_for(tool, output, key):
    rec = cm.Recorder()
    rec.record_tool("research_X", tool, output, key=key)
    return cm.build("research_X", rec.take("research_X"), [], has_model=True)["items"][0]


# ── the shared helper ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
class TestNewestTime:
    def test_picks_the_newest_and_formats_utc(self):
        et = timezone(timedelta(hours=-4))
        assert cm.newest_time([T1, T2.astimezone(et), None]) == Z(T2)

    def test_iso_string_with_zone(self):
        assert cm.newest_time([T2.isoformat()]) == Z(T2)

    def test_naive_and_malformed_are_skipped(self):
        assert cm.newest_time([datetime(2026, 1, 1), "garbage", None, 12345, ""]) is None
        assert cm.newest_time([datetime(2026, 1, 1), T1]) == Z(T1)

    def test_newest_in_the_future_gives_none_not_the_older_one(self):
        assert cm.newest_time([T1, NOW + timedelta(days=1)]) is None

    def test_before_2000_is_refused(self):
        assert cm.newest_time([datetime(1999, 1, 1, tzinfo=timezone.utc)]) is None

    def test_a_bare_day_is_that_days_start_utc(self):
        assert cm.newest_time(["2026-01-05", date(2026, 1, 3)]) == "2026-01-05T00:00:00.000Z"

    def test_a_day_that_has_not_begun_is_refused(self):
        assert cm.newest_time([(NOW + timedelta(days=3)).date().isoformat()]) is None

    def test_empty_and_none(self):
        assert cm.newest_time([]) is None and cm.newest_time(None) is None

    def test_stamp_newest_is_a_noop_with_the_switch_off(self, monkeypatch):
        monkeypatch.delenv(cm.SWITCH_ENV)
        cm.stamp_newest("k:", [T1])
        assert cm.take_stamp("k:") is None


# ── get_ticker_market_data ────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def session_open():
    with patch("agents.tools.research_tools._is_premarket", return_value=False, create=True):
        yield


class TestMarketData:
    def test_newest_minute_bar_time_is_as_of(self, session_open):
        daily = [bar(NOW - timedelta(days=d)) for d in range(25, 0, -1)]
        minute = [bar(T1), bar(T2)]
        with patch("core.alpaca._dclient", dclient({"AAPL": daily}, {"AAPL": []}, {"AAPL": minute, "SPY": minute})):
            out = rt.get_ticker_market_data("AAPL")
        assert out["live_price"] is not None
        assert cm.take_stamp("get_ticker_market_data:AAPL") == {"as_of": Z(T2)}

    def test_return_value_is_unchanged_by_the_switch(self, session_open, monkeypatch):
        def run():
            daily = [bar(T1, close=50 + i) for i in range(25)]
            with patch("core.alpaca._dclient", dclient({"AAPL": daily}, {"AAPL": []}, {"AAPL": [bar(T2)], "SPY": [bar(T2)]})):
                return rt.get_ticker_market_data("AAPL")
        a = run()
        monkeypatch.delenv(cm.SWITCH_ENV)
        assert run() == a

    def test_bars_without_timestamps_stay_undated(self, session_open):
        no_ts = [bar(None) for _ in range(25)]
        with patch("core.alpaca._dclient", dclient({"AAPL": no_ts}, {"AAPL": []}, {"AAPL": [bar(None)], "SPY": []})):
            rt.get_ticker_market_data("AAPL")
        assert cm.take_stamp("get_ticker_market_data:AAPL") is None

    def test_naive_timestamp_is_not_stamped(self, session_open):
        naive = datetime.now()
        with patch("core.alpaca._dclient", dclient({"AAPL": [bar(naive)] * 25}, {"AAPL": []}, {"AAPL": [bar(naive)], "SPY": []})):
            rt.get_ticker_market_data("AAPL")
        assert cm.take_stamp("get_ticker_market_data:AAPL") is None

    def test_future_bar_is_not_stamped(self, session_open):
        fut = NOW + timedelta(hours=2)
        with patch("core.alpaca._dclient", dclient({"AAPL": [bar(T1)] * 25}, {"AAPL": []}, {"AAPL": [bar(fut)], "SPY": []})):
            rt.get_ticker_market_data("AAPL")
        assert cm.take_stamp("get_ticker_market_data:AAPL") is None

    def test_quote_only_fallback_has_no_time_and_is_not_stamped(self):
        """Daily bars failed and minute bars blocked: only a live quote answers, whose only time would be the fetch. Stay undated."""
        client = MagicMock()
        client.get_stock_bars.side_effect = Exception("blocked")
        client.get_stock_latest_quote.return_value = {"AAPL": MagicMock(ask_price=10, bid_price=10)}
        with patch("core.alpaca._dclient", MagicMock(return_value=client)), \
                patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            out = rt.get_ticker_market_data("AAPL")
        assert out["daily_bars_error"]
        assert cm.take_stamp("get_ticker_market_data:AAPL") is None

    def test_premarket_uses_the_premarket_minute_bar_time(self):
        daily = [bar(NOW - timedelta(days=d)) for d in range(25, 0, -1)]
        with patch("core.alpaca._dclient", dclient({"AAPL": daily}, {"AAPL": [bar(T1), bar(T2)]})), \
                patch("agents.tools.research_tools._is_premarket", return_value=True, create=True):
            rt.get_ticker_market_data("AAPL")
        assert cm.take_stamp("get_ticker_market_data:AAPL") == {"as_of": Z(T2)}


# ── get_ticker_fundamentals ───────────────────────────────────────────────────────────────────────────────────────────────────────
def snapshot(prev):
    return MagicMock(return_value=MagicMock(get_stock_snapshot=MagicMock(return_value={"AAPL": MagicMock(previous_daily_bar=prev)})))


class TestFundamentals:
    def test_previous_daily_bar_time_is_as_of(self):
        with patch("core.alpaca._dclient", snapshot(bar(T1, close=10))):
            out = rt.get_ticker_fundamentals("AAPL")
        assert out["prev_day_close"] == 10.0
        assert cm.take_stamp("get_ticker_fundamentals:AAPL") == {"as_of": Z(T1)}

    def test_no_snapshot_is_not_stamped(self):
        with patch("core.alpaca._dclient", snapshot(None)):
            assert rt.get_ticker_fundamentals("AAPL")["prev_day_error"] == "no snapshot data"
        assert cm.take_stamp("get_ticker_fundamentals:AAPL") is None

    def test_missing_timestamp_is_not_stamped(self):
        with patch("core.alpaca._dclient", snapshot(bar(None))):
            rt.get_ticker_fundamentals("AAPL")
        assert cm.take_stamp("get_ticker_fundamentals:AAPL") is None


# ── get_position_history ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def positions(rows):
    q = MagicMock()
    for m in ("table", "select", "eq", "gte"):
        getattr(q, m).return_value = q
    q.execute.return_value = MagicMock(data=rows)
    return MagicMock(return_value=q)


class TestPositionHistory:
    def test_newest_close_date_is_as_of_at_day_grain(self):
        rows = [{"realized_pnl": 5, "exit_reason": "tp", "close_date": "2026-01-02"},
                {"realized_pnl": -1, "exit_reason": "sl", "close_date": "2026-01-09"}]
        with patch("core.db.get_client", positions(rows)):
            out = rt.get_position_history("AAPL")
        assert out["trades"] == 2 and set(out) == {"trades", "wins", "win_rate_pct", "avg_pnl", "last_exit"}
        assert cm.take_stamp("get_position_history:AAPL") == {"as_of": "2026-01-09T00:00:00.000Z"}

    def test_no_rows_is_not_stamped(self):
        with patch("core.db.get_client", positions([])):
            assert rt.get_position_history("AAPL")["trades"] == 0
        assert cm.take_stamp("get_position_history:AAPL") is None

    def test_malformed_or_missing_dates_are_not_stamped(self):
        with patch("core.db.get_client", positions([{"close_date": None}, {"close_date": "not a date"}, {}])):
            rt.get_position_history("AAPL")
        assert cm.take_stamp("get_position_history:AAPL") is None

    def test_future_close_date_is_not_stamped(self):
        far = (date.today() + timedelta(days=30)).isoformat()
        with patch("core.db.get_client", positions([{"close_date": far}])):
            rt.get_position_history("AAPL")
        assert cm.take_stamp("get_position_history:AAPL") is None

    def test_timezoned_close_date_is_converted_to_utc(self):
        with patch("core.db.get_client", positions([{"close_date": "2026-01-09T20:00:00-05:00"}])):
            rt.get_position_history("AAPL")
        assert cm.take_stamp("get_position_history:AAPL") == {"as_of": "2026-01-10T01:00:00.000Z"}


# ── market reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def hist(*closes, tz="America/New_York", end=None):
    end = end or T2
    idx = pd.DatetimeIndex([end - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))]).tz_convert(tz) if tz else \
        pd.DatetimeIndex([(end - timedelta(days=len(closes) - 1 - i)).replace(tzinfo=None) for i in range(len(closes))])
    return pd.DataFrame({"Close": list(closes)}, index=idx)


def yf_returning(h):
    return patch("agents.tools.market_tools.yf.Ticker", return_value=MagicMock(history=MagicMock(return_value=h)))


class TestMarketReads:
    def test_vix_provider_time(self):
        with yf_returning(hist(17.0)):
            assert mt.get_vix()["value"] == 17.0
        assert cm.take_stamp("get_vix:") == {"as_of": Z(T2)}

    def test_vix_naive_index_is_not_stamped(self):
        with yf_returning(hist(17.0, tz=None)):
            mt.get_vix()
        assert cm.take_stamp("get_vix:") is None

    def test_vix_error_is_not_stamped(self):
        with yf_returning(pd.DataFrame({"Close": []})):
            assert "error" in mt.get_vix()
        assert cm.take_stamp("get_vix:") is None

    def test_futures_newest_of_three(self):
        older = hist(100.0, 101.0, end=T1)
        newer = hist(100.0, 101.0, end=T2)
        seq = iter([older, newer, older])
        with patch("agents.tools.market_tools.yf.Ticker", side_effect=lambda s: MagicMock(history=MagicMock(return_value=next(seq)))):
            mt.get_futures()
        assert cm.take_stamp("get_futures:") == {"as_of": Z(T2)}

    def test_treasury_yields(self):
        with yf_returning(hist(4.0, 4.1, tz="UTC")):
            assert mt.get_treasury_yields()["yield_10y"] == 4.1
        assert cm.take_stamp("get_treasury_yields:") == {"as_of": Z(T2)}

    def test_sector_rotation_uses_newest_bar(self):
        bars = {e: [bar(T1, 10), bar(T2, 11)] for e in mt._SECTOR_ETFS}
        with patch("core.alpaca._dclient", dclient(bars)):
            rows = mt.get_sector_rotation()
        assert len(rows) == 11
        assert cm.take_stamp("get_sector_rotation:") == {"as_of": Z(T2)}

    def test_fear_greed_uses_the_indexs_own_unix_timestamp(self):
        payload = json.dumps({"data": [{"value": "40", "value_classification": "Fear", "timestamp": str(int(T1.timestamp()))}]}).encode()
        resp = MagicMock(read=MagicMock(return_value=payload))
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            out = mt.get_fear_greed()
        assert out == {"value": 40, "classification": "Fear"}
        assert cm.take_stamp("get_fear_greed:") == {"as_of": Z(T1)}

    def test_fear_greed_without_timestamp_is_not_stamped(self):
        payload = json.dumps({"data": [{"value": "40", "value_classification": "Fear"}]}).encode()
        resp = MagicMock(read=MagicMock(return_value=payload))
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            assert mt.get_fear_greed()["value"] == 40
        assert cm.take_stamp("get_fear_greed:") is None

    def test_economic_calendar_stays_undated(self):
        ev = [{"country": "USD", "date": date.today().isoformat() + "T08:30:00-0400", "title": "CPI", "impact": "High"}]
        resp = MagicMock(read=MagicMock(return_value=json.dumps(ev).encode()))
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            assert mt.get_economic_calendar()["high_impact_count"] == 1
        assert cm.take_stamp("get_economic_calendar:") is None


# ── undated stays undated, errors never inherit an old time ───────────────────────────────────────────────────────────────────────
class TestUndated:
    @pytest.mark.parametrize("tool", ["get_buying_power", "get_open_positions", "get_portfolio_exposure", "get_today_pnl"])
    def test_risk_account_reads_never_carry_as_of(self, tool):
        cm.stamp_source(f"{tool}:", as_of=T1)     # even a stray parked time would be taken, so prove the tools never park one
        cm._STAMPS.clear()
        from agents.tools import risk_tools
        with patch("core.db.get_client", positions([])), patch("core.alpaca._tclient", MagicMock(), create=True):
            getattr(risk_tools, tool)()
        assert cm.take_stamp(f"{tool}:") is None
        item = manifest_for(tool, {"x": 1}, f"{tool}:")
        assert "as_of" not in item and item["hash"].startswith("sha256:")

    def test_an_errored_read_does_not_inherit_an_older_parked_time(self):
        cm.stamp_source("get_vix:", as_of=T1)
        item = manifest_for("get_vix", {"error": "no VIX data"}, "get_vix:")
        assert "as_of" not in item

    def test_circuit_breaker_manifest_carries_the_provider_times(self):
        cm.stamp_source("get_vix:", as_of=T1)
        m = cm.circuit_breaker_context({"value": 40}, {"avg_change_pct": -3})
        assert m["items"][0]["as_of"] == Z(T1) and "as_of" not in m["items"][1]


# ── every step item carries a hash ────────────────────────────────────────────────────────────────────────────────────────────────
class TestStepHash:
    def test_every_step_item_in_a_whole_session_has_a_hash(self, tracer, mock_argus_exporter):
        sh.run_session(tracer)
        seen = 0
        for s in mock_argus_exporter.spans:
            raw = dict(s.attributes or {}).get(cm.ATTRIBUTE)
            if raw is None:
                continue
            for item in json.loads(raw)["items"]:
                if item["source"].startswith("step:") and item["source"] != "step:unknown":
                    seen += 1
                    assert item["hash"].startswith("sha256:") and len(item["hash"]) == 71
        assert seen >= 3

    def test_hash_is_of_the_content_and_the_content_is_never_in_the_manifest(self, tracer, mock_argus_exporter):
        secret = "SENTINEL-STEP-TEXT-QZX"
        up = tracer.log_agent_message("market", secret, "completed", model=sh.MODEL)
        tracer.log_agent_message("scanner", "other", "completed", model=sh.MODEL, inputs=[up])
        span = [s for s in mock_argus_exporter.spans if dict(s.attributes or {}).get("argus.agent") == "scanner"][-1]
        raw = dict(span.attributes)[cm.ATTRIBUTE]
        item = json.loads(raw)["items"][0]
        assert item["hash"] == cm.content_hash({"agent_reasoning": secret, "outcome": "completed"})
        assert secret not in raw

    def test_different_output_gives_a_different_hash(self, tracer, mock_argus_exporter):
        a = tracer.log_agent_message("market", "one", "completed", model=sh.MODEL)
        b = tracer.log_agent_message("market", "two", "completed", model=sh.MODEL)
        tracer.log_agent_message("scanner", "x", "completed", model=sh.MODEL, inputs=[a, b])
        span = [s for s in mock_argus_exporter.spans if dict(s.attributes or {}).get("argus.agent") == "scanner"][-1]
        items = json.loads(dict(span.attributes)[cm.ATTRIBUTE])["items"]
        assert items[0]["hash"] != items[1]["hash"]

    def test_a_step_the_logger_never_wrote_has_nothing_to_hash(self):
        m = cm.build("risk", [], [{"agent": None, "span_id": "f" * 16}], has_model=True)
        assert m["items"][0] == {"kind": "input", "source": "step:unknown", "used": True, "id": "f" * 16}


def test_stamped_manifest_passes_provys_own_normaliser(tmp_path):
    import os, shutil, subprocess
    src = "/Users/amitgarg/Claude Projects/argus/web/lib/context-capture.ts"
    node = shutil.which("node")
    if not node or not os.path.exists(src):
        pytest.skip("node or the argus checkout is not on this machine")
    shutil.copy(src, tmp_path / "context-capture.ts")
    cm.stamp_newest("get_ticker_market_data:G", [T2])
    r = cm.Recorder()
    r.record_tool("research_G", "get_ticker_market_data", {"a": 1}, key="get_ticker_market_data:G")
    r.record_tool("research_G", "get_position_history", {"trades": 0}, key="get_position_history:G")
    r.remember_span("a" * 16, "market", cm.content_hash({"agent_reasoning": "x"}))
    m = cm.build("research_G", r.take("research_G"), r.upstream(["a" * 16]), has_model=True)
    script = tmp_path / "n.ts"
    script.write_text("import { normalizeContext } from './context-capture.ts';\n"
                      f"console.log(JSON.stringify(normalizeContext({json.dumps(m)}, {{ capturedBy: 'otlp:provy' }})));\n")
    out = subprocess.run([node, "--experimental-strip-types", str(script)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    if out.returncode != 0 and "strip-types" in out.stderr:
        pytest.skip("this node cannot run TypeScript directly")
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["notes"] == [], res["notes"]
    got = res["context"]["items"]
    assert got[0]["hash"] == m["items"][0]["hash"] and got[1]["as_of"] == Z(T2) and "as_of" not in got[2]
