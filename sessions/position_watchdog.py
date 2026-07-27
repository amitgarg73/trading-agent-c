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
    from core import run_state
    pending = run_state.get_pending_trades(premarket_session_id)
    if not pending:
        return
    print(f"[watchdog] Executing {len(pending)} deferred premarket trade(s)...")
    from sessions.premarket import _execute_trades
    _execute_trades(pending, premarket_session_id, trail_pct)
    run_state.clear_pending_trades(premarket_session_id)


def _reconcile_opening_orders(trail_pct: float) -> int:
    """
    Post-open reconcile for opening-auction (OPG) entries. For each 'pending_open' position, read the
    real open fill, backfill entry_price, attach the trailing stop, and flip status to 'open'. Runs
    only in opening-entry mode. Idempotent: an unfilled order is retried next cycle; an already-open
    position is not re-touched. Returns the count reconciled.
    """
    from core.db import get_client
    from core.alpaca import get_bracket_status, submit_trailing_stop, get_day_open
    client = get_client()
    rows = (
        client.table("c_positions").select("*")
        .eq("status", "pending_open").eq("open_date", date.today().isoformat())
        .execute().data
    ) or []
    reconciled = 0
    for pos in rows:
        oid = pos.get("alpaca_order_id")
        if not oid:
            continue
        fill = get_bracket_status(oid).get("entry_price")
        if not fill:
            print(f"[watchdog] {pos['ticker']} opening order not filled yet — retry next cycle.")
            continue
        trail_id = submit_trailing_stop(pos["ticker"], pos["shares"], trail_pct)
        client.table("c_positions").update(
            {"entry_price": fill, "status": "open", "trail_order_id": trail_id}
        ).eq("id", pos["id"]).execute()
        op = get_day_open(pos["ticker"])
        basis = f" ({(fill / op - 1) * 100:+.2f}% vs open)" if op else ""
        print(f"[watchdog] {pos['ticker']} opening fill ${fill:.2f}{basis} — position open, trail attached.")
        reconciled += 1
    return reconciled


def _maybe_opening_fallback(now_t: time, params) -> bool:
    """
    Near-open recovery. When premarket produced NO session today (it failed or did not run) and we are
    still in the 09:30-09:45 ET window, run the funnel once and enter at market so a premarket miss does
    not cost the whole day. Hard window gate: it can never become a mid-morning chase. Only fires in
    opening-entry mode (caller gates on the flag). Creates the day's session, so it runs at most once.
    Returns True if it ran.
    """
    if not (time(9, 30) <= now_t <= time(9, 45)):
        return False
    if get_premarket_session_id():
        return False  # premarket ran (whatever it decided) — do not second-guess it
    from uuid import uuid4
    from trace.logger import TraceLogger
    from agents.orchestrator import run_premarket_pipeline
    from sessions.premarket import _execute_opening_orders
    print("[watchdog] Premarket missing — near-open fallback: funnel + market entry (9:30-9:45 window).")
    session_id = str(uuid4())
    tracer = TraceLogger(session_id, session_type="premarket")
    try:
        result = run_premarket_pipeline(tracer, params)
        result.pop("_v2_market_report", {})
        trades = result.get("trades", [])
        n = _execute_opening_orders(trades, session_id, tracer=tracer, on_open=False) if trades else 0
        tracer.close_session(
            terminal_reason="opening_fallback" if n else result["session_meta"]["terminal_reason"],
            trades_proposed=len(trades), trades_executed=n,
            result_summary=f"Near-open fallback: {n} market entry(ies).",
        )
        print(f"[watchdog] Fallback placed {n} near-open market entry(ies).")
    except Exception as e:
        tracer.close_session(terminal_reason="error", result_summary=f"fallback error: {e}")
        print(f"[watchdog] fallback error: {e}")
    return True


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

    protection = check_protection_status()
    if protection.suspended:
        print(f"[watchdog] Protection suspended: {protection.reason}")
        return

    params = load_params()
    from sessions.premarket import _opening_entry_enabled
    opening = _opening_entry_enabled()

    premarket_session_id = get_premarket_session_id()
    if not premarket_session_id:
        # Opening-entry mode: try the near-open fallback (it creates the day's session on success).
        if opening and _maybe_opening_fallback(now_t, params):
            premarket_session_id = get_premarket_session_id()
        if not premarket_session_id:
            print("[watchdog] No premarket session today. Exiting.")
            return

    if opening:
        # Opening-entry mode: backfill OPG/fallback fills + attach trailing stops. No deferred chase.
        n = _reconcile_opening_orders(params.trail_pct)
        if n:
            print(f"[watchdog] Reconciled {n} opening position(s).")
    elif now_t >= time(9, 45):
        # Old path: execute premarket trades deferred until 9:45 AM (opening volatility settled).
        _execute_pending_trades(premarket_session_id, params.trail_pct)

    _sync_positions(premarket_session_id, params.trail_pct)
    print("[watchdog] Position sync complete.")


if __name__ == "__main__":
    main()
