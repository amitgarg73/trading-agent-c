from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from sessions.eod import (
    DailyPerformance,
    _opening_entry_report,
    build_daily_summary,
    compute_performance,
    force_close_positions,
    get_open_positions,
    get_today_session_id,
    get_today_trades,
    reconcile_positions,
    save_performance,
)
from core.protection import ProtectionStatus
from tests.conftest import make_query

_SESSION_ID = "sess-eod-0001"

_TRADES = [
    {
        "ticker": "AAPL", "realized_pnl": 120.0,
        "entry_time": "2026-05-27T09:30:00Z",
        "close_time": "2026-05-27T11:00:00Z",
        "exit_reason": "target", "position_size": 3500,
    },
    {
        "ticker": "TSLA", "realized_pnl": -40.0,
        "entry_time": "2026-05-27T09:45:00Z",
        "close_time": "2026-05-27T10:15:00Z",
        "exit_reason": "stop", "position_size": 2000,
    },
]


class TestGetTodaySessionId:
    def test_returns_id_when_row_exists(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"id": _SESSION_ID}])
        assert get_today_session_id() == _SESSION_ID

    def test_returns_none_when_no_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_today_session_id() is None


class TestGetTodayTrades:
    def test_returns_trade_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query(_TRADES)
        result = get_today_trades(_SESSION_ID)
        assert len(result) == 2
        assert result[0]["ticker"] == "AAPL"

    def test_returns_empty_list_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_today_trades(_SESSION_ID) == []


