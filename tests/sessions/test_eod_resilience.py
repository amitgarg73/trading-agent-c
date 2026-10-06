"""
argus#1566 (EOD takes an explicit date, dry-run, watchdog advice) and argus#1567 (read-only
broker-versus-database report). Everything here runs on fakes: no broker, no database.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

from core import settle
from core.position_report import compare_holdings, format_report, run_report
from core.settle import SettleDateError, settle_day, set_settle_day
from sessions.eod import get_open_positions, get_today_session_id, get_today_trades, main
from tests.conftest import make_query

_ET = pytz.timezone("America/New_York")
_SID = "sess-1"


class _FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 6)


@pytest.fixture
def today_is_oct6():
    with patch("core.settle.date", _FixedDate):
        yield


# ── settle_day ────────────────────────────────────────────────────────────────

class TestDefaultPathUnchanged:
    def test_default_is_the_clock(self, today_is_oct6):
        assert settle_day() == date(2026, 10, 6)

    def test_default_session_lookup_is_called_exactly_as_before(self):
        with patch("core.run_state.today_premarket_run_id", return_value="x") as m:
            assert get_today_session_id() == "x"
        m.assert_called_once_with()

    def test_default_queries_use_todays_date(self, mock_supabase, today_is_oct6):
        q = make_query([])
        mock_supabase.table.return_value = q
        get_today_trades(_SID)
        get_open_positions(_SID)
        eq_calls = [c.args for c in q.eq.call_args_list]
        assert ("close_date", "2026-10-06") in eq_calls
        assert ("open_date", "2026-10-06") in eq_calls

    def test_blank_env_var_means_default(self):
        assert settle.requested_date(None, {"EOD_DATE": "  "}) is None


class TestExplicitDate:
    def test_explicit_day_flows_to_queries_and_session_lookup(self, mock_supabase, today_is_oct6):
        set_settle_day(date(2026, 10, 5))
        q = make_query([])
        mock_supabase.table.return_value = q
        get_today_trades(_SID)
        get_open_positions(_SID)
        eq_calls = [c.args for c in q.eq.call_args_list]
        assert ("close_date", "2026-10-05") in eq_calls
        assert ("open_date", "2026-10-05") in eq_calls
        with patch("core.run_state.today_premarket_run_id", return_value="x") as m:
            get_today_session_id()
        m.assert_called_once_with("2026-10-05", bounded=True)

    def test_cli_value_beats_env(self):
        assert settle.requested_date("2026-10-01", {"EOD_DATE": "2026-10-02"}) == "2026-10-01"
        assert settle.requested_date(None, {"EOD_DATE": "2026-10-02"}) == "2026-10-02"

    def test_bounded_lookup_only_reads_that_day(self):
        from core import run_state
        q = make_query([])
        q.lt = MagicMock(return_value=q)
        client = MagicMock()
        client.table.return_value = q
        with patch("core.db.get_client", return_value=client), \
             patch("core.db.execute_with_retry", side_effect=lambda r, description="": r.execute()):
            run_state.today_premarket_run_id("2026-10-05", bounded=True)
            q.lt.assert_called_once_with("started_at", "2026-10-06")
            q.lt.reset_mock()
            run_state.today_premarket_run_id("2026-10-05")          # default: unbounded, as before
            q.lt.assert_not_called()


def _validate(d, weekday_ok=True, market_open=True):
    settle.validate_settle_date(
        d, is_trading_weekday=lambda w: weekday_ok, market_open_on=lambda x: market_open,
        now_et=_ET.localize(datetime(2026, 10, 6, 18, 0)),
    )


class TestRefusals:
    def test_bad_format(self):
        with pytest.raises(SettleDateError, match="not a date"):
            settle.parse_settle_date("05/10/2026")

    def test_future_date(self, today_is_oct6):
        with pytest.raises(SettleDateError, match="future"):
            _validate(date(2026, 10, 7))

    def test_today_and_past_pass(self, today_is_oct6):
        _validate(date(2026, 10, 6))
        _validate(date(2026, 10, 5))

    def test_not_a_trading_weekday(self, today_is_oct6):
        with pytest.raises(SettleDateError, match="not a configured trading day"):
            _validate(date(2026, 10, 3), weekday_ok=False)

    def test_market_holiday(self, today_is_oct6):
        with pytest.raises(SettleDateError, match="not a market trading day"):
            _validate(date(2026, 9, 7), market_open=False)

    def test_main_refuses_and_touches_nothing(self, mock_supabase, capsys):
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("core.market_calendar.check_market_day") as cal, \
             patch("sessions.eod.TraceLogger") as tr:
            with pytest.raises(SystemExit) as e:
                main(["--date", "2999-01-01"])
        assert e.value.code == 2
        assert "REFUSED" in capsys.readouterr().out
        tr.assert_not_called()
        cal.assert_not_called()   # refused on the future-date rule before any lookup
        assert settle.explicit_settle_day() is None

    def test_main_refuses_a_holiday(self, mock_supabase, capsys):
        closed = MagicMock(open=False)
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("core.market_calendar.check_market_day", return_value=closed), \
             patch("sessions.eod.TraceLogger") as tr:
            with pytest.raises(SystemExit):
                main(["--date", "2026-09-07"])
        tr.assert_not_called()


# ── dry run ───────────────────────────────────────────────────────────────────

def _fake_broker(holdings: dict, owned: set):
    c = MagicMock()
    c.get_all_positions.return_value = [MagicMock(symbol=s, qty=str(q)) for s, q in holdings.items()]
    c.get_orders.return_value = [MagicMock(symbol=s, client_order_id=f"stratc_{s}") for s in owned]
    return c


class TestDryRun:
    def test_prints_the_plan_and_places_and_writes_nothing(self, mock_supabase, capsys):
        open_rows = [{"id": 1, "ticker": "AAPL", "shares": 10, "entry_price": 100.0,
                      "entry_time": "x", "alpaca_order_id": "o", "trail_order_id": None}]
        q = make_query(open_rows)
        mock_supabase.table.return_value = q
        broker = _fake_broker({"AAPL": 10, "CL": 32}, owned={"AAPL"})
        open_day = MagicMock(open=True, day=date(2026, 10, 5), reason=None, source="alpaca")
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("core.settle.date", _FixedDate), \
             patch("core.market_calendar.check_market_day", return_value=open_day), \
             patch("core.alpaca._client", return_value=broker), \
             patch("core.alpaca._is_market_open", return_value=False), \
             patch("core.run_state.today_premarket_run_id", return_value=_SID), \
             patch("sessions.eod.TraceLogger") as tr, \
             patch("sessions.eod.send_alert") as alert:
            main(["--date", "2026-10-05", "--dry-run"])
        out = capsys.readouterr().out
        assert "settling 2026-10-05 as a re-run" in out
        assert "DRY RUN for 2026-10-05" in out
        assert "AAPL" in out and "Would submit market closes" in out
        assert "CL" in out and "NOT selected" in out
        assert "unprotected" in out
        assert "Broker reconciliation" in out
        # nothing placed
        for name in ("submit_order", "close_position", "close_all_positions", "cancel_orders",
                     "cancel_order_by_id"):
            assert not getattr(broker, name).called, name
        # nothing written
        for name in ("update", "insert", "upsert", "delete"):
            assert not getattr(q, name).called, name
        tr.assert_not_called()      # no trace session opened either
        alert.assert_not_called()

    def test_dry_run_without_a_date_settles_today(self, mock_supabase, capsys):
        mock_supabase.table.return_value = make_query([])
        broker = _fake_broker({}, owned=set())
        open_day = MagicMock(open=True, day=date(2026, 10, 6), reason=None, source="alpaca")
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("core.market_calendar.check_market_day", return_value=open_day), \
             patch("core.alpaca._client", return_value=broker), \
             patch("core.alpaca._is_market_open", return_value=True), \
             patch("core.run_state.today_premarket_run_id", return_value=_SID), \
             patch("sessions.eod.TraceLogger") as tr:
            main(["--dry-run"])
        out = capsys.readouterr().out
        assert "re-run" not in out
        tr.assert_not_called()


# ── reconciliation report ─────────────────────────────────────────────────────

class TestReconciliationReport:
    def test_match(self):
        r = compare_holdings([{"ticker": "AAPL", "shares": 10}], {"AAPL": 10.0})
        assert r["ok"] and r["status"] == "match"
        assert "match" in format_report(r)

    def test_orphan_at_broker(self):
        r = compare_holdings([], {"CL": 32.0})
        assert not r["ok"]
        assert r["orphans_at_broker"] == [{"ticker": "CL", "broker_qty": 32.0}]
        assert "CL: broker holds 32" in format_report(r)

    def test_row_without_holding(self):
        r = compare_holdings([{"ticker": "JPM", "shares": 8}], {})
        assert r["rows_without_holding"] == [{"ticker": "JPM", "db_shares": 8}]
        assert "broker holds none" in format_report(r)

    def test_quantity_mismatch_and_two_rows_sum(self):
        r = compare_holdings(
            [{"ticker": "PSX", "shares": 2}, {"ticker": "PSX", "shares": 1},
             {"ticker": "AAPL", "shares": 10}],
            {"PSX": 3.0, "AAPL": 7.0},
        )
        assert r["quantity_mismatches"] == [{"ticker": "AAPL", "db_shares": 10, "broker_qty": 7.0}]
        assert not r["orphans_at_broker"] and not r["rows_without_holding"]

    def test_broker_read_fails_is_not_a_pass(self):
        r = compare_holdings([{"ticker": "AAPL", "shares": 10}], None)
        assert r["status"] == "broker_unreadable" and r["ok"] is False
        text = format_report(r)
        assert "could not read the broker" in text and "not a pass" in text

    def test_run_report_with_a_failing_broker_never_raises(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"ticker": "AAPL", "shares": 10}])
        broker = MagicMock()
        broker.get_all_positions.side_effect = RuntimeError("boom")
        with patch("core.alpaca._client", return_value=broker):
            r = run_report()
        assert r["status"] == "broker_unreadable"

    def test_run_report_with_a_failing_database_never_raises(self):
        with patch("core.alpaca.get_broker_holdings", return_value={}), \
             patch("core.db.get_client", side_effect=RuntimeError("db down")):
            r = run_report()
        assert r["status"] == "report_failed" and r["ok"] is False
        assert "not a pass" in format_report(r)

    def test_report_only_reads(self, mock_supabase):
        q = make_query([{"ticker": "AAPL", "shares": 10}])
        mock_supabase.table.return_value = q
        broker = _fake_broker({"AAPL": 10, "CL": 32}, owned=set())
        with patch("core.alpaca._client", return_value=broker):
            run_report()
        for name in ("update", "insert", "upsert", "delete"):
            assert not getattr(q, name).called
        for name in ("submit_order", "close_position", "cancel_orders"):
            assert not getattr(broker, name).called

    def test_eod_alert_carries_the_report_and_a_failed_report_does_not_stop_eod(self, mock_supabase):
        perf_session = MagicMock()
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("core.market_calendar.check_market_day", return_value=MagicMock(open=True)), \
             patch("sessions.eod.get_today_session_id", return_value=_SID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": False}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions", return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=[]), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={}), \
             patch("evals.outcomes.push_trade_outcomes", return_value=0), \
             patch("evals.outcomes.push_outcome_signals", return_value=True), \
             patch("evals.outcomes.backfill_server_judge"), \
             patch("sessions.eod.check_protection_status", return_value=MagicMock(tier=0)), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("core.alpaca.get_broker_holdings", return_value=None), \
             patch("sessions.eod.send_alert") as alert, \
             patch("sessions.eod.TraceLogger") as tr:
            main([])
        body = alert.call_args.args[1]
        assert "could not read the broker" in body
        decisions = [c.args[1] for c in tr.return_value.log_decision.call_args_list]
        assert "broker_reconciliation" in decisions and "eod_complete" in decisions


# ── watchdog text ─────────────────────────────────────────────────────────────

class TestWatchdogAdvice:
    def test_missing_performance_says_what_to_do_with_the_exact_command(self):
        from tests.sessions.test_watchdog_health import _at, _COMPLETED, _run
        problems = _run(_at(17, 0), premarket=_COMPLETED, perf=False, heartbeat_age=5.0)
        text = next(p for p in problems if "No end-of-day performance" in p)
        assert "python sessions/eod.py --date 2026-07-27 --dry-run" in text
        assert "python sessions/eod.py --date 2026-07-27`" in text
        assert "cancels EVERY open broker order" in text
        assert "queue for the next open" in text

    def test_threshold_is_unchanged(self):
        from sessions.watchdog import _EOD_LATE_AFTER
        from datetime import time
        assert _EOD_LATE_AFTER == time(16, 30)
