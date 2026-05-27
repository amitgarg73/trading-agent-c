from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

ET = pytz.timezone("America/New_York")

# Thresholds for tiers 1-5 are dollar/pct values.
# Tier 6 is a count (consecutive losing days).
TIER_THRESHOLDS = {
    1: -200.0,   # soft floor — reduce new entries to 1
    2: -500.0,   # hard stop day
    3: -1000.0,  # rolling 3-day — halve max_positions for 2 days
    4: -0.10,    # 10% account drawdown — suspend 24h
    5: -0.20,    # 20% account drawdown — suspend 7 days, human unlock
    6: 5,        # consecutive losing days — suspend, human review
}


def _get_total_capital() -> float:
    return float(os.getenv("TOTAL_CAPITAL", "50000"))


@dataclass
class ProtectionStatus:
    suspended: bool = False
    tier: int = 0           # 0 = no event; 1-6 = which tier triggered
    reason: str = ""
    action: str = ""
    resume_at: Optional[datetime] = None
    max_positions_override: Optional[int] = None  # set by tier 3


@dataclass
class ProtectionEvent:
    event_date: date
    tier: int
    trigger_field: str
    trigger_value: float
    threshold: float
    action: str
    description: str
    suspended_until: Optional[datetime] = None
    human_unlocked: bool = False


