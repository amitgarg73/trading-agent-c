from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

PARAM_DEFAULTS: dict[str, float] = {
    "strategy_min_score":          5.0,
    "max_positions":               10.0,
    "partial_profit_pct":          0.005,
    "atr_multiplier":              0.8,
    "rr_ratio":                    2.0,
    "caution_position_multiplier": 0.6,
    "caution_min_score":           7.0,
    "max_sector_concentration":    0.35,
    "trail_pct":                   0.015,
    "max_entry_premium":           0.005,
}


@dataclass
class StrategyParams:
    """Live strategy parameters, loaded from c_strategy_params."""
    strategy_min_score: int = 5
    max_positions: int = 10
    partial_profit_pct: float = 0.005
    atr_multiplier: float = 0.8
    rr_ratio: float = 2.0
    caution_position_multiplier: float = 0.6
    caution_min_score: int = 7
    max_sector_concentration: float = 0.35
    trail_pct: float = 0.015
    max_entry_premium: float = 0.005  # skip an entry already > this fraction above the day's open (chase guard)


@dataclass
class ParamRow:
    """Full row from c_strategy_params, including bounds and cooldown metadata."""
    param_key: str
    param_value: float
    min_bound: float
    max_bound: float
    default_value: float
    cooldown_days: int
    cooldown_until: Optional[date]
    previous_value: Optional[float]


@dataclass
class AdjustResult:
    status: str                    # "applied" | "rejected"
    rejection_reason: Optional[str]  # "out_of_bounds" | "cooldown_active" | "not_found"
    old_value: Optional[float]
    new_value: Optional[float]
    cooldown_until: Optional[date]


def load_params() -> StrategyParams:
    """
    Load live strategy params from c_strategy_params.
    Falls back to PARAM_DEFAULTS for any missing rows.
    """
    from core.db import get_client
    result = get_client().table("c_strategy_params").select("param_key,param_value").execute()
    values: dict[str, float] = {
        row["param_key"]: row["param_value"]
        for row in (result.data or [])
    }
    return StrategyParams(
        strategy_min_score=int(values.get("strategy_min_score", PARAM_DEFAULTS["strategy_min_score"])),
        max_positions=int(values.get("max_positions", PARAM_DEFAULTS["max_positions"])),
        partial_profit_pct=values.get("partial_profit_pct", PARAM_DEFAULTS["partial_profit_pct"]),
        atr_multiplier=values.get("atr_multiplier", PARAM_DEFAULTS["atr_multiplier"]),
        rr_ratio=values.get("rr_ratio", PARAM_DEFAULTS["rr_ratio"]),
        caution_position_multiplier=values.get("caution_position_multiplier", PARAM_DEFAULTS["caution_position_multiplier"]),
        caution_min_score=int(values.get("caution_min_score", PARAM_DEFAULTS["caution_min_score"])),
        max_sector_concentration=values.get("max_sector_concentration", PARAM_DEFAULTS["max_sector_concentration"]),
        trail_pct=values.get("trail_pct", PARAM_DEFAULTS["trail_pct"]),
        max_entry_premium=values.get("max_entry_premium", PARAM_DEFAULTS["max_entry_premium"]),
    )


def get_param_row(param_key: str) -> Optional[ParamRow]:
    """Load a single param row with full metadata (bounds, cooldown)."""
    from core.db import get_client
    result = (
        get_client()
        .table("c_strategy_params")
        .select("*")
        .eq("param_key", param_key)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    cooldown_until = None
    if row.get("cooldown_until"):
        cooldown_until = date.fromisoformat(row["cooldown_until"])
    return ParamRow(
        param_key=row["param_key"],
        param_value=float(row["param_value"]),
        min_bound=float(row["min_bound"]),
        max_bound=float(row["max_bound"]),
        default_value=float(row["default_value"]),
        cooldown_days=int(row["cooldown_days"]),
        cooldown_until=cooldown_until,
        previous_value=float(row["previous_value"]) if row.get("previous_value") is not None else None,
    )


def adjust_param(
    param_key: str,
    new_value: float,
    reason: str,
    updated_by: str = "learning_agent",
) -> AdjustResult:
    """
    Update a strategy parameter value within its defined bounds and cooldown.
    Returns AdjustResult describing what happened.
    """
    from core.db import get_client

    row = get_param_row(param_key)
    if row is None:
        return AdjustResult(
            status="rejected",
            rejection_reason="not_found",
            old_value=None,
            new_value=None,
            cooldown_until=None,
        )

    if not (row.min_bound <= new_value <= row.max_bound):
        return AdjustResult(
            status="rejected",
            rejection_reason="out_of_bounds",
            old_value=row.param_value,
            new_value=new_value,
            cooldown_until=None,
        )

    if row.cooldown_until is not None and row.cooldown_until >= date.today():
        return AdjustResult(
            status="rejected",
            rejection_reason="cooldown_active",
            old_value=row.param_value,
            new_value=new_value,
            cooldown_until=row.cooldown_until,
        )

    new_cooldown = date.today() + timedelta(days=row.cooldown_days)
    (
        get_client()
        .table("c_strategy_params")
        .update({
            "param_value":    new_value,
            "previous_value": row.param_value,
            "cooldown_until": new_cooldown.isoformat(),
            "last_updated":   datetime.utcnow().isoformat(),
            "updated_by":     updated_by,
            "change_reason":  reason,
        })
        .eq("param_key", param_key)
        .execute()
    )

    return AdjustResult(
        status="applied",
        rejection_reason=None,
        old_value=row.param_value,
        new_value=new_value,
        cooldown_until=new_cooldown,
    )
