from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest

from sessions.intraday import (
    classify_exit,
    count_open_positions,
    get_daily_pnl,
    get_investigated_tickers,
    get_last_entry_scan_time,
    get_today_session_id,
)
from tests.conftest import make_query

_SESSION_ID = "sess-intra-0001"


class TestGetTodaySessionId:
    def test_returns_id_when_row_exists(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"id": _SESSION_ID}])
        from sessions.intraday import get_today_session_id
        result = get_today_session_id()
        assert result == _SESSION_ID

    def test_returns_none_when_no_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from sessions.intraday import get_today_session_id
        result = get_today_session_id()
        assert result is None


class TestGetDailyPnl:
    def test_sums_realized_pnl(self, mock_supabase):
        rows = [{"realized_pnl": 100.0}, {"realized_pnl": -30.0}, {"realized_pnl": None}]
        mock_supabase.table.return_value = make_query(rows)
        assert get_daily_pnl(_SESSION_ID) == 70.0

    def test_returns_zero_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_daily_pnl(_SESSION_ID) == 0.0


class TestCountOpenPositions:
    def test_returns_count(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"id": "a"}, {"id": "b"}])
        assert count_open_positions(_SESSION_ID) == 2

    def test_returns_zero_when_none(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert count_open_positions(_SESSION_ID) == 0


class TestGetInvestigatedTickers:
    def test_returns_unique_tickers(self, mock_supabase):
        rows = [
            {"entity_id": "AAPL"},
            {"entity_id": "MSFT"},
            {"entity_id": "AAPL"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = get_investigated_tickers(_SESSION_ID)
        assert set(result) == {"AAPL", "MSFT"}

    def test_ignores_none_entity_id(self, mock_supabase):
        rows = [{"entity_id": "AAPL"}, {"entity_id": None}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_investigated_tickers(_SESSION_ID)
        assert None not in result


class TestGetLastEntryScanTime:
    def test_returns_none_when_no_scan_decisions(self, mock_supabase):
        rows = [{"created_at": "2026-05-27T14:00:00", "outcome": "lock_in_mode"}]
        mock_supabase.table.return_value = make_query(rows)
        assert get_last_entry_scan_time(_SESSION_ID) is None

    def test_returns_datetime_for_scan_outcome(self, mock_supabase):
        rows = [
            {"created_at": "2026-05-27T14:30:00", "outcome": "intraday_entries_placed"},
            {"created_at": "2026-05-27T14:00:00", "outcome": "lock_in_mode"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = get_last_entry_scan_time(_SESSION_ID)
        assert result == datetime(2026, 5, 27, 14, 30, 0)

    def test_returns_most_recent_scan_outcome(self, mock_supabase):
        rows = [
            {"created_at": "2026-05-27T15:00:00", "outcome": "no_intraday_candidates"},
            {"created_at": "2026-05-27T14:00:00", "outcome": "intraday_all_rejected"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = get_last_entry_scan_time(_SESSION_ID)
        assert result == datetime(2026, 5, 27, 15, 0, 0)

    def test_returns_none_when_no_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_last_entry_scan_time(_SESSION_ID) is None


class TestClassifyExit:
    def test_limit_sell_is_target(self):
        assert classify_exit({"order_type": "limit", "side": "sell"}) == "target"

    def test_stop_sell_is_stop(self):
        assert classify_exit({"order_type": "stop", "side": "sell"}) == "stop"

    def test_market_sell_is_eod_forced(self):
        assert classify_exit({"order_type": "market", "side": "sell"}) == "eod_forced"

    def test_unknown_is_manual(self):
        assert classify_exit({"order_type": "other", "side": "buy"}) == "manual"

    def test_trailing_stop_sell_is_native_trail(self):
        assert classify_exit({"order_type": "trailing_stop", "side": "sell"}) == "NATIVE_TRAIL"

    def test_trailing_stop_buy_is_manual(self):
        assert classify_exit({"order_type": "trailing_stop", "side": "buy"}) == "manual"


_BRACKET_PATCH = patch("core.alpaca.submit_bracket_order", return_value=("ord-intra-001", 185.0))
_TRAIL_PATCH   = patch("core.alpaca.submit_trailing_stop", return_value="trail-intra-001")

_INTRA_PROPOSAL = {
    "proposals": [
        {
            "ticker": "AAPL",
            "entry_price": 185.0,
            "target_price": 192.4,
            "stop_loss": 183.78,
            "position_size": 3500,
            "shares": 18,
            "confidence": "HIGH",
        },
    ]
}


class TestPlaceIntradayTrades:
    def test_inserts_approved_trades(self, mock_supabase):
        inserted = {}

        def capture(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        from sessions.intraday import _place_intraday_trades
        with _BRACKET_PATCH, _TRAIL_PATCH:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 1
        assert inserted.get("entry_context") == "intraday"
        assert inserted.get("alpaca_order_id") == "ord-intra-001"
        assert inserted.get("trail_order_id") == "trail-intra-001"

    def test_trail_order_id_none_when_fill_pending(self, mock_supabase):
        inserted = {}

        def capture(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=("ord-intra-001", None)), \
             _TRAIL_PATCH as mock_trail:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 1
        mock_trail.assert_not_called()
        assert inserted.get("trail_order_id") is None

    def test_skips_non_approved(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        proposals = {
            "proposals": [
                {
                    "ticker": "MSFT",
                    "entry_price": 300.0,
                    "target_price": 310.0,
                    "stop_loss": 295.0,
                    "position_size": 3000,
                    "shares": 10,
                    "confidence": "MEDIUM",
                },
            ]
        }
        from sessions.intraday import _place_intraday_trades
        with _BRACKET_PATCH, _TRAIL_PATCH:
            count = _place_intraday_trades(proposals, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 0

    def test_skips_rejected_order(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=(None, None)), _TRAIL_PATCH:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 0


class TestSyncPositions:
    """Tests for _sync_positions: trail submission and exit detection."""

    _OPEN_POS = {
        "id": "pos-001",
        "ticker": "AAPL",
        "alpaca_order_id": "ord-001",
        "trail_order_id": None,
        "entry_price": 185.0,
        "shares": 10,
    }

    def test_submits_trailing_stop_when_missing(self, mock_supabase):
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": True, "entry_price": 185.0}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg"), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_called_once_with("AAPL", 10, 0.008)

    def test_backfills_entry_price_on_fill_confirmation(self, mock_supabase):
        pos = {**self._OPEN_POS, "entry_price": 184.0}  # original requested price differs from fill
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        bracket_status = {"entry_filled": True, "entry_price": 185.5}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg"), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("entry_price") == 185.5

    def test_skips_trail_when_entry_not_yet_filled(self, mock_supabase):
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": False, "entry_price": None}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca.submit_trailing_stop") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_not_called()

    def test_skips_trail_submission_when_already_set(self, mock_supabase):
        pos = {**self._OPEN_POS, "trail_order_id": "trail-existing"}
        mock_supabase.table.return_value = make_query([pos])
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.submit_trailing_stop") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_not_called()

    def test_closes_position_on_trail_exit(self, mock_supabase):
        pos = {**self._OPEN_POS, "trail_order_id": "trail-001"}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(187.5, "NATIVE_TRAIL")):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "NATIVE_TRAIL"

    def test_cancels_stop_leg_before_trail_retry(self, mock_supabase):
        """_cancel_bracket_stop_leg is called before submitting the trailing stop."""
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": True, "entry_price": 185.0}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg") as mock_cancel, \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_cancel.assert_called_once_with("ord-001")

    def test_unfilled_limit_order_marked_unfilled_not_external_close(self, mock_supabase):
        """Limit order that never filled should be cancelled and marked unfilled with 0 P&L."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order") as mock_cancel:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "unfilled"
        assert updated.get("realized_pnl") == pytest.approx(0.0)
        mock_cancel.assert_called_once_with("ord-001")

    def test_unfilled_does_not_use_external_close(self, mock_supabase):
        """Unfilled limit order must not produce an external_close exit reason."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("exit_reason") != "external_close"

    def test_external_close_uses_last_trade_price(self, mock_supabase):
        """When entry filled but position gone with no fill record, close at last trade price."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        trade_mock = MagicMock()
        trade_mock.price = 188.0

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": True}), \
             patch("core.alpaca._dclient") as mock_dc:
            mock_dc.return_value.get_stock_latest_trade.return_value = {"AAPL": trade_mock}
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "external_close"
        assert updated.get("exit_price") == pytest.approx(188.0)
        assert updated.get("realized_pnl") == pytest.approx((188.0 - 185.0) * 10)

    def test_external_close_falls_back_to_entry_price_on_data_failure(self, mock_supabase):
        """When last trade fetch fails, falls back to entry_price so the row is still closed."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": True}), \
             patch("core.alpaca._dclient", side_effect=Exception("rate limit")):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "external_close"
        assert updated.get("realized_pnl") == pytest.approx(0.0)  # entry_price == exit_price

    def test_no_rows_returns_early(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.alpaca.get_open_alpaca_tickers") as mock_tickers:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_tickers.assert_not_called()


class TestIntradayMain:
    def _mock_protection(self, suspended=False):
        p = MagicMock()
        p.suspended = suspended
        p.reason = ""
        return p

    def _mock_goal(self, lock_in=False, pnl_floor=False, daily_target=500.0):
        g = MagicMock()
        g.lock_in_mode = lock_in
        g.pnl_floor_hit = pnl_floor
        g.daily_target = daily_target
        return g

    def _mock_params(self):
        from core.params import StrategyParams
        p = StrategyParams()
        p.max_positions = 3
        return p

    def test_exits_on_non_trading_day(self, mock_supabase, capsys):
        with patch("sessions.intraday.is_trading_day", return_value=False):
            from sessions.intraday import main
            main()
        assert "Not a trading day" in capsys.readouterr().out

    def test_exits_outside_poll_window(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 8, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "Outside poll window" in capsys.readouterr().out

    def test_exits_when_no_session(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=None):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "No premarket session" in capsys.readouterr().out

    def test_dispatches_premarket_when_no_session_in_window(self, mock_supabase, capsys):
        """At 9:30 AM with no session yet, intraday dispatches to premarket pipeline."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 30, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=None), \
             patch("sessions.premarket.main") as mock_pm:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        mock_pm.assert_called_once()
        assert "premarket pipeline" in capsys.readouterr().out

    def test_exits_when_protection_suspended(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection(suspended=True)):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "Protection suspended" in capsys.readouterr().out

    def test_exits_in_lock_in_mode(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config", return_value={}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=500.0), \
             patch("sessions.intraday.evaluate_goals",
                   return_value=self._mock_goal(lock_in=True)), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "Lock-in mode" in capsys.readouterr().out

    def test_skips_when_entry_scan_too_recent(self, mock_supabase, capsys):
        import pytz
        from datetime import timedelta
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 30, tzinfo=_ET)
        recent_scan = datetime.utcnow() - timedelta(minutes=20)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": True,
                                 "intraday_entry_min_interval_mins": 55}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday.get_last_entry_scan_time", return_value=recent_scan), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.utcnow.return_value = datetime.utcnow()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "too recent" in capsys.readouterr().out

    def test_entries_disabled_exits_cleanly(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals",
                   return_value=self._mock_goal()), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "Entries disabled" in capsys.readouterr().out

    def test_places_trades_when_candidates_approved(self, mock_supabase):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        proposals = {
            "proposals": [
                {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.0,
                 "stop_loss": 183.0, "position_size": 3500, "shares": 18, "confidence": "HIGH"},
            ]
        }
        verdicts = {"verdicts": [{"ticker": "AAPL", "verdict": "APPROVED"}]}
        mock_supabase.table.return_value = make_query([])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": True, "intraday_min_score_bonus": 1,
                                 "intraday_max_new_positions": 2}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday.count_open_positions", return_value=0), \
             patch("sessions.intraday.get_investigated_tickers", return_value=[]), \
             patch("sessions.intraday.get_last_entry_scan_time", return_value=None), \
             patch("agents.research_agent.run_research_agent", return_value=proposals), \
             patch("agents.risk_agent.run_risk_agent", return_value=verdicts), \
             patch("sessions.intraday._place_intraday_trades", return_value=1) as mock_place, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_place.assert_called_once()

    def test_executes_pending_trades_at_market_open(self, mock_supabase, capsys):
        """Deferred premarket trades stored in ag_sessions.metadata.pending_trades are executed at 9:45+."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 45, tzinfo=_ET)
        _pending = [
            {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.0,
             "stop_loss": 183.0, "position_size": 3500, "shares": 18, "confidence": "HIGH"}
        ]
        mock_supabase.table.return_value = make_query([{"metadata": {"pending_trades": _pending}}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_called_once_with(_pending, _SESSION_ID, mock_tracer_cls.return_value.trail_pct if False else self._mock_params().trail_pct)
        out = capsys.readouterr().out
        assert "deferred premarket" in out

    def test_no_pending_execution_before_945(self, mock_supabase):
        """Before 9:45 AM, pending_trades are not checked."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 15, tzinfo=_ET)
        mock_supabase.table.return_value = make_query([{"pending_trades": [{"ticker": "X"}]}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_not_called()

    def test_no_pending_execution_at_930(self, mock_supabase):
        """At 9:30 AM (before 9:45 gate), pending_trades are not executed."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 30, tzinfo=_ET)
        mock_supabase.table.return_value = make_query([{"pending_trades": [{"ticker": "X"}]}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_not_called()