def check_protection_status() -> ProtectionStatus:
    """
    Check all protection tiers in priority order.
    Returns the most severe active ProtectionStatus.
    Called at the start of every session.
    """
    active = _check_active_suspension()
    if active:
        return active

    today = date.today()

    # Tier 2: today's hard stop
    daily_pnl = _get_daily_pnl(today)
    if daily_pnl <= TIER_THRESHOLDS[2]:
        event = ProtectionEvent(
            event_date=today,
            tier=2,
            trigger_field="daily_pnl",
            trigger_value=daily_pnl,
            threshold=TIER_THRESHOLDS[2],
            action="stopped_day",
            description=f"Daily P&L ${daily_pnl:.2f} reached hard stop ${TIER_THRESHOLDS[2]:.2f}",
        )
        record_protection_event(event)
        return ProtectionStatus(
            suspended=True,
            tier=2,
            reason=f"Daily loss limit hit: ${daily_pnl:.2f}",
            action="stopped_day",
        )

    # Tier 3: rolling 3-day (soft restriction, not a suspension)
    rolling_pnl = _get_rolling_pnl(days=3)
    if rolling_pnl <= TIER_THRESHOLDS[3]:
        current_max = _get_current_max_positions()
        reduced = max(1, current_max // 2)
        event = ProtectionEvent(
            event_date=today,
            tier=3,
            trigger_field="rolling_3day_pnl",
            trigger_value=rolling_pnl,
            threshold=TIER_THRESHOLDS[3],
            action="reduced_sizing",
            description=f"3-day rolling P&L ${rolling_pnl:.2f} — max_positions reduced to {reduced}",
        )
        record_protection_event(event)
        return ProtectionStatus(
            suspended=False,
            tier=3,
            reason=f"3-day rolling loss ${rolling_pnl:.2f}",
            action="reduced_sizing",
            max_positions_override=reduced,
        )

    # Tier 6: consecutive losing days
    consecutive = _get_consecutive_losing_days()
    if consecutive >= TIER_THRESHOLDS[6]:
        suspended_until = datetime.now(pytz.utc) + timedelta(days=7)
        event = ProtectionEvent(
            event_date=today,
            tier=6,
            trigger_field="consecutive_losing_days",
            trigger_value=float(consecutive),
            threshold=float(TIER_THRESHOLDS[6]),
            action="suspended_human_required",
            description=f"{consecutive} consecutive losing days — suspended, human review required",
            suspended_until=suspended_until,
        )
        record_protection_event(event)
        return ProtectionStatus(
            suspended=True,
            tier=6,
            reason=f"{consecutive} consecutive losing days",
            action="suspended_human_required",
            resume_at=suspended_until,
        )

    # Tiers 4 and 5: account drawdown (5 before 4 — more severe wins)
    drawdown_pct = _get_account_drawdown_pct()
    if drawdown_pct <= TIER_THRESHOLDS[5]:
        suspended_until = datetime.now(pytz.utc) + timedelta(days=7)
        event = ProtectionEvent(
            event_date=today,
            tier=5,
            trigger_field="account_drawdown_pct",
            trigger_value=drawdown_pct,
            threshold=TIER_THRESHOLDS[5],
            action="suspended_7days_human_required",
            description=f"Account drawdown {drawdown_pct:.1%} exceeds 20% limit",
            suspended_until=suspended_until,
        )
        record_protection_event(event)
        return ProtectionStatus(
            suspended=True,
            tier=5,
            reason=f"Account drawdown {drawdown_pct:.1%}",
            action="suspended_7days_human_required",
            resume_at=suspended_until,
        )

    if drawdown_pct <= TIER_THRESHOLDS[4]:
        suspended_until = datetime.now(pytz.utc) + timedelta(hours=24)
        event = ProtectionEvent(
            event_date=today,
            tier=4,
            trigger_field="account_drawdown_pct",
            trigger_value=drawdown_pct,
            threshold=TIER_THRESHOLDS[4],
            action="suspended_24h",
            description=f"Account drawdown {drawdown_pct:.1%} exceeds 10% limit",
            suspended_until=suspended_until,
        )
        record_protection_event(event)
        return ProtectionStatus(
            suspended=True,
            tier=4,
            reason=f"Account drawdown {drawdown_pct:.1%}",
            action="suspended_24h",
            resume_at=suspended_until,
        )

    return ProtectionStatus(suspended=False, tier=0, reason="", action="")


def check_tier1_soft_floor(daily_pnl: float) -> bool:
    """
    Returns True if the soft floor is hit (daily_pnl <= -$200).
    Does not create a suspension — the caller reduces max_new_entries to 1.
    """
    return daily_pnl <= TIER_THRESHOLDS[1]


def record_protection_event(event: ProtectionEvent) -> None:
    """
    Write a protection event to c_protection_events.
    Idempotent: skips if an event for the same date and tier already exists.
    """
    from core.db import get_client
    existing = (
        get_client()
        .table("c_protection_events")
        .select("id")
        .eq("event_date", event.event_date.isoformat())
        .eq("tier", event.tier)
        .execute()
    )
    if existing.data:
        return

    row: dict = {
        "event_date":    event.event_date.isoformat(),
        "tier":          event.tier,
        "trigger_field": event.trigger_field,
        "trigger_value": event.trigger_value,
        "threshold":     event.threshold,
        "action":        event.action,
        "description":   event.description,
        "human_unlocked": event.human_unlocked,
    }
    if event.suspended_until:
        row["suspended_until"] = event.suspended_until.isoformat()

    get_client().table("c_protection_events").insert(row).execute()


# ── Private helpers ────────────────────────────────────────────────────────────

def _check_active_suspension() -> Optional[ProtectionStatus]:
    """
    Query c_protection_events for any suspension that is still active
    (suspended_until in the future, not human-unlocked).
    Returns a ProtectionStatus if found, else None.
    """
    from core.db import get_client
    now_utc = datetime.utcnow().isoformat()
    result = (
        get_client()
        .table("c_protection_events")
        .select("tier,action,suspended_until")
        .gte("suspended_until", now_utc)
        .eq("human_unlocked", False)
        .order("tier", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    resume_at = datetime.fromisoformat(row["suspended_until"]) if row.get("suspended_until") else None
    return ProtectionStatus(
        suspended=True,
        tier=row["tier"],
        reason=f"Active suspension from tier {row['tier']} event",
        action=row["action"],
        resume_at=resume_at,
    )


def _get_daily_pnl(for_date: date) -> float:
    from core.db import get_client
    result = (
        get_client()
        .table("c_trades")
        .select("realized_pnl")
        .eq("date", for_date.isoformat())
        .eq("status", "closed")
        .execute()
    )
    return sum(float(row["realized_pnl"] or 0) for row in (result.data or []))


def _get_rolling_pnl(days: int) -> float:
    from core.db import get_client
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    result = (
        get_client()
        .table("c_daily_performance")
        .select("realized_pnl")
        .gte("date", start)
        .execute()
    )
    return sum(float(row["realized_pnl"] or 0) for row in (result.data or []))


def _get_account_drawdown_pct() -> float:
    from core.db import get_client
    result = (
        get_client()
        .table("c_daily_performance")
        .select("realized_pnl")
        .order("date", desc=False)
        .execute()
    )
    if not result.data:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    for row in result.data:
        cumulative += float(row["realized_pnl"] or 0)
        if cumulative > peak:
            peak = cumulative
    if peak <= 0:
        return 0.0
    total_capital = _get_total_capital()
    return (cumulative - peak) / (total_capital + peak)


def _get_consecutive_losing_days() -> int:
    from core.db import get_client
    result = (
        get_client()
        .table("c_daily_performance")
        .select("realized_pnl")
        .order("date", desc=True)
        .limit(10)
        .execute()
    )
    count = 0
    for row in (result.data or []):
        if float(row["realized_pnl"] or 0) < 0:
            count += 1
        else:
            break
    return count


def _get_current_max_positions() -> int:
    from core.params import load_params
    return load_params().max_positions
