from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class GoalStatus:
    lock_in_mode: bool = False      # daily target hit — no new entries
    pnl_floor_hit: bool = False     # soft floor hit — extra caution
    daily_target: Optional[float] = None
    daily_floor: Optional[float] = None


def load_active_goals() -> list[dict]:
    """Load all active goals effective today from c_goals."""
    from core.db import get_client
    today = date.today().isoformat()
    result = (
        get_client()
        .table("c_goals")
        .select("id,goal_type,entity_id,target_value,current_value,status,effective_until")
        .eq("status", "active")
        .lte("effective_from", today)
        .execute()
    )
    rows = result.data or []
    # Filter out goals past their effective_until date
    return [
        r for r in rows
        if r.get("effective_until") is None or r["effective_until"] >= today
    ]


def evaluate_goals(daily_pnl: float) -> GoalStatus:
    """
    Evaluate current daily P&L against active goals.
    Read-only — no DB writes. Safe to call on every intraday poll.
    """
    goals = load_active_goals()
    status = GoalStatus()

    for goal in goals:
        gtype = goal["goal_type"]
        target = float(goal["target_value"])

        if gtype == "daily_pnl_target":
            status.daily_target = target
            if daily_pnl >= target:
                status.lock_in_mode = True

        elif gtype == "daily_pnl_floor":
            status.daily_floor = target
            if daily_pnl <= target:
                status.pnl_floor_hit = True

    return status


def update_goal_progress(daily_pnl: float) -> None:
    """
    Update current_value on all active daily P&L goals.
    Called once by EOD session after final P&L is known.
    """
    from core.db import get_client
    goals = load_active_goals()
    client = get_client()
    today = date.today().isoformat()

    for goal in goals:
        gtype = goal["goal_type"]
        if gtype not in ("daily_pnl_target", "daily_pnl_floor"):
            continue

        target = float(goal["target_value"])
        if gtype == "daily_pnl_target":
            new_status = "achieved" if daily_pnl >= target else "missed"
        else:
            new_status = "missed" if daily_pnl <= target else "active"

        (
            client
            .table("c_goals")
            .update({"current_value": daily_pnl, "status": new_status, "updated_at": today})
            .eq("id", goal["id"])
            .execute()
        )


def record_goal_snapshots(snapshot_date: date, daily_pnl: float) -> None:
    """
    Write a c_goal_snapshots row for each active daily P&L goal.
    Idempotent: upserts on (goal_id, snapshot_date).
    Called once by EOD session.
    """
    from core.db import get_client
    goals = load_active_goals()
    client = get_client()

    for goal in goals:
        gtype = goal["goal_type"]
        if gtype not in ("daily_pnl_target", "daily_pnl_floor"):
            continue

        target = float(goal["target_value"])
        if gtype == "daily_pnl_target":
            snap_status = "achieved" if daily_pnl >= target else "missed"
        else:
            snap_status = "missed" if daily_pnl <= target else "active"

        (
            client
            .table("c_goal_snapshots")
            .upsert({
                "goal_id":       goal["id"],
                "snapshot_date": snapshot_date.isoformat(),
                "value":         daily_pnl,
                "status":        snap_status,
            })
            .execute()
        )