class TestGetOpenPositions:
    def test_returns_open_positions(self, mock_supabase):
        rows = [{"id": "p1", "ticker": "AAPL", "shares": 10, "entry_time": "2026-05-27T09:30:00Z"}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_open_positions(_SESSION_ID)
        assert len(result) == 1

    def test_returns_empty_when_none(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_open_positions(_SESSION_ID) == []


class TestForceClosePositions:
    def test_returns_count_of_closed_positions(self, mock_supabase):
        positions = [
            {"id": "p1", "ticker": "AAPL", "shares": 10, "entry_time": "2026-05-27T09:30:00Z"},
            {"id": "p2", "ticker": "TSLA", "shares": 5, "entry_time": "2026-05-27T09:45:00Z"},
        ]
        q = make_query(positions)
        mock_supabase.table.return_value = q
        with patch("sessions.eod.get_open_positions", return_value=positions):
            result = force_close_positions(_SESSION_ID)
        assert result == 2

    def test_updates_status_to_closed(self, mock_supabase):
        positions = [{"id": "p1", "ticker": "AAPL", "shares": 10, "entry_time": "2026-05-27T09:30:00Z"}]
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq.return_value = q
            q.execute.return_value = MagicMock(data=[])
            return q

        q = make_query([])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=positions):
            force_close_positions(_SESSION_ID)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "eod_forced"

    def test_returns_zero_when_no_open_positions(self, mock_supabase):
        with patch("sessions.eod.get_open_positions", return_value=[]):
            result = force_close_positions(_SESSION_ID)
        assert result == 0
        mock_supabase.table.assert_not_called()

    def test_writes_realized_pnl_from_fill_price(self, mock_supabase):
        positions = [{"id": "p1", "ticker": "AAPL", "shares": 10, "entry_price": 180.0,
                      "entry_time": "2026-05-27T09:30:00Z", "alpaca_order_id": None}]
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq.return_value = q
            q.execute.return_value = MagicMock(data=[])
            return q

        q = make_query([])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=positions), \
             patch("core.alpaca.cancel_all_orders"), \
             patch("core.alpaca.close_all_strategy_positions",
                   return_value=[{"ticker": "AAPL", "success": True, "fill_price": 185.0}]), \
             patch("core.alpaca.get_position_data", return_value=None):
            force_close_positions(_SESSION_ID)

        assert updated["realized_pnl"] == pytest.approx(50.0)  # (185 - 180) * 10
        assert updated["status"] == "closed"

    def test_falls_back_to_alpaca_snapshot_when_fill_unavailable(self, mock_supabase):
        positions = [{"id": "p1", "ticker": "TSLA", "shares": 5, "entry_price": 200.0,
                      "entry_time": "2026-05-27T09:30:00Z", "alpaca_order_id": None}]
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq.return_value = q
            q.execute.return_value = MagicMock(data=[])
            return q

        q = make_query([])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=positions), \
             patch("core.alpaca.cancel_all_orders"), \
             patch("core.alpaca.close_all_strategy_positions",
                   return_value=[{"ticker": "TSLA", "success": True, "fill_price": None}]), \
             patch("core.alpaca.get_position_data",
                   return_value={"current_price": 210.0, "unrealized_pnl": 75.0}):
            force_close_positions(_SESSION_ID)

        assert updated["realized_pnl"] == pytest.approx(75.0)
        assert updated["status"] == "closed"

    def test_writes_zero_when_no_fill_and_no_snapshot(self, mock_supabase):
        positions = [{"id": "p1", "ticker": "GM", "shares": 20, "entry_price": 50.0,
                      "entry_time": "2026-05-27T09:30:00Z", "alpaca_order_id": None}]
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq.return_value = q
            q.execute.return_value = MagicMock(data=[])
            return q

        q = make_query([])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=positions), \
             patch("core.alpaca.cancel_all_orders"), \
             patch("core.alpaca.close_all_strategy_positions",
                   return_value=[{"ticker": "GM", "success": True, "fill_price": None}]), \
             patch("core.alpaca.get_position_data", return_value=None):
            force_close_positions(_SESSION_ID)

        assert updated["realized_pnl"] == pytest.approx(0.0)

    def test_continues_on_db_error_for_one_position(self, mock_supabase):
        """A DB failure on one position must not prevent others from being closed."""
        positions = [
            {"id": "p1", "ticker": "AAPL", "shares": 10, "entry_price": 180.0,
             "entry_time": "2026-05-27T09:30:00Z", "alpaca_order_id": None},
            {"id": "p2", "ticker": "TSLA", "shares": 5,  "entry_price": 200.0,
             "entry_time": "2026-05-27T09:30:00Z", "alpaca_order_id": None},
        ]
        call_count = [0]

        def capture_update(data):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Supabase timeout")
            q = make_query([])
            q.eq.return_value = q
            q.execute.return_value = MagicMock(data=[])
            return q

        q = make_query([])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=positions), \
             patch("core.alpaca.cancel_all_orders"), \
             patch("core.alpaca.close_all_strategy_positions",
                   return_value=[
                       {"ticker": "AAPL", "success": True, "fill_price": 185.0},
                       {"ticker": "TSLA", "success": True, "fill_price": 205.0},
                   ]), \
             patch("core.alpaca.get_position_data", return_value=None):
            result = force_close_positions(_SESSION_ID)

        assert result == 2          # still returns count of positions processed
        assert call_count[0] == 2   # both positions attempted


