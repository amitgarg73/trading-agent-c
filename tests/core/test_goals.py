from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from core.goals import GoalStatus, evaluate_goals, load_active_goals, record_goal_snapshots, update_goal_progress
from tests.conftest import make_query


# ── load_active_goals ──────────────────────────────────────────────────────────

class TestLoadActiveGoals:
    def test_returns_active_goals(self, mock_supabase):
        rows = [
            {"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
             "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None},
        ]
        mock_supabase.table.return_value = make_query(rows)
        goals = load_active_goals()
        assert len(goals) == 1
        assert goals[0]["goal_type"] == "daily_pnl_target"

    def test_returns_empty_when_no_goals(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert load_active_goals() == []

    def test_filters_out_expired_goals(self, mock_supabase):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        rows = [
            {"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
             "current_value": 0.0, "status": "active", "entity_id": None,
             "effective_until": yesterday},
        ]
        mock_supabase.table.return_value = make_query(rows)
        goals = load_active_goals()
        assert goals == []

    def test_includes_goals_with_future_effective_until(self, mock_supabase):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        rows = [
            {"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
             "current_value": 0.0, "status": "active", "entity_id": None,
             "effective_until": tomorrow},
        ]
        mock_supabase.table.return_value = make_query(rows)
        assert len(load_active_goals()) == 1

    def test_includes_goals_with_no_effective_until(self, mock_supabase):
        rows = [
            {"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
             "current_value": 0.0, "status": "active", "entity_id": None,
             "effective_until": None},
        ]
        mock_supabase.table.return_value = make_query(rows)
        assert len(load_active_goals()) == 1


# ── evaluate_goals ─────────────────────────────────────────────────────────────

class TestEvaluateGoals:
    def _goals(self, daily_target=300.0, daily_floor=-200.0):
        rows = []
        if daily_target is not None:
            rows.append({"id": "g1", "goal_type": "daily_pnl_target",
                         "target_value": daily_target, "current_value": 0.0,
                         "status": "active", "entity_id": None, "effective_until": None})
        if daily_floor is not None:
            rows.append({"id": "g2", "goal_type": "daily_pnl_floor",
                         "target_value": daily_floor, "current_value": 0.0,
                         "status": "active", "entity_id": None, "effective_until": None})
        return rows

    def test_lock_in_triggers_at_target(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_target=300.0))
        status = evaluate_goals(300.0)
        assert status.lock_in_mode is True
        assert status.daily_target == 300.0

    def test_lock_in_triggers_above_target(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_target=300.0))
        assert evaluate_goals(350.0).lock_in_mode is True

    def test_lock_in_not_triggered_below_target(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_target=300.0))
        assert evaluate_goals(299.99).lock_in_mode is False

    def test_floor_gate_triggers_at_floor(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_floor=-200.0))
        status = evaluate_goals(-200.0)
        assert status.pnl_floor_hit is True
        assert status.daily_floor == -200.0

    def test_floor_gate_triggers_below_floor(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_floor=-200.0))
        assert evaluate_goals(-250.0).pnl_floor_hit is True

    def test_floor_gate_not_triggered_above_floor(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_floor=-200.0))
        assert evaluate_goals(-199.0).pnl_floor_hit is False

    def test_no_goals_returns_clean_status(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        status = evaluate_goals(100.0)
        assert status.lock_in_mode is False
        assert status.pnl_floor_hit is False
        assert status.daily_target is None
        assert status.daily_floor is None

    def test_both_target_and_floor_evaluated(self, mock_supabase):
        mock_supabase.table.return_value = make_query(self._goals(daily_target=300.0, daily_floor=-200.0))
        # P&L above target — lock in mode, not at floor
        status = evaluate_goals(400.0)
        assert status.lock_in_mode is True
        assert status.pnl_floor_hit is False

    def test_returns_goal_status_dataclass(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert isinstance(evaluate_goals(0.0), GoalStatus)

    def test_non_pnl_goal_types_dont_affect_lock_in(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "win_rate_target",
                 "target_value": 0.60, "current_value": 0.0,
                 "status": "active", "entity_id": None, "effective_until": None}]
        mock_supabase.table.return_value = make_query(rows)
        status = evaluate_goals(1000.0)
        assert status.lock_in_mode is False


# ── update_goal_progress ───────────────────────────────────────────────────────

class TestUpdateGoalProgress:
    def test_marks_target_achieved_when_hit(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        query = make_query(rows)
        mock_supabase.table.return_value = query

        update_goal_progress(350.0)

        assert query.update.called
        payload = query.update.call_args[0][0]
        assert payload["current_value"] == 350.0
        assert payload["status"] == "achieved"

    def test_marks_target_missed_when_not_hit(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        mock_supabase.table.return_value = make_query(rows)

        update_goal_progress(200.0)

        query = mock_supabase.table.return_value
        payload = query.update.call_args[0][0]
        assert payload["status"] == "missed"

    def test_marks_floor_missed_when_breached(self, mock_supabase):
        rows = [{"id": "g2", "goal_type": "daily_pnl_floor", "target_value": -200.0,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        mock_supabase.table.return_value = make_query(rows)

        update_goal_progress(-300.0)

        query = mock_supabase.table.return_value
        payload = query.update.call_args[0][0]
        assert payload["status"] == "missed"

    def test_skips_non_pnl_goal_types(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "win_rate_target", "target_value": 0.60,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        query = make_query(rows)
        mock_supabase.table.return_value = query

        update_goal_progress(100.0)

        # update() should not have been called for non-P&L goal types
        query.update.assert_not_called()

    def test_no_goals_does_nothing(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        # Should not raise
        update_goal_progress(100.0)


# ── record_goal_snapshots ──────────────────────────────────────────────────────

class TestRecordGoalSnapshots:
    def test_writes_snapshot_for_each_pnl_goal(self, mock_supabase):
        rows = [
            {"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
             "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None},
            {"id": "g2", "goal_type": "daily_pnl_floor", "target_value": -200.0,
             "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None},
        ]
        query = make_query(rows)
        mock_supabase.table.return_value = query

        record_goal_snapshots(date.today(), 250.0)

        assert query.upsert.call_count == 2

    def test_snapshot_status_achieved_when_target_hit(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        query = make_query(rows)
        mock_supabase.table.return_value = query

        record_goal_snapshots(date.today(), 350.0)

        payload = query.upsert.call_args[0][0]
        assert payload["status"] == "achieved"
        assert payload["value"] == 350.0

    def test_snapshot_status_missed_when_target_not_hit(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "daily_pnl_target", "target_value": 300.0,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        mock_supabase.table.return_value = make_query(rows)

        record_goal_snapshots(date.today(), 200.0)

        query = mock_supabase.table.return_value
        payload = query.upsert.call_args[0][0]
        assert payload["status"] == "missed"

    def test_skips_non_pnl_goals(self, mock_supabase):
        rows = [{"id": "g1", "goal_type": "win_rate_target", "target_value": 0.60,
                 "current_value": 0.0, "status": "active", "entity_id": None, "effective_until": None}]
        query = make_query(rows)
        mock_supabase.table.return_value = query

        record_goal_snapshots(date.today(), 100.0)

        query.upsert.assert_not_called()

    def test_no_goals_does_nothing(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        record_goal_snapshots(date.today(), 100.0)
