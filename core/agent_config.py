from __future__ import annotations

from typing import Any

# Keys that must be in TRADING_DAYS to run
VALID_TRADING_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}

_DEFAULTS: dict[str, Any] = {
    "trading_days":               ["MON", "TUE", "WED", "THU", "FRI"],
    "enable_intraday_entries":    False,
    "intraday_min_score_bonus":   1,
    "intraday_max_new_positions": 2,
    "intraday_entry_window_end":  "13:00",
    "enable_learning_agent":      True,
    "enable_news_analyst":        True,
    "auto_approve_goal_recs":     False,
    "phase":                      "simulation",
}


def load_agent_config() -> dict[str, Any]:
    """
    Load all active config rows from c_agent_config.
    Returns a flat dict: config_key -> Python value (JSONB already parsed by Supabase).
    Falls back to _DEFAULTS for any key not in the DB.

    This is the first statement every session runs, so a blip here kills the whole session before
    it does anything (2026-07-24: a Cloudflare 525 in front of Supabase took out premarket). The
    read is therefore retried on transient infrastructure errors.

    It does NOT fall back to _DEFAULTS when the database is unreachable, and that is deliberate.
    The defaults describe a suspended system (phase=simulation, intraday entries off, standard
    trading days and windows). Substituting them on a network error would silently run a live
    trading day on the wrong configuration every time Supabase hiccups. Failing loudly is correct
    here: the fallback covers a key missing from a SUCCESSFUL read, nothing more.
    """
    from core.db import execute_with_retry, get_client
    result = execute_with_retry(
        get_client()
        .table("c_agent_config")
        .select("config_key,config_value")
        .eq("is_active", True),
        description="c_agent_config read",
    )
    config = dict(_DEFAULTS)
    for row in (result.data or []):
        config[row["config_key"]] = row["config_value"]
    return config


def get_config(key: str, default: Any = None) -> Any:
    """Get a single config value by key. Falls back to _DEFAULTS, then `default`."""
    config = load_agent_config()
    return config.get(key, default)


def is_trading_day(weekday_abbr: str) -> bool:
    """
    Check if the given 3-letter weekday abbreviation (e.g. 'MON') is a configured trading day.
    """
    trading_days = get_config("trading_days", _DEFAULTS["trading_days"])
    return weekday_abbr.upper() in [d.upper() for d in trading_days]
