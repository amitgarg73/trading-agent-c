from __future__ import annotations

import os
import time as _time
from datetime import date, datetime, time, timezone, timedelta
from typing import Optional
from uuid import uuid4

import pytz

from core.agent_config import get_config, is_trading_day, load_agent_config
from core.goals import evaluate_goals
from core.params import load_params
from core.protection import check_protection_status
from trace.logger import TraceLogger

_ET          = pytz.timezone("America/New_York")
_POLL_START  = time(9, 15)
_POLL_END    = time(15, 50)
_ENTRY_CLOSE = time(13, 0)

_SCAN_OUTCOMES = {"no_intraday_candidates", "intraday_all_rejected", "intraday_entries_placed"}


def get_premarket_session_id() -> Optional[str]:
    """Return today's premarket session_id from ag_sessions, or None."""
    from core.db import get_client
    workflow_id = os.environ.get("WORKFLOW_ID", "")
    rows = (
        get_client()
        .table("ag_sessions")
        .select("id")
        .eq("workflow_id", workflow_id)
        .eq("session_type", "premarket")
        .gte("started_at", date.today().isoformat())
        .order("started_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["id"] if rows else None


# Backwards-compatible alias — remove after all callers updated
get_today_session_id = get_premarket_session_id


def get_daily_pnl(session_id: str) -> float:
    """Sum realized P&L from today's closed positions."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("realized_pnl")
        .eq("session_id", session_id)
        .eq("status", "closed")
        .eq("close_date", date.today().isoformat())
        .execute()
        .data
    ) or []
    return sum(r.get("realized_pnl") or 0 for r in rows)


def count_open_positions(session_id: str) -> int:
    """Count open positions for today's session."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("id")
        .eq("session_id", session_id)
        .eq("status", "open")
        .eq("open_date", date.today().isoformat())
        .execute()
        .data
    ) or []
    return len(rows)


def get_today_tickers(session_id: str) -> set[str]:
    """Return all tickers entered today (any status) — prevents same-day re-entry."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("ticker")
        .eq("session_id", session_id)
        .eq("open_date", date.today().isoformat())
        .execute()
        .data
    ) or []
    return {r["ticker"] for r in rows if r.get("ticker")}


def get_last_entry_scan_time(premarket_session_id: str) -> Optional[datetime]:
    """Return UTC datetime of the last intraday scan decision, or None.

    Queries ag_sessions for all intraday sessions that are children of the
    premarket session, then finds the most recent scan-outcome decision in
    ag_traces across those sessions. This handles multiple intraday polls
    per day, each with its own session_id.
    """
    from core.db import get_client
    db = get_client()
    intraday_rows = (
        db.table("ag_sessions")
        .select("id")
        .eq("parent_session_id", premarket_session_id)
        .eq("session_type", "intraday")
        .execute()
        .data
    ) or []
    if not intraday_rows:
        return None
    intraday_ids = [r["id"] for r in intraday_rows]
    rows = (
        db.table("ag_traces")
        .select("created_at, outcome")
        .in_("session_id", intraday_ids)
        .eq("step_type", "decision")
        .order("created_at", desc=True)
        .execute()
        .data
    ) or []
    for row in rows:
        if row.get("outcome") in _SCAN_OUTCOMES:
            ts = row["created_at"]
            if isinstance(ts, str):
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
            return ts
    return None


def classify_exit(fill_details: dict) -> str:
    """Infer exit reason from Alpaca fill details."""
    order_type = fill_details.get("order_type", "")
    side       = fill_details.get("side", "")
    if "trailing" in order_type and side == "sell": return "NATIVE_TRAIL"
    if order_type == "limit"    and side == "sell": return "target"
    if order_type == "stop"     and side == "sell": return "stop"
    if order_type == "market"   and side == "sell": return "eod_forced"
    return "manual"


def _sync_positions(session_id: str, trail_pct: float) -> None:
    """
    Sync open positions from Alpaca into c_positions:
    - updates unrealized_pnl with live data
    - submits trailing stop for newly-filled entries that don't have one yet
    - marks positions closed if Alpaca no longer holds them (bracket or trail exit fired)
    """
    from core.alpaca import (
        get_open_alpaca_tickers, get_position_data, get_order_fill, get_bracket_status,
        submit_trailing_stop, _cancel_bracket_stop_leg, _dclient,
    )
    from core.db import get_client
    db = get_client()

    rows = (
        db.table("c_positions")
        .select("id,ticker,alpaca_order_id,trail_order_id,entry_price,shares,entry_time")
        .eq("session_id", session_id)
        .eq("status", "open")
        .eq("open_date", date.today().isoformat())
        .execute()
        .data
    ) or []

    if not rows:
        return

    alpaca_tickers = get_open_alpaca_tickers()

    for pos in rows:
        ticker         = pos["ticker"]
        order_id       = pos.get("alpaca_order_id")
        trail_order_id = pos.get("trail_order_id")
        shares         = int(pos.get("shares") or 0)

        if ticker in alpaca_tickers:
            get_position_data(ticker)  # keep for side-effects / future use
            update: dict = {}
            # Entry just filled (or still pending from open) — backfill actual fill price
            # and submit trailing stop. get_bracket_status is only called while trail is
            # absent; once trail_order_id is set, subsequent cycles skip this entirely.
            if not trail_order_id and order_id:
                bracket = get_bracket_status(order_id)
                if bracket.get("entry_filled") and bracket.get("entry_price"):
                    actual_entry = bracket["entry_price"]
                    if abs(actual_entry - float(pos.get("entry_price") or 0)) > 0.001:
                        update["entry_price"] = actual_entry
                    _cancel_bracket_stop_leg(order_id)
                    _time.sleep(2)  # wait for Alpaca to release bracket leg qty
                    new_trail_id = submit_trailing_stop(ticker, shares, trail_pct)
                    if new_trail_id:
                        update["trail_order_id"] = new_trail_id
                        print(f"  [intraday] {ticker} trailing stop submitted (post-fill): {new_trail_id[:8]}")
                    else:
                        print(f"  [intraday] ⚠️  Trail still pending for {ticker} — will retry next cycle")
            if update:
                db.table("c_positions").update(update).eq("id", pos["id"]).execute()
        else:
            # Not in Alpaca — check trail order first, then bracket
            fill_price, exit_reason = None, None
            if trail_order_id:
                fill_price, exit_reason = get_order_fill(trail_order_id)
            if fill_price is None and order_id:
                fill_price, exit_reason = get_order_fill(order_id)

            if fill_price is None:
                # Before declaring external_close, verify entry actually filled.
                # A pending limit order (never filled) lands here too — treat it as unfilled.
                if order_id:
                    from core.alpaca import cancel_order
                    bracket = get_bracket_status(order_id)
                    if not bracket.get("entry_filled"):
                        # Allow 30 minutes before cancelling — the next 15-min cycle would
                        # otherwise cancel a freshly-submitted order before it has a chance to fill.
                        entry_time_str = pos.get("entry_time")
                        if entry_time_str:
                            try:
                                entry_dt = datetime.fromisoformat(
                                    entry_time_str.replace("Z", "+00:00")
                                ).replace(tzinfo=timezone.utc)
                                age_mins = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
                                if age_mins < 30:
                                    print(f"  [intraday] {ticker} order pending {age_mins:.0f}m — waiting (cancel at 30m)")
                                    continue
                            except Exception:
                                pass
                        cancel_order(order_id)
                        db.table("c_positions").update({
                            "status":       "closed",
                            "exit_reason":  "unfilled",
                            "exit_price":   float(pos.get("entry_price") or 0),
                            "close_date":   date.today().isoformat(),
                            "close_time":   datetime.utcnow().isoformat(),
                            "realized_pnl": 0.0,
                        }).eq("id", pos["id"]).execute()
                        print(f"  [intraday] {ticker} limit order never filled — cancelled and marked unfilled")
                        continue
                # Entry was filled but position is gone with no fill record — use last trade price
                exit_reason = "external_close"
                try:
                    from alpaca.data.requests import StockLatestTradeRequest
                    trade = _dclient().get_stock_latest_trade(
                        StockLatestTradeRequest(symbol_or_symbols=[ticker])
                    )
                    fill_price = float(trade[ticker].price)
                except Exception:
                    fill_price = float(pos.get("entry_price") or 0)

            entry    = float(pos.get("entry_price") or 0)
            realized = round((fill_price - entry) * shares, 2)
            now_     = datetime.utcnow().isoformat()
            db.table("c_positions").update({
                "status":       "closed",
                "exit_reason":  exit_reason or "unknown",
                "exit_price":   fill_price,
                "close_date":   date.today().isoformat(),
                "close_time":   now_,
                "realized_pnl": realized,
            }).eq("id", pos["id"]).execute()
            print(f"  [intraday] {ticker} closed via {exit_reason} @ ${fill_price:.4f} P&L ${realized:+.2f}")


def _place_intraday_trades(
    proposals: dict,
    approved_tickers: set[str],
    session_id: str,
    trail_pct: float,
    today_tickers: set[str] | None = None,
) -> int:
    """Submit bracket orders to Alpaca and write confirmed positions to c_positions."""
    from core.alpaca import submit_bracket_order, submit_trailing_stop
    from core.db import get_client
    today = date.today().isoformat()
    now_  = datetime.utcnow().isoformat()
    already_entered = today_tickers or set()
    count = 0
    for p in proposals.get("proposals", []):
        ticker = p["ticker"]
        if ticker not in approved_tickers:
            continue
        if ticker in already_entered:
            print(f"  [intraday] {ticker} already entered today — skipping (hard gate)")
            continue
        already_entered.add(ticker)
        shares   = p.get("shares") or int(p["position_size"] / p["entry_price"])
        order_id, fill_price = submit_bracket_order(
            ticker=p["ticker"],
            shares=shares,
            entry_price=p["entry_price"],
            target_price=p["target_price"],
            stop_price=p["stop_loss"],
        )
        if order_id is None:
            print(f"  [intraday] {p['ticker']} order rejected — skipping")
            continue

        trail_order_id = None
        if fill_price is not None:
            trail_order_id = submit_trailing_stop(p["ticker"], shares, trail_pct)

        get_client().table("c_positions").insert({
            "session_id":      session_id,
            "ticker":          p["ticker"],
            "action":          "BUY",
            "entry_price":     fill_price or p["entry_price"],
            "target_price":    p["target_price"],
            "stop_loss":       p["stop_loss"],
            "position_size":   p["position_size"],
            "shares":          shares,
            "confidence":      p["confidence"],
            "status":          "open",
            "open_date":       today,
            "entry_time":      now_,
            "entry_context":   "intraday",
            "alpaca_order_id": order_id,
            "trail_order_id":  trail_order_id,
        }).execute()
        count += 1
    return count


def main() -> None:
    from agents.research_agent import run_research_agent
    from agents.risk_agent import run_risk_agent

    now_et  = datetime.now(_ET)
    weekday = now_et.strftime("%a").upper()[:3]
    now_t   = now_et.time()

    if not is_trading_day(weekday):
        print(f"[intraday] Not a trading day ({weekday}). Exiting.")
        return

    if not (_POLL_START <= now_t <= _POLL_END):
        print(f"[intraday] Outside poll window ({now_t}). Exiting.")
        return

    # premarket_session_id is the day-level data key: used for all c_positions
    # reads/writes, PnL, capacity, and deferred trade metadata. It never changes
    # within a trading day. Each intraday poll gets its own session_id for traces.
    premarket_session_id = get_premarket_session_id()
    if not premarket_session_id:
        _PREMARKET_DISPATCH_END = time(10, 30)
        if now_t <= _PREMARKET_DISPATCH_END:
            print(f"[intraday] No session yet at {now_et.strftime('%H:%M ET')} — running premarket pipeline")
            from sessions.premarket import main as _premarket_main
            _premarket_main()
            premarket_session_id = get_premarket_session_id()
        if not premarket_session_id:
            print("[intraday] No premarket session today. Exiting.")
            return

    protection = check_protection_status()
    if protection.suspended:
        print(f"[intraday] Protection suspended: {protection.reason}")
        return

    config = load_agent_config()
    params = load_params()

    # Each poll is its own trace session, linked to premarket via parent_session_id.
    intraday_session_id = str(uuid4())
    tracer = TraceLogger(
        intraday_session_id,
        session_type="intraday",
        parent_session_id=premarket_session_id,
    )

    # Execute any premarket trades that were deferred — wait for 9:45 AM so the
    # first 15 minutes of open volatility settle before we enter.
    if now_t >= time(9, 45):
        from core.db import get_client as _get_client
        _rows = _get_client().table("ag_sessions").select("metadata") \
            .eq("id", premarket_session_id).limit(1).execute().data or []
        _meta    = (_rows[0].get("metadata") or {}) if _rows else {}
        _pending = _meta.get("pending_trades") or []
        if _pending:
            print(f"[intraday] Executing {len(_pending)} deferred premarket trade(s)...")
            from sessions.premarket import _execute_trades
            _execute_trades(_pending, premarket_session_id, params.trail_pct)
            _meta.pop("pending_trades", None)
            _get_client().table("ag_sessions").update(
                {"metadata": _meta}
            ).eq("id", premarket_session_id).execute()

    _sync_positions(premarket_session_id, params.trail_pct)

    daily_pnl   = get_daily_pnl(premarket_session_id)
    goal_status = evaluate_goals(daily_pnl)

    if goal_status.lock_in_mode:
        tracer.log_decision("orchestrator", "lock_in_mode",
                            detail={"daily_pnl": daily_pnl, "target": goal_status.daily_target})
        tracer.close_session("lock_in_mode", result_summary=f"P&L {daily_pnl:.2f} — lock-in active")
        print(f"[intraday] Lock-in mode active. P&L {daily_pnl:.2f}.")
        return

    if goal_status.pnl_floor_hit:
        tracer.log_decision("orchestrator", "pnl_floor_hit", detail={"daily_pnl": daily_pnl})
        tracer.close_session("pnl_floor_hit", result_summary=f"P&L floor hit ({daily_pnl:.2f})")
        print(f"[intraday] P&L floor hit ({daily_pnl:.2f}). No new entries.")
        return

    if not config.get("enable_intraday_entries", True):
        tracer.log_decision("orchestrator", "normal_no_new_entries",
                            detail={"daily_pnl": daily_pnl})
        tracer.close_session("entries_disabled", result_summary="Intraday entries disabled in config")
        print(f"[intraday] Poll complete. P&L {daily_pnl:.2f}. Entries disabled.")
        return

    min_interval = config.get("intraday_entry_min_interval_mins", 55)
    last_scan = get_last_entry_scan_time(premarket_session_id)
    if last_scan is not None:
        elapsed_mins = (datetime.utcnow() - last_scan).total_seconds() / 60
        if elapsed_mins < min_interval:
            tracer.log_decision("orchestrator", "entry_scan_too_recent",
                                detail={"elapsed_mins": round(elapsed_mins, 1), "min_interval": min_interval})
            tracer.close_session(
                "scan_too_recent",
                result_summary=f"Last scan {elapsed_mins:.0f}m ago (min {min_interval}m)",
            )
            print(f"[intraday] Entry scan too recent ({elapsed_mins:.0f}m ago, min {min_interval}m). Skipping.")
            return

    if now_t >= _ENTRY_CLOSE:
        tracer.log_decision("orchestrator", "past_entry_window", detail={"time": str(now_t)})
        tracer.close_session("past_entry_window", result_summary="Past entry window cutoff")
        print(f"[intraday] Past entry window. No new entries.")
        return

    open_count = count_open_positions(premarket_session_id)
    available  = params.max_positions - open_count
    if available <= 0:
        tracer.log_decision("orchestrator", "no_capacity",
                            detail={"open": open_count, "max": params.max_positions})
        tracer.close_session("no_capacity", result_summary=f"Full ({open_count}/{params.max_positions})")
        print(f"[intraday] No capacity ({open_count}/{params.max_positions}).")
        return

    min_score_bonus = config.get("intraday_min_score_bonus", 1)
    today_tickers   = get_today_tickers(premarket_session_id)
    max_new         = min(2, available, config.get("intraday_max_new_positions", 2))

    synthetic_report = {
        "decision": "GO",
        "max_positions": max_new,
        "bias": "NEUTRAL",
        "skip_reason": None,
        "summary": (
            f"Intraday scan {now_et.strftime('%H:%M')} ET. "
            f"Score >={params.strategy_min_score + min_score_bonus}. "
            f"Avoid (already entered today): {sorted(today_tickers)}."
        ),
    }

    try:
        from agents.orchestrator import _run_semantic_evals
        proposals = run_research_agent(tracer, synthetic_report, params)
        if not proposals.get("proposals"):
            tracer.log_decision("orchestrator", "no_intraday_candidates")
            tracer.close_session("no_intraday_candidates", result_summary="No candidates from research agent")
            print("[intraday] No candidates found.")
            return

        verdicts = run_risk_agent(tracer, proposals, params)
        # Evals belong to this intraday session, not the premarket session.
        _run_semantic_evals(intraday_session_id, {}, {}, proposals, verdicts, {})

        approved = [v for v in verdicts.get("verdicts", []) if v.get("verdict") == "APPROVED"]
        if not approved:
            tracer.log_decision("orchestrator", "intraday_all_rejected")
            tracer.close_session("intraday_all_rejected", result_summary="All proposals rejected by risk")
            print("[intraday] All proposals rejected.")
            return

        # Positions are written under premarket_session_id — the day-level data key.
        count = _place_intraday_trades(
            proposals, {v["ticker"] for v in approved}, premarket_session_id, params.trail_pct,
            today_tickers=today_tickers,
        )
        tracer.log_decision("orchestrator", "intraday_entries_placed", detail={"count": count})
        tracer.close_session(
            "intraday_entries_placed",
            trades_proposed=len(proposals.get("proposals", [])),
            trades_executed=count,
            result_summary=f"{count} trade(s): {', '.join(v['ticker'] for v in approved)}",
        )
        print(f"[intraday] {count} trade(s) placed: "
              f"{', '.join(v['ticker'] for v in approved)}")

    except Exception as e:
        tracer.log_error("orchestrator", f"intraday error: {e}")
        tracer.close_session("error", result_summary=f"Error: {e}")
        print(f"[intraday] Error: {e}")
        raise


if __name__ == "__main__":
    main()