class TestComputePerformance:
    def test_realized_pnl_sum(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.realized_pnl == 80.0

    def test_trades_total_count(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.trades_total == 2

    def test_wins_and_losses(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.trades_won == 1
        assert perf.trades_lost == 1

    def test_win_rate(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.win_rate == 0.5

    def test_largest_win(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.largest_win == 120.0

    def test_largest_loss(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        assert perf.largest_loss == -40.0

    def test_avg_hold_min(self):
        perf = compute_performance(_SESSION_ID, _TRADES)
        # AAPL: 90 min, TSLA: 30 min → avg 60 min
        assert perf.avg_hold_min == 60.0

    def test_empty_trades(self):
        perf = compute_performance(_SESSION_ID, [])
        assert perf.realized_pnl == 0.0
        assert perf.trades_total == 0
        assert perf.win_rate == 0.0
        assert perf.largest_win == 0.0
        assert perf.largest_loss == 0.0

    def test_all_winners(self):
        wins = [
            {"ticker": "A", "realized_pnl": 50.0,
             "entry_time": "2026-05-27T09:30:00Z", "close_time": "2026-05-27T10:00:00Z"},
            {"ticker": "B", "realized_pnl": 30.0,
             "entry_time": "2026-05-27T09:30:00Z", "close_time": "2026-05-27T10:00:00Z"},
        ]
        perf = compute_performance(_SESSION_ID, wins)
        assert perf.trades_won == 2
        assert perf.trades_lost == 0
        assert perf.win_rate == 1.0
        assert perf.largest_loss == 0.0


class TestSavePerformance:
    def test_calls_upsert(self, mock_supabase):
        upserted = {}
        q = make_query([])
        q.upsert.side_effect = lambda data: q.__setattr__("_captured", data) or q
        mock_supabase.table.return_value = q

        perf = DailyPerformance(
            session_id=_SESSION_ID, date="2026-05-27",
            realized_pnl=80.0, trades_total=2, trades_won=1, trades_lost=1,
            win_rate=0.5, largest_win=120.0, largest_loss=-40.0, avg_hold_min=60.0,
        )
        save_performance(perf)
        assert mock_supabase.table.called
        table_name = mock_supabase.table.call_args[0][0]
        assert table_name == "c_daily_performance"


class TestBuildDailySummary:
    def _make_perf(self, pnl=80.0, won=1, lost=1, win_rate=0.5):
        return DailyPerformance(
            session_id=_SESSION_ID, date="2026-05-27",
            realized_pnl=pnl, trades_total=won + lost,
            trades_won=won, trades_lost=lost,
            win_rate=win_rate, largest_win=120.0, largest_loss=-40.0, avg_hold_min=60.0,
        )

    def test_contains_pnl(self):
        summary = build_daily_summary(self._make_perf(), _TRADES, None)
        assert "80.00" in summary

    def test_contains_win_loss(self):
        summary = build_daily_summary(self._make_perf(), _TRADES, None)
        assert "1W" in summary or ("1" in summary and "W" in summary)

    def test_contains_winner_ticker(self):
        summary = build_daily_summary(self._make_perf(), _TRADES, None)
        assert "AAPL" in summary

    def test_contains_loser_ticker(self):
        summary = build_daily_summary(self._make_perf(), _TRADES, None)
        assert "TSLA" in summary

    def test_learning_section_when_provided(self):
        learnings = {
            "learnings_written": 3,
            "params_adjusted": 1,
            "context_for_tomorrow": "Tech sector strong.",
        }
        summary = build_daily_summary(self._make_perf(), _TRADES, learnings)
        assert "Learning Agent" in summary
        assert "3" in summary

    def test_no_learning_section_when_none(self):
        summary = build_daily_summary(self._make_perf(), _TRADES, None)
        assert "Learning Agent" not in summary

    def test_positive_pnl_has_plus_sign(self):
        summary = build_daily_summary(self._make_perf(pnl=80.0), _TRADES, None)
        assert "+$80.00" in summary

    def test_negative_pnl_no_plus_sign(self):
        summary = build_daily_summary(self._make_perf(pnl=-30.0), _TRADES, None)
        first_line = summary.splitlines()[0]
        assert "+$" not in first_line


class TestEodMain:
    def _mock_protection(self, tier=0, reason="", suspended=False):
        p = MagicMock()
        p.tier = tier
        p.reason = reason
        p.suspended = suspended
        p.action = "none"
        return p

    def _default_perf(self):
        return DailyPerformance(
            session_id=_SESSION_ID, date="2026-05-27",
            realized_pnl=80.0, trades_total=2, trades_won=1, trades_lost=1,
            win_rate=0.5, largest_win=120.0, largest_loss=-40.0, avg_hold_min=60.0,
        )

    def test_exits_on_non_trading_day(self, mock_supabase, capsys):
        with patch("sessions.eod.is_trading_day", return_value=False):
            from sessions.eod import main
            main()
        assert "Not a trading day" in capsys.readouterr().out

    def test_exits_when_no_session(self, mock_supabase, capsys):
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=None):
            from sessions.eod import main
            main()
        assert "No premarket session" in capsys.readouterr().out

    def test_runs_full_pipeline(self, mock_supabase):
        perf = self._default_perf()
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": True}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=_TRADES), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance") as mock_save, \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("agents.learning_agent.run_learning_agent",
                   return_value={"learnings_written": 1, "params_adjusted": 0,
                                 "context_for_tomorrow": ""}), \
             patch("sessions.eod.send_alert"), \
             patch("sessions.eod.TraceLogger") as mock_tracer_cls:
            mock_tracer_cls.return_value = MagicMock()
            from sessions.eod import main
            main()
        mock_save.assert_called_once()

    def test_reconcile_runs_before_force_close(self, mock_supabase):
        """reconcile_positions must be called before force_close_positions."""
        call_order = []
        perf = self._default_perf()
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": False}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   side_effect=lambda *a: call_order.append("reconcile") or
                   {"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions",
                   side_effect=lambda *a: call_order.append("force_close") or 0), \
             patch("sessions.eod.get_today_trades", return_value=_TRADES), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("sessions.eod.send_alert"), \
             patch("sessions.eod.TraceLogger") as mock_tracer_cls:
            mock_tracer_cls.return_value = MagicMock()
            from sessions.eod import main
            main()
        assert call_order == ["reconcile", "force_close"]

    def test_learning_skipped_when_no_trades(self, mock_supabase):
        perf = DailyPerformance(
            session_id=_SESSION_ID, date="2026-05-27",
            realized_pnl=0.0, trades_total=0, trades_won=0, trades_lost=0,
            win_rate=0.0, largest_win=0.0, largest_loss=0.0, avg_hold_min=0.0,
        )
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": True}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=[]), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("agents.learning_agent.run_learning_agent") as mock_learn, \
             patch("sessions.eod.send_alert"), \
             patch("sessions.eod.TraceLogger") as mock_tracer_cls:
            mock_tracer_cls.return_value = MagicMock()
            from sessions.eod import main
            main()
        mock_learn.assert_not_called()

    def test_sends_alert_on_protection_tier_4(self, mock_supabase):
        perf = self._default_perf()
        perf.protection_tier = 4
        # Real ProtectionStatus (not a MagicMock): it has no `event_date`, so this also
        # guards the regression where EOD tried to re-record it and crashed. See
        # test_protection_tier_does_not_recrash.
        status = ProtectionStatus(
            suspended=True, tier=4, reason="drawdown", action="suspended_24h",
        )
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": False}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=_TRADES), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status", return_value=status), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("sessions.eod.send_alert") as mock_alert, \
             patch("sessions.eod.TraceLogger") as mock_tracer_cls:
            mock_tracer_cls.return_value = MagicMock()
            from sessions.eod import main
            main()
        alert_subjects = [c[0][0] for c in mock_alert.call_args_list]
        assert any("ALERT" in s for s in alert_subjects)

    def test_protection_tier_does_not_recrash(self, mock_supabase):
        """Regression: a triggered tier returns a real ProtectionStatus (no `event_date`).
        EOD must act on it without re-recording — the recording already happened inside
        check_protection_status(). Previously EOD called record_protection_event(status)
        and crashed with AttributeError: 'ProtectionStatus' object has no attribute
        'event_date'."""
        perf = self._default_perf()
        status = ProtectionStatus(
            suspended=True, tier=2, reason="Daily loss limit hit: $-600.00",
            action="stopped_day",
        )
        mock_tracer = MagicMock()
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": False}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=_TRADES), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status", return_value=status), \
             patch("sessions.eod.update_goal_progress") as mock_goal, \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("sessions.eod.send_alert"), \
             patch("sessions.eod.TraceLogger", return_value=mock_tracer):
            from sessions.eod import main
            main()  # must NOT raise AttributeError
        # EOD continued past the protection block to the goal update.
        mock_goal.assert_called_once()
        # The tier trigger was logged as a decision.
        decision_names = [c[0][1] for c in mock_tracer.log_decision.call_args_list]
        assert "tier_2_triggered" in decision_names

    def test_learning_agent_error_does_not_abort(self, mock_supabase):
        perf = self._default_perf()
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id", return_value=_SESSION_ID), \
             patch("sessions.eod.load_agent_config", return_value={"enable_learning_agent": True}), \
             patch("sessions.eod.load_params"), \
             patch("sessions.eod.reconcile_positions",
                   return_value={"entry_updated": 0, "exits_synced": 0, "errors": 0}), \
             patch("sessions.eod.force_close_positions", return_value=0), \
             patch("sessions.eod.get_today_trades", return_value=_TRADES), \
             patch("sessions.eod.compute_performance", return_value=perf), \
             patch("sessions.eod.save_performance"), \
             patch("core.scoring.score_trades", return_value={"trades_scored": 0}), \
             patch("sessions.eod.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.eod.update_goal_progress"), \
             patch("sessions.eod.record_goal_snapshots"), \
             patch("agents.learning_agent.run_learning_agent",
                   side_effect=RuntimeError("LLM error")), \
             patch("sessions.eod.send_alert") as mock_alert, \
             patch("sessions.eod.TraceLogger") as mock_tracer_cls:
            mock_tracer_cls.return_value = MagicMock()
            from sessions.eod import main
            main()  # must NOT raise
        mock_alert.assert_called_once()  # daily summary still sent


# ── reconcile_positions ────────────────────────────────────────────────────────

_OPEN_POS_WITH_ORDER = [
    {
        "id": "p1", "ticker": "AAPL", "shares": 18,
        "entry_price": 185.0, "entry_time": "2026-05-27T09:30:00Z",
        "alpaca_order_id": "ord-bracket-1", "trail_order_id": None,
    },
]

_OPEN_POS_WITH_TRAIL = [
    {
        "id": "p2", "ticker": "AAPL", "shares": 18,
        "entry_price": 185.0, "entry_time": "2026-05-27T09:30:00Z",
        "alpaca_order_id": "ord-bracket-1", "trail_order_id": "ord-trail-1",
    },
]

_STATUS_ENTRY_ONLY = {
    "entry_filled": True,  "entry_price": 185.22,
    "exit_filled":  False, "exit_price": None,
    "exit_reason":  None,  "order_status": "filled",
}

_STATUS_FULL_EXIT = {
    "entry_filled": True,  "entry_price": 185.10,
    "exit_filled":  True,  "exit_price": 192.40,
    "exit_reason":  "TARGET", "order_status": "filled",
}


class TestReconcilePositions:
    def test_returns_zeros_when_no_open_positions(self, mock_supabase):
        with patch("sessions.eod.get_open_positions", return_value=[]):
            result = reconcile_positions(_SESSION_ID)
        assert result == {"entry_updated": 0, "exits_synced": 0, "errors": 0}
        mock_supabase.table.assert_not_called()

    def test_backfills_entry_price_when_fill_differs(self, mock_supabase):
        q = make_query([])
        mock_supabase.table.return_value = q
        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_ORDER), \
             patch("core.alpaca.get_bracket_status", return_value=_STATUS_ENTRY_ONLY):
            result = reconcile_positions(_SESSION_ID)
        assert result["entry_updated"] == 1
        assert result["exits_synced"]  == 0

    def test_closes_position_when_exit_leg_filled(self, mock_supabase):
        updates_captured = {}

        def capture(data):
            updates_captured.update(data)
            r = make_query([])
            r.eq.return_value = r
            return r

        q = make_query([])
        q.update.side_effect = capture
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_ORDER), \
             patch("core.alpaca.get_bracket_status", return_value=_STATUS_FULL_EXIT):
            result = reconcile_positions(_SESSION_ID)

        assert result["exits_synced"] == 1
        assert updates_captured.get("status")      == "closed"
        assert updates_captured.get("exit_reason") == "TARGET"

    def test_realized_pnl_calculated_from_actual_fills(self, mock_supabase):
        # entry_price=185.10, exit=192.40, shares=18 → (192.40-185.10)*18 = $131.40
        updates_captured = {}

        def capture(data):
            updates_captured.update(data)
            r = make_query([])
            r.eq.return_value = r
            return r

        q = make_query([])
        q.update.side_effect = capture
        mock_supabase.table.return_value = q

        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_ORDER), \
             patch("core.alpaca.get_bracket_status", return_value=_STATUS_FULL_EXIT):
            reconcile_positions(_SESSION_ID)

        assert updates_captured.get("realized_pnl") == pytest.approx(131.4)

    def test_counts_error_when_bracket_status_fails(self, mock_supabase):
        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_ORDER), \
             patch("core.alpaca.get_bracket_status", return_value={"error": "not found"}):
            result = reconcile_positions(_SESSION_ID)
        assert result["errors"] == 1
        mock_supabase.table.assert_not_called()

    def test_skips_position_without_alpaca_order_id(self, mock_supabase):
        pos = [{**_OPEN_POS_WITH_ORDER[0], "alpaca_order_id": None, "trail_order_id": None}]
        with patch("sessions.eod.get_open_positions", return_value=pos), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)):
            result = reconcile_positions(_SESSION_ID)
        assert result == {"entry_updated": 0, "exits_synced": 0, "errors": 0}
        mock_supabase.table.assert_not_called()

    def test_closes_via_trail_when_bracket_not_exited(self, mock_supabase):
        """Trailing stop fill detected via trail_order_id when bracket is still open."""
        updates_captured = {}

        def capture(data):
            updates_captured.update(data)
            r = make_query([])
            r.eq.return_value = r
            return r

        q = make_query([])
        q.update.side_effect = capture
        mock_supabase.table.return_value = q

        bracket_no_exit = {**_STATUS_ENTRY_ONLY}
        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_TRAIL), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_no_exit), \
             patch("core.alpaca.get_order_fill", return_value=(191.50, "NATIVE_TRAIL")):
            result = reconcile_positions(_SESSION_ID)

        assert result["exits_synced"] == 1
        assert updates_captured.get("status")      == "closed"
        assert updates_captured.get("exit_reason") == "NATIVE_TRAIL"
        assert updates_captured.get("exit_price")  == pytest.approx(191.50)

    def test_trail_exit_pnl_uses_actual_entry(self, mock_supabase):
        """P&L from trailing stop exit uses actual entry fill price, not proposal price."""
        updates_captured = {}

        def capture(data):
            updates_captured.update(data)
            r = make_query([])
            r.eq.return_value = r
            return r

        q = make_query([])
        q.update.side_effect = capture
        mock_supabase.table.return_value = q

        bracket_with_entry_fill = {**_STATUS_ENTRY_ONLY, "entry_price": 185.22}
        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_TRAIL), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_with_entry_fill), \
             patch("core.alpaca.get_order_fill", return_value=(191.50, "NATIVE_TRAIL")):
            reconcile_positions(_SESSION_ID)

        # (191.50 - 185.22) * 18 = 113.04
        assert updates_captured.get("realized_pnl") == pytest.approx(113.04)

    def test_bracket_exit_takes_priority_over_trail(self, mock_supabase):
        """If bracket exit already fired, trailing stop is not checked."""
        updates_captured = {}

        def capture(data):
            updates_captured.update(data)
            r = make_query([])
            r.eq.return_value = r
            return r

        q = make_query([])
        q.update.side_effect = capture
        mock_supabase.table.return_value = q

        mock_get_fill = MagicMock()
        with patch("sessions.eod.get_open_positions", return_value=_OPEN_POS_WITH_TRAIL), \
             patch("core.alpaca.get_bracket_status", return_value=_STATUS_FULL_EXIT), \
             patch("core.alpaca.get_order_fill", mock_get_fill):
            reconcile_positions(_SESSION_ID)

        mock_get_fill.assert_not_called()
        assert updates_captured.get("exit_reason") == "TARGET"


class TestOpeningEntryReport:
    """EOD entry-basis-vs-open proof metric (opening-entry mode)."""

    def test_empty_when_flag_off(self):
        with patch("sessions.premarket._opening_entry_enabled", return_value=False):
            assert _opening_entry_report([{"ticker": "AAPL", "entry_price": 185.0}]) == ""

    def test_reports_basis_when_flag_on(self):
        with patch("sessions.premarket._opening_entry_enabled", return_value=True), \
             patch("core.alpaca.get_day_open", return_value=185.0):
            out = _opening_entry_report([{"ticker": "AAPL", "entry_price": 185.5}])
        assert "across 1 fill" in out
        assert "+0.27%" in out

    def test_empty_when_no_entry_prices(self):
        with patch("sessions.premarket._opening_entry_enabled", return_value=True), \
             patch("core.alpaca.get_day_open", return_value=None):
            assert _opening_entry_report([{"ticker": "AAPL", "entry_price": 185.0}]) == ""
