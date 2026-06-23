from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.tools.learning_tools import (
    adjust_param,
    read_recent_learnings,
    read_session_context,
    read_strategy_params,
    read_today_trades,
    recommend_goal,
    write_learning,
)
from tests.conftest import make_query


# ── read_today_trades ──────────────────────────────────────────────────────────

class TestReadTodayTrades:
    def test_returns_closed_trades(self, mock_supabase):
        rows = [
            {"ticker": "AAPL", "realized_pnl": 150.0, "exit_reason": "TARGET",
             "entry_price": 185.0, "exit_price": 187.0, "entry_time": "09:45",
             "close_time": "11:30", "position_size": 5000, "score_at_entry": 8},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = read_today_trades()
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_returns_empty_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert read_today_trades() == []

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = read_today_trades()
        assert "error" in result[0]


# ── read_session_context ───────────────────────────────────────────────────────

class TestReadSessionContext:
    def test_returns_session_row_with_flattened_metadata(self, mock_supabase):
        rows = [{"id": "sess-001", "terminal_reason": "converged",
                 "metadata": {"spy_change_pct": -0.4, "regime": "neutral"}}]
        mock_supabase.table.return_value = make_query(rows)
        result = read_session_context("sess-001")
        assert result["id"] == "sess-001"
        assert result["terminal_reason"] == "converged"
        assert result["spy_change_pct"] == -0.4 and result["regime"] == "neutral"  # metadata flattened
        assert "metadata" not in result

    def test_returns_error_when_not_found(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = read_session_context("missing-id")
        assert "error" in result

    def test_returns_error_on_db_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = read_session_context("sess-001")
        assert "error" in result


# ── read_strategy_params ───────────────────────────────────────────────────────

class TestReadStrategyParams:
    def test_returns_all_param_rows(self, mock_supabase):
        rows = [
            {"param_key": "strategy_min_score", "param_value": 5, "default_value": 5,
             "min_bound": 3, "max_bound": 9, "cooldown_until": None, "updated_by": "seed"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = read_strategy_params()
        assert len(result) == 1
        assert result[0]["param_name"] == "strategy_min_score"      # mapped from param_key
        assert result[0]["current_value"] == 5                      # from param_value
        assert result[0]["min_value"] == 3 and result[0]["max_value"] == 9
        assert result[0]["last_adjusted_by"] == "seed"             # from updated_by

    def test_returns_empty_when_no_params(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert read_strategy_params() == []

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = read_strategy_params()
        assert "error" in result[0]


# ── read_recent_learnings ──────────────────────────────────────────────────────

class TestReadRecentLearnings:
    def test_returns_learnings(self, mock_supabase):
        rows = [
            {"session_date": "2026-05-27", "learning_type": "observation",
             "dimension": "entry_quality", "finding": "bid entries outperform",
             "param_key": "trail_pct", "confidence": "high", "outcome": "pending"},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = read_recent_learnings()
        assert len(result) == 1
        assert result[0]["learning_type"] == "observation"
        assert result[0]["learning_date"] == "2026-05-27"     # mapped from session_date
        assert result[0]["param_adjusted"] == "trail_pct"     # mapped from param_key

    def test_returns_empty_when_none(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert read_recent_learnings() == []


# ── write_learning ─────────────────────────────────────────────────────────────

class TestWriteLearning:
    def test_writes_row_and_returns_id(self, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        result = write_learning(
            learning_type="observation",
            dimension="entry_quality",
            finding="bid entries beat mid entries",
            sample_size=5,
            confidence=0.85,
        )

        assert result["status"] == "written"
        assert len(result["id"]) == 36

    def test_insert_maps_to_real_schema_columns(self, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        write_learning(
            learning_type="adjustment",
            dimension="parameter",
            finding="min_score raised",
            param_adjusted="strategy_min_score",
            old_value=4.0,
            new_value=5.0,
            sample_size=6,
            confidence=0.9,
        )

        row = query.insert.call_args[0][0]
        assert row["learning_type"] == "adjustment"
        # Mapped to the actual c_learnings columns
        assert row["param_key"] == "strategy_min_score"
        assert row["old_param_value"] == 4.0
        assert row["new_param_value"] == 5.0
        assert row["confidence"] == "high"          # numeric -> TEXT
        assert row["session_date"] and row["expires_date"]
        assert "(n=6)" in row["finding"]
        # Columns that don't exist on c_learnings must NOT be sent (these failed the insert)
        for bad in ("learning_date", "param_adjusted", "old_value", "new_value", "sample_size", "active"):
            assert bad not in row

    def test_returns_error_on_db_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = write_learning("observation", "entry_quality", "finding")
        assert "error" in result


# ── adjust_param ───────────────────────────────────────────────────────────────

class TestAdjustParam:
    def test_returns_applied_on_success(self, mock_supabase):
        query = make_query([
            {"param_key": "strategy_min_score", "param_value": 4,
             "min_bound": 3, "max_bound": 9, "cooldown_until": None,
             "default_value": 5, "cooldown_days": 3, "previous_value": None}
        ])
        mock_supabase.table.return_value = query

        result = adjust_param("strategy_min_score", 5.0, "win rate improved")
        assert result["status"] == "applied"
        assert result["new_value"] == 5.0

    def test_returns_rejected_when_out_of_bounds(self, mock_supabase):
        query = make_query([
            {"param_key": "strategy_min_score", "param_value": 5,
             "min_bound": 3, "max_bound": 9, "cooldown_until": None,
             "default_value": 5, "cooldown_days": 3, "previous_value": None}
        ])
        mock_supabase.table.return_value = query

        result = adjust_param("strategy_min_score", 15.0, "test")
        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "out_of_bounds"

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = adjust_param("strategy_min_score", 5.0, "test")
        assert "error" in result


# ── recommend_goal ─────────────────────────────────────────────────────────────

class TestRecommendGoal:
    def test_writes_goal_recommendation_learning(self, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        result = recommend_goal(
            goal_type="daily_pnl_target",
            target_value=400.0,
            rationale="Win rate 80% over 5 days suggests higher target is achievable.",
        )

        assert result["status"] == "written"
        row = query.insert.call_args[0][0]
        assert row["learning_type"] == "goal_recommendation"
        assert "400.0" in row["finding"]
