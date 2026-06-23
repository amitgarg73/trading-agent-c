from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional
from uuid import uuid4

# Learning-window default for new learnings (c_learnings.expires_date is NOT NULL).
_LEARNING_TTL_DAYS = 30


def _confidence_text(c: Any) -> str:
    """c_learnings.confidence is TEXT (low|medium|high). Accept a string or a 0-1 number."""
    if isinstance(c, str):
        return c if c in ("low", "medium", "high") else "medium"
    try:
        v = float(c)
    except (TypeError, ValueError):
        return "medium"
    return "high" if v >= 0.8 else "medium" if v >= 0.5 else "low"


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
    """Fetch the session row for market context. Sessions live in ag_sessions
    (TraceLogger migrated off c_sessions); market context is in the metadata JSONB,
    which we flatten so the agent sees it alongside the session fields."""
    try:
        from core.db import get_client
        rows = (
            get_client()
            .table("ag_sessions")
            .select("id,session_type,status,terminal_reason,result_summary,metadata,started_at,ended_at")
            .eq("id", session_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            return {"error": "session not found"}
        r = rows[0]
        meta = r.get("metadata") or {}
        return {**(meta if isinstance(meta, dict) else {}), **{k: v for k, v in r.items() if k != "metadata"}}
    except Exception as e:
        return {"error": str(e)}


def read_strategy_params() -> list[dict[str, Any]]:
    """Fetch all strategy params with current values and bounds. Returns agent-facing
    keys (param_name/current_value/min_value/max_value) mapped from the actual
    c_strategy_params columns (param_key/param_value/min_bound/max_bound)."""
    try:
        from core.db import get_client
        rows = (
            get_client()
            .table("c_strategy_params")
            .select("param_key,param_value,default_value,min_bound,max_bound,cooldown_until,updated_by")
            .execute()
            .data
        )
        return [
            {
                "param_name":       r.get("param_key"),
                "current_value":    r.get("param_value"),
                "default_value":    r.get("default_value"),
                "min_value":        r.get("min_bound"),
                "max_value":        r.get("max_bound"),
                "cooldown_until":   r.get("cooldown_until"),
                "last_adjusted_by": r.get("updated_by"),
            }
            for r in (rows or [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


def read_recent_learnings(days: int = 14) -> list[dict[str, Any]]:
    """Fetch learnings written in the past N days, newest first. Maps the actual
    c_learnings columns (session_date/param_key) to the agent-facing shape."""
    try:
        from core.db import get_client
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = (
            get_client()
            .table("c_learnings")
            .select("session_date,learning_type,dimension,finding,param_key,confidence,outcome")
            .gte("session_date", cutoff)
            .order("session_date", desc=True)
            .execute()
            .data
        )
        return [
            {
                "learning_date":  r.get("session_date"),
                "learning_type":  r.get("learning_type"),
                "dimension":      r.get("dimension"),
                "finding":        r.get("finding"),
                "param_adjusted": r.get("param_key"),
                "confidence":     r.get("confidence"),
                "outcome":        r.get("outcome"),
            }
            for r in (rows or [])
        ]
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
    confidence: Any = 0.0,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist a structured learning to c_learnings.
    learning_type: "observation" | "adjustment" | "false_positive_detected" | "goal_recommendation"
    dimension: "entry_quality" | "exit_quality" | "market_regime" | "ticker_pattern" | "parameter"

    Column-mapped to the actual c_learnings schema: session_date / param_key /
    old_param_value / new_param_value, confidence as TEXT, and the NOT NULL
    expires_date. sample_size has no column, so it is folded into the finding text.
    """
    try:
        from core.db import get_client
        finding_text = finding + (f" (n={sample_size})" if sample_size else "")
        row = {
            "id":               str(uuid4()),
            "session_id":       session_id,
            "session_date":     date.today().isoformat(),
            "learning_type":    learning_type,
            "dimension":        dimension,
            "finding":          finding_text,
            "confidence":       _confidence_text(confidence),
            "param_key":        param_adjusted,
            "old_param_value":  old_value,
            "new_param_value":  new_value,
            "expires_date":     (date.today() + timedelta(days=_LEARNING_TTL_DAYS)).isoformat(),
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
            confidence="medium",
            session_id=session_id,
        )
    except Exception as e:
        return {"error": str(e)}
