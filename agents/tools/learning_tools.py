from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional
from uuid import uuid4


def read_today_trades() -> list[dict[str, Any]]:
    """Fetch all trades closed today for Learning Agent analysis."""
    try:
        from core.db import get_client
        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select(
                "ticker,entry_price,exit_price,realized_pnl,exit_reason,"
                "entry_time,close_time,position_size,score_at_entry"
            )
            .eq("status", "closed")
            .eq("close_date", today)
            .order("close_time")
            .execute()
            .data
        )
        return rows or []
    except Exception as e:
        return [{"error": str(e)}]


def read_session_context(session_id: str) -> dict[str, Any]:
    """Fetch the session row from c_sessions for market context."""
    try:
        from core.db import get_client
        rows = (
            get_client()
            .table("c_sessions")
            .select("*")
            .eq("id", session_id)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else {"error": "session not found"}
    except Exception as e:
        return {"error": str(e)}


def read_strategy_params() -> list[dict[str, Any]]:
    """Fetch all active strategy params with current values and bounds."""
    try:
        from core.db import get_client
        rows = (
            get_client()
            .table("c_strategy_params")
            .select(
                "param_name,current_value,default_value,"
                "min_value,max_value,cooldown_until,last_adjusted_by"
            )
            .eq("active", True)
            .execute()
            .data
        )
        return rows or []
    except Exception as e:
        return [{"error": str(e)}]


def read_recent_learnings(days: int = 14) -> list[dict[str, Any]]:
    """Fetch learnings written in the past N days, newest first."""
    try:
        from core.db import get_client
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = (
            get_client()
            .table("c_learnings")
            .select("learning_date,learning_type,dimension,finding,param_adjusted,confidence")
            .gte("learning_date", cutoff)
            .order("learning_date", desc=True)
            .execute()
            .data
        )
        return rows or []
    except Exception as e:
        return [{"error": str(e)}]


def write_learning(
    learning_type: str,
    dimension: str,
    finding: str,
    param_adjusted: Optional[str] = None,
    old_value: Optional[float] = None,
    new_value: Optional[float] = None,
    sample_size: int = 0,
    confidence: float = 0.0,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist a structured learning to c_learnings.
    learning_type: "observation" | "adjustment" | "false_positive_detected"
    dimension: "entry_quality" | "exit_quality" | "market_regime" | "ticker_pattern" | "parameter"
    """
    try:
        from core.db import get_client
        row = {
            "id":             str(uuid4()),
            "session_id":     session_id,
            "learning_date":  date.today().isoformat(),
            "learning_type":  learning_type,
            "dimension":      dimension,
            "finding":        finding,
            "param_adjusted": param_adjusted,
            "old_value":      old_value,
            "new_value":      new_value,
            "sample_size":    sample_size,
            "confidence":     confidence,
        }
        get_client().table("c_learnings").insert(row).execute()
        return {"status": "written", "id": row["id"]}
    except Exception as e:
        return {"error": str(e)}


def adjust_param(
    param_name: str,
    new_value: float,
    reason: str,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Adjust a strategy parameter via core.params.adjust_param.
    Returns AdjustResult as a dict.
    """
    try:
        from core.params import adjust_param as _adjust
        result = _adjust(param_name, new_value, reason=reason, updated_by=f"learning_agent:{session_id or 'unknown'}")
        return {
            "status":           result.status,
            "rejection_reason": result.rejection_reason,
            "old_value":        result.old_value,
            "new_value":        result.new_value,
            "cooldown_until":   result.cooldown_until,
        }
    except Exception as e:
        return {"error": str(e)}


def recommend_goal(
    goal_type: str,
    target_value: float,
    rationale: str,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Write a goal recommendation to c_learnings as a 'goal_recommendation' learning type.
    Does NOT create the goal — that requires human approval via c_goals insert.
    """
    try:
        return write_learning(
            learning_type="goal_recommendation",
            dimension="parameter",
            finding=f"Recommend goal: {goal_type} target={target_value}. {rationale}",
            session_id=session_id,
        )
    except Exception as e:
        return {"error": str(e)}
