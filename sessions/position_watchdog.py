from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytz

from core.agent_config import is_trading_day, load_agent_config
from core.params import load_params
from core.protection import check_protection_status
from sessions.intraday import (
    _POLL_END,
    _POLL_START,
    _sync_positions,
    get_premarket_session_id,
)

_ET = pytz.timezone("America/New_York")


def _execute_pending_trades(premarket_session_id: str, trail_pct: float) -> None:
    """Execute any premarket trades deferred until after market open volatility settles."""
    from core.db import get_client
    rows = (
        get_client()
        .table("ag_sessions")
        .select("metadata")
        .eq("id", premarket_session_id)
        .limit(1)
        .execute()
        .data
    ) or []
    meta    = (rows[0].get("metadata") or {}) if rows else {}
    pending = meta.get("pending_trades") or []
    if not pending:
        return
    print(f"[watchdog] Executing {len(pending)} deferred premarket trade(s)...")
    from sessions.premarket import _execute_trades
    _execute_trades(pending, premarket_session_id, trail_pct)
    meta.pop("pending_trades", None)
    get_client().table("ag_sessions").update(
        {"metadata": meta}
    ).eq("id", premarket_session_id).execute()


def main() -> None:
    now_et  = datetime.now(_ET)
    weekday = now_et.strftime("%a").upper()[:3]
    now_t   = now_et.time()

    if not is_trading_day(weekday):
        print(f"[watchdog] Not a trading day ({weekday}). Exiting.")
        return

    if not (_POLL_START <= now_t <= _POLL_END):
        print(f"[watchdog] Outside poll window ({now_t}). Exiting.")
        return

    premarket_session_id = get_premarket_session_id()
    if not premarket_session_id:
        print("[watchdog] No premarket session today. Exiting.")
        return

    protection = check_protection_status()
    if protection.suspended:
        print(f"[watchdog] Protection suspended: {protection.reason}")
        return

    params = load_params()

    # Execute any premarket trades deferred until 9:45 AM (opening volatility settled)
    if now_t >= time(9, 45):
        _execute_pending_trades(premarket_session_id, params.trail_pct)

    _sync_positions(premarket_session_id, params.trail_pct)
    print("[watchdog] Position sync complete.")


if __name__ == "__main__":
    main()
