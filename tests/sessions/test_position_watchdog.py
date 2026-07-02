from __future__ import annotations

from datetime import datetime, time
from unittest.mock import MagicMock, call, patch

import pytest

import pytz

_ET = pytz.timezone("America/New_York")
_TRADING_WEEKDAY = "MON"
_POLL_TIME = time(10, 0)   # inside poll window
_OUTSIDE_TIME = time(8, 0)  # before poll window


def _make_dt(t: time) -> datetime:
    return datetime.now(_ET).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


class TestPositionWatchdogMain:
    """sessions/position_watchdog.py main() — unit tests with all external calls mocked."""

    def _run(self, weekday=_TRADING_WEEKDAY, now_t=_POLL_TIME, session_id="sess-001",
             suspended=False, pending_trades=None):
        """Run position_watchdog.main() with standard mocks."""
        protection = MagicMock()
        protection.suspended = suspended
        protection.reason = "test"

        params = MagicMock()
        params.trail_pct = 0.025

        def mock_now(_tz):
            dt = MagicMock()
            dt.strftime.return_value = weekday
            dt.time.return_value = now_t
            return dt

        with patch("sessions.position_watchdog.datetime") as mock_dt, \
             patch("sessions.position_watchdog.is_trading_day", return_value=(weekday != "SAT")), \
             patch("sessions.position_watchdog.get_premarket_session_id", return_value=session_id), \
             patch("sessions.position_watchdog.check_protection_status", return_value=protection), \
             patch("sessions.position_watchdog.load_params", return_value=params), \
             patch("sessions.position_watchdog._sync_positions") as mock_sync, \
             patch("sessions.position_watchdog._execute_pending_trades") as mock_pending:
            mock_dt.now.side_effect = mock_now
            from sessions.position_watchdog import main
            main()

        return mock_sync, mock_pending

    def test_syncs_positions_on_trading_day(self):
        mock_sync, _ = self._run()
        mock_sync.assert_called_once_with("sess-001", 0.025)

    def test_skips_on_non_trading_day(self):
        mock_sync, _ = self._run(weekday="SAT")
        mock_sync.assert_not_called()

    def test_skips_outside_poll_window(self):
        mock_sync, _ = self._run(now_t=_OUTSIDE_TIME)
        mock_sync.assert_not_called()

    def test_skips_when_no_premarket_session(self):
        mock_sync, _ = self._run(session_id=None)
        mock_sync.assert_not_called()

    def test_skips_when_protection_suspended(self):
        mock_sync, _ = self._run(suspended=True)
        mock_sync.assert_not_called()

    def test_calls_pending_trades_after_9_45(self):
        _, mock_pending = self._run(now_t=time(10, 0))
        mock_pending.assert_called_once()

    def test_no_pending_trades_before_9_45(self):
        _, mock_pending = self._run(now_t=time(9, 30))
        mock_pending.assert_not_called()


class TestExecutePendingTrades:
    """_execute_pending_trades clears pending_trades metadata after execution."""

    def test_executes_and_clears_pending(self, mock_supabase):
        from tests.conftest import make_query
        pending = [{"ticker": "AAPL", "entry_price": 200.0}]
        meta    = {"pending_trades": pending, "other_key": "preserved"}

        calls_iter = iter([
            make_query([{"metadata": meta}]),  # first call: fetch session metadata
            make_query([]),                      # second call: update session metadata
        ])
        mock_supabase.table.side_effect = lambda _: next(calls_iter)

        with patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("core.db.get_client", return_value=mock_supabase):
            from sessions.position_watchdog import _execute_pending_trades
            _execute_pending_trades("sess-001", 0.025)

        mock_exec.assert_called_once_with(pending, "sess-001", 0.025)

    def test_no_execution_when_no_pending(self, mock_supabase):
        from tests.conftest import make_query
        mock_supabase.table.return_value = make_query([{"metadata": {}}])

        with patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("core.db.get_client", return_value=mock_supabase):
            from sessions.position_watchdog import _execute_pending_trades
            _execute_pending_trades("sess-001", 0.025)

        mock_exec.assert_not_called()


class TestReconcileOpeningOrders:
    """position_watchdog._reconcile_opening_orders — backfill OPG fills + attach trailing stops."""

    def test_backfills_fill_attaches_trail_flips_open(self, mock_supabase):
        from tests.conftest import make_query
        pending = {"id": "p1", "ticker": "AAPL", "shares": 10,
                   "alpaca_order_id": "opg-1", "status": "pending_open"}
        updated = {}

        def capture_update(data):
            updated.update(data)
            return make_query([])

        q = make_query([pending])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_bracket_status", return_value={"entry_price": 185.2}), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-1"):
            from sessions.position_watchdog import _reconcile_opening_orders
            n = _reconcile_opening_orders(0.015)

        assert n == 1
        assert updated.get("status") == "open"
        assert updated.get("entry_price") == 185.2
        assert updated.get("trail_order_id") == "trail-1"

    def test_skips_unfilled_order(self, mock_supabase):
        from tests.conftest import make_query
        pending = {"id": "p1", "ticker": "AAPL", "shares": 10,
                   "alpaca_order_id": "opg-1", "status": "pending_open"}
        mock_supabase.table.return_value = make_query([pending])

        with patch("core.alpaca.get_bracket_status", return_value={"entry_price": None}), \
             patch("core.alpaca.submit_trailing_stop") as trail:
            from sessions.position_watchdog import _reconcile_opening_orders
            n = _reconcile_opening_orders(0.015)

        assert n == 0
        trail.assert_not_called()
