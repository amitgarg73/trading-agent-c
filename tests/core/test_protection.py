from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import pytz

from core.protection import (
    TIER_THRESHOLDS,
    ProtectionEvent,
    ProtectionStatus,
    check_protection_status,
    check_tier1_soft_floor,
    record_protection_event,
)
from tests.conftest import make_query


# ── Helpers ────────────────────────────────────────────────────────────────────

def _patch_helpers(
    active_suspension=None,
    daily_pnl: float = 0.0,
    rolling_pnl: float = 0.0,
    consecutive: int = 0,
    drawdown_pct: float = 0.0,
):
    """Return a context manager tuple patching all private helpers."""
    return (
        patch("core.protection._check_active_suspension", return_value=active_suspension),
        patch("core.protection._get_daily_pnl",              return_value=daily_pnl),
        patch("core.protection._get_rolling_pnl",            return_value=rolling_pnl),
        patch("core.protection._get_consecutive_losing_days", return_value=consecutive),
        patch("core.protection._get_account_drawdown_pct",   return_value=drawdown_pct),
        patch("core.protection.record_protection_event"),
    )


# ── check_protection_status ────────────────────────────────────────────────────

class TestCheckProtectionStatus:
    def test_returns_clean_status_when_no_triggers(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=100.0),
            patch("core.protection._get_rolling_pnl",            return_value=50.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is False
        assert status.tier == 0
        assert status.reason == ""

    def test_active_suspension_wins_immediately(self):
        active = ProtectionStatus(
            suspended=True, tier=4, reason="Prior event", action="suspended_24h"
        )
        with patch("core.protection._check_active_suspension", return_value=active):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 4

    def test_tier2_hard_stop_triggered(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-600.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=0.0),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 2
        assert status.action == "stopped_day"
        assert "-600" in status.reason or "500" in status.reason

    def test_tier2_not_triggered_above_threshold(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-499.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=0.0),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is False
        assert status.tier != 2

    def test_tier2_at_exact_threshold(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-500.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=0.0),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 2

    def test_tier3_rolling_reduces_max_positions(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-100.0),
            patch("core.protection._get_rolling_pnl",            return_value=-1200.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
            patch("core.protection._get_current_max_positions",  return_value=10),
        ):
            status = check_protection_status()

        assert status.suspended is False
        assert status.tier == 3
        assert status.action == "reduced_sizing"
        assert status.max_positions_override == 5  # 10 // 2

    def test_tier3_not_triggered_above_threshold(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-100.0),
            patch("core.protection._get_rolling_pnl",            return_value=-999.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.tier != 3

    def test_tier4_drawdown_10pct_suspends_24h(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=0.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.12),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 4
        assert status.action == "suspended_24h"
        assert status.resume_at is not None
        # resume_at should be ~24h from now
        delta = status.resume_at - datetime.now(pytz.utc)
        assert timedelta(hours=23) < delta < timedelta(hours=25)

    def test_tier5_drawdown_20pct_suspends_7_days(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=0.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.25),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 5
        assert status.action == "suspended_7days_human_required"
        delta = status.resume_at - datetime.now(pytz.utc)
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)

    def test_tier5_takes_priority_over_tier4(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=0.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.22),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.tier == 5  # -22% triggers tier 5 before tier 4

    def test_tier6_five_consecutive_losses_suspends(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-100.0),
            patch("core.protection._get_rolling_pnl",            return_value=-300.0),
            patch("core.protection._get_consecutive_losing_days", return_value=5),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.suspended is True
        assert status.tier == 6
        assert status.action == "suspended_human_required"

    def test_tier6_four_consecutive_does_not_trigger(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-100.0),
            patch("core.protection._get_rolling_pnl",            return_value=-300.0),
            patch("core.protection._get_consecutive_losing_days", return_value=4),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.tier != 6

    def test_record_protection_event_called_on_trigger(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-600.0),
            patch("core.protection._get_rolling_pnl",            return_value=0.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=0.0),
            patch("core.protection.record_protection_event") as mock_record,
        ):
            check_protection_status()

        mock_record.assert_called_once()
        event_arg = mock_record.call_args[0][0]
        assert isinstance(event_arg, ProtectionEvent)
        assert event_arg.tier == 2

    def test_no_record_called_when_clean(self):
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=100.0),
            patch("core.protection._get_rolling_pnl",            return_value=200.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event") as mock_record,
        ):
            check_protection_status()

        mock_record.assert_not_called()

    def test_tier2_wins_over_tier3(self):
        # Both tier 2 and tier 3 conditions met — tier 2 (checked first) should win
        with (
            patch("core.protection._check_active_suspension", return_value=None),
            patch("core.protection._get_daily_pnl",              return_value=-600.0),
            patch("core.protection._get_rolling_pnl",            return_value=-1500.0),
            patch("core.protection._get_consecutive_losing_days", return_value=0),
            patch("core.protection._get_account_drawdown_pct",   return_value=-0.01),
            patch("core.protection.record_protection_event"),
        ):
            status = check_protection_status()

        assert status.tier == 2


# ── check_tier1_soft_floor ─────────────────────────────────────────────────────

class TestCheckTier1SoftFloor:
    def test_triggers_at_threshold(self):
        assert check_tier1_soft_floor(-200.0) is True

    def test_triggers_below_threshold(self):
        assert check_tier1_soft_floor(-250.0) is True

    def test_does_not_trigger_above_threshold(self):
        assert check_tier1_soft_floor(-199.0) is False

    def test_does_not_trigger_on_profit(self):
        assert check_tier1_soft_floor(100.0) is False

    def test_does_not_trigger_at_zero(self):
        assert check_tier1_soft_floor(0.0) is False


# ── record_protection_event ────────────────────────────────────────────────────

class TestRecordProtectionEvent:
    def test_inserts_event_when_no_existing(self, mock_supabase):
        # First call (existence check) returns empty; second call (insert) returns dummy
        check_query = make_query([])    # no existing event
        insert_query = make_query([{}]) # insert succeeds
        mock_supabase.table.side_effect = [check_query, insert_query]

        event = ProtectionEvent(
            event_date=date.today(),
            tier=2,
            trigger_field="daily_pnl",
            trigger_value=-600.0,
            threshold=-500.0,
            action="stopped_day",
            description="Daily loss limit hit",
        )
        record_protection_event(event)

        assert insert_query.insert.called

    def test_skips_insert_when_event_already_exists(self, mock_supabase):
        existing_query = make_query([{"id": "some-uuid"}])  # event exists
        mock_supabase.table.side_effect = [existing_query]

        event = ProtectionEvent(
            event_date=date.today(),
            tier=2,
            trigger_field="daily_pnl",
            trigger_value=-600.0,
            threshold=-500.0,
            action="stopped_day",
            description="Already recorded",
        )
        record_protection_event(event)

        # insert() should not have been called since event already exists
        existing_query.insert.assert_not_called()

    def test_includes_suspended_until_when_set(self, mock_supabase):
        check_query = make_query([])
        insert_query = make_query([{}])
        mock_supabase.table.side_effect = [check_query, insert_query]

        suspended_until = datetime.now(pytz.utc) + timedelta(hours=24)
        event = ProtectionEvent(
            event_date=date.today(),
            tier=4,
            trigger_field="account_drawdown_pct",
            trigger_value=-0.12,
            threshold=-0.10,
            action="suspended_24h",
            description="10% drawdown",
            suspended_until=suspended_until,
        )
        record_protection_event(event)

        # Verify insert was called (payload includes suspended_until)
        assert insert_query.insert.called
        payload = insert_query.insert.call_args[0][0]
        assert "suspended_until" in payload


# ── _check_active_suspension ───────────────────────────────────────────────────

class TestCheckActiveSuspension:
    def test_returns_none_when_no_active_suspension(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from core.protection import _check_active_suspension
        result = _check_active_suspension()
        assert result is None

    def test_returns_status_when_active_suspension_found(self, mock_supabase):
        future = (datetime.now(pytz.utc) + timedelta(hours=12)).isoformat()
        mock_supabase.table.return_value = make_query([{
            "tier": 4,
            "action": "suspended_24h",
            "suspended_until": future,
        }])
        from core.protection import _check_active_suspension
        result = _check_active_suspension()
        assert result is not None
        assert result.suspended is True
        assert result.tier == 4
        assert result.action == "suspended_24h"


# ── _get_daily_pnl ─────────────────────────────────────────────────────────────

class TestGetDailyPnl:
    def test_sums_closed_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 100.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": 75.0},
        ])
        from core.protection import _get_daily_pnl
        assert _get_daily_pnl(date.today()) == pytest.approx(125.0)

    def test_returns_zero_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from core.protection import _get_daily_pnl
        assert _get_daily_pnl(date.today()) == 0.0

    def test_handles_none_pnl_values(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": None},
            {"realized_pnl": 50.0},
        ])
        from core.protection import _get_daily_pnl
        assert _get_daily_pnl(date.today()) == pytest.approx(50.0)


# ── _get_consecutive_losing_days ───────────────────────────────────────────────

class TestGetConsecutiveLosingDays:
    def test_counts_consecutive_losses(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": -100.0},
            {"realized_pnl": -50.0},
            {"realized_pnl": -75.0},
            {"realized_pnl": 80.0},   # streak breaks here
            {"realized_pnl": -30.0},
        ])
        from core.protection import _get_consecutive_losing_days
        assert _get_consecutive_losing_days() == 3

    def test_returns_zero_when_last_day_profitable(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 100.0},
            {"realized_pnl": -50.0},
        ])
        from core.protection import _get_consecutive_losing_days
        assert _get_consecutive_losing_days() == 0

    def test_returns_zero_with_no_data(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from core.protection import _get_consecutive_losing_days
        assert _get_consecutive_losing_days() == 0


# ── _get_account_drawdown_pct ──────────────────────────────────────────────────

class TestGetAccountDrawdownPct:
    def test_returns_zero_when_no_data(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from core.protection import _get_account_drawdown_pct
        assert _get_account_drawdown_pct() == 0.0

    def test_returns_zero_when_at_or_above_peak(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 100.0},
            {"realized_pnl": 200.0},
            {"realized_pnl": 50.0},
        ])
        from core.protection import _get_account_drawdown_pct
        result = _get_account_drawdown_pct()
        # Cumulative is 350, peak is 300 — currently above peak, no drawdown
        assert result == 0.0

    def test_calculates_drawdown_correctly(self, mock_supabase, monkeypatch):
        monkeypatch.setenv("TOTAL_CAPITAL", "50000")
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 5000.0},   # peak: 5000, cumulative: 5000
            {"realized_pnl": -7000.0},  # cumulative: -2000 (drawdown from 5000)
        ])
        from core.protection import _get_account_drawdown_pct
        result = _get_account_drawdown_pct()
        # drawdown = (-2000 - 5000) / (50000 + 5000) = -7000 / 55000 ≈ -0.127
        assert result == pytest.approx(-7000 / 55000, rel=1e-4)
