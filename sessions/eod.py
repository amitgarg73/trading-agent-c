from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pytz

from core.agent_config import is_trading_day, load_agent_config
from core.alerts import send_alert
from core.goals import evaluate_goals, record_goal_snapshots, update_goal_progress
from core.params import load_params
from core.protection import check_protection_status, record_protection_event
from trace.logger import TraceLogger

_ET = pytz.timezone("America/New_York")


@dataclass
class DailyPerformance:
    session_id:      str
    date:            str
    realized_pnl:    float
    trades_total:    int
    trades_won:      int
    trades_lost:     int
    win_rate:        float
    largest_win:     float
    largest_loss:    float
    avg_hold_min:    float
    vix_at_open:     Optional[float] = None
    market_signal:   Optional[str]   = None
    protection_tier: int = 0


def get_today_session_id() -> Optional[str]:
    """Return today's premarket session_id from c_sessions, or None."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_sessions")
        .select("id")
        .eq("date", date.today().isoformat())
        .order("started_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["id"] if rows else None


def get_today_trades(session_id: str) -> list[dict]:
    """Fetch all closed positions for today's session."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("ticker,realized_pnl,entry_time,close_time,exit_reason,position_size")
        .eq("session_id", session_id)
        .eq("status", "closed")
        .eq("close_date", date.today().isoformat())
        .execute()
        .data
    )
    return rows or []


def get_open_positions(session_id: str) -> list[dict]:
    """Fetch open positions for today's session."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("id,ticker,shares,entry_price,entry_time,alpaca_order_id")
        .eq("session_id", session_id)
        .eq("status", "open")
        .eq("open_date", date.today().isoformat())
        .execute()
        .data
    )
    return rows or []


def force_close_positions(session_id: str) -> int:
    """
    Cancel open bracket orders, then market-close any remaining positions via Alpaca.
    Updates c_positions with actual fill prices and realized P&L.
    Returns count of positions force-closed.
    """
    from core.alpaca import cancel_all_orders, close_all_strategy_positions
    from core.db import get_client
    positions = get_open_positions(session_id)
    if not positions:
        return 0

    cancel_all_orders()
    closed = close_all_strategy_positions()
    fills  = {r["ticker"]: r.get("fill_price") for r in closed}

    today  = date.today().isoformat()
    now_   = datetime.utcnow().isoformat()
    client = get_client()
    for pos in positions:
        fill_price  = fills.get(pos["ticker"])
        realized    = None
        if fill_price:
            entry = pos.get("entry_price") or 0.0
            realized = round((fill_price - float(entry)) * int(pos["shares"]), 2)
        client.table("c_positions").update({
            "status":       "closed",
            "exit_reason":  "eod_forced",
            "close_date":   today,
            "close_time":   now_,
            "realized_pnl": realized or 0.0,
        }).eq("id", pos["id"]).execute()
    return len(positions)


def reconcile_positions(session_id: str) -> dict:
    """
    Reconcile open DB positions against Alpaca bracket fills.
    - Backfills actual entry_price where the pre-market submission returned fill=None.
    - Closes positions whose bracket exit leg has already settled between intraday polls.
    Returns {"entry_updated": int, "exits_synced": int, "errors": int}.
    """
    from core.alpaca import get_bracket_status
    from core.db import get_client

    positions = get_open_positions(session_id)
    if not positions:
        return {"entry_updated": 0, "exits_synced": 0, "errors": 0}

    today         = date.today().isoformat()
    now_          = datetime.utcnow().isoformat()
    client        = get_client()
    entry_updated = exits_synced = errors = 0

    for pos in positions:
        order_id = pos.get("alpaca_order_id")
        if not order_id:
            continue

        status = get_bracket_status(order_id)
        if "error" in status:
            print(f"  [reconcile] {pos['ticker']} bracket_status error: {status['error']}")
            errors += 1
            continue

        updates: dict = {}

        if status["entry_filled"] and status["entry_price"]:
            stored = float(pos.get("entry_price") or 0)
            actual = status["entry_price"]
            if abs(actual - stored) > 0.001:
                updates["entry_price"] = actual
                entry_updated += 1
                print(f"  [reconcile] {pos['ticker']} entry_price {stored} → {actual}")

        if status["exit_filled"] and status["exit_price"]:
            effective_entry = updates.get("entry_price") or float(pos.get("entry_price") or 0)
            shares          = int(pos.get("shares") or 0)
            realized        = round(
                (status["exit_price"] - effective_entry) * shares, 2
            ) if effective_entry and shares else 0.0
            updates.update({
                "status":       "closed",
                "exit_reason":  status["exit_reason"] or "bracket_exit_detected",
                "close_date":   today,
                "close_time":   now_,
                "realized_pnl": realized,
            })
            exits_synced += 1
            print(f"  [reconcile] {pos['ticker']} exit synced: "
                  f"{status['exit_reason']} @ ${status['exit_price']}, P&L ${realized:+.2f}")

        if updates:
            client.table("c_positions").update(updates).eq("id", pos["id"]).execute()

    return {"entry_updated": entry_updated, "exits_synced": exits_synced, "errors": errors}


def compute_performance(session_id: str, trades: list[dict]) -> DailyPerformance:
    """Compute daily performance metrics from closed trades."""
    pnl_values = [t.get("realized_pnl") or 0.0 for t in trades]
    won        = [p for p in pnl_values if p > 0]
    lost       = [p for p in pnl_values if p <= 0]

    avg_hold = 0.0
    hold_mins = []
    for t in trades:
        try:
            entry = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
            close = datetime.fromisoformat(t["close_time"].replace("Z", "+00:00"))
            hold_mins.append((close - entry).total_seconds() / 60)
        except Exception:
            pass
    if hold_mins:
        avg_hold = sum(hold_mins) / len(hold_mins)

    total = len(trades)
    return DailyPerformance(
        session_id    = session_id,
        date          = date.today().isoformat(),
        realized_pnl  = round(sum(pnl_values), 2),
        trades_total  = total,
        trades_won    = len(won),
        trades_lost   = len(lost),
        win_rate      = round(len(won) / total, 4) if total else 0.0,
        largest_win   = round(max(won),  2) if won  else 0.0,
        largest_loss  = round(min(lost), 2) if lost else 0.0,
        avg_hold_min  = round(avg_hold, 1),
    )


def save_performance(perf: DailyPerformance) -> None:
    from core.db import get_client
    get_client().table("c_daily_performance").upsert({
        "session_id":    perf.session_id,
        "date":          perf.date,
        "realized_pnl":  perf.realized_pnl,
        "trades_total":  perf.trades_total,
        "trades_won":    perf.trades_won,
        "trades_lost":   perf.trades_lost,
        "win_rate":      perf.win_rate,
        "largest_win":   perf.largest_win,
        "largest_loss":  perf.largest_loss,
        "avg_hold_min":  perf.avg_hold_min,
        "protection_tier": perf.protection_tier,
    }).execute()


def build_daily_summary(
    perf: DailyPerformance,
    trades: list[dict],
    learnings: Optional[dict],
) -> str:
    sign = "+" if perf.realized_pnl >= 0 else ""
    lines = [
        f"P&L: {sign}${perf.realized_pnl:.2f}  |  "
        f"Trades: {perf.trades_won}W / {perf.trades_lost}L  |  "
        f"Win rate: {perf.win_rate * 100:.0f}%",
        "",
    ]
    winners = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    losers  = [t for t in trades if (t.get("realized_pnl") or 0) <= 0]
    if winners:
        lines.append("Winners:")
        for t in winners:
            lines.append(f"  {t['ticker']:6}  +${t.get('realized_pnl', 0):.2f}")
    if losers:
        lines.append("Losers:")
        for t in losers:
            lines.append(f"  {t['ticker']:6}   ${t.get('realized_pnl', 0):.2f}")
    if learnings:
        lines += [
            "",
            "Learning Agent:",
            f"  Wrote {learnings.get('learnings_written', 0)} observation(s), "
            f"{learnings.get('params_adjusted', 0)} adjustment(s).",
            f"  {learnings.get('context_for_tomorrow', '')}",
        ]
    return "\n".join(lines)


def main() -> None:
    from agents.learning_agent import run_learning_agent

    now_et  = datetime.now(_ET)
    weekday = now_et.strftime("%a").upper()[:3]

    if not is_trading_day(weekday):
        print(f"[eod] Not a trading day ({weekday}). Exiting.")
        return

    session_id = get_today_session_id()
    if not session_id:
        print("[eod] No premarket session today. Exiting.")
        return

    config = load_agent_config()
    params = load_params()
    tracer = TraceLogger(session_id)
    print(f"[eod] Session {session_id} — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    # Force-close any remaining open positions
    n_forced = force_close_positions(session_id)
    tracer.log_decision(
        "orchestrator",
        "forced_close",
        detail={"count": n_forced} if n_forced else {"status": "nothing_open"},
    )
    if n_forced:
        print(f"[eod] Force-closed {n_forced} position(s).")

    recon = reconcile_positions(session_id)
    tracer.log_decision("orchestrator", "reconcile_complete", detail=recon)
    if recon["exits_synced"] or recon["entry_updated"]:
        print(f"[eod] Reconciled: {recon['entry_updated']} entry fill(s), "
              f"{recon['exits_synced']} bracket exit(s).")

    # Daily performance
    trades = get_today_trades(session_id)
    perf   = compute_performance(session_id, trades)
    save_performance(perf)
    tracer.log_decision("orchestrator", "performance_saved",
                        detail={"pnl": perf.realized_pnl, "trades": perf.trades_total})

    # Principal protection check
    protection = check_protection_status()
    if protection.tier and protection.tier > 0:
        record_protection_event(protection)
        tracer.log_decision("orchestrator", f"tier_{protection.tier}_triggered",
                            detail={"reason": protection.reason})
        if protection.tier >= 4:
            send_alert(
                f"[ALERT] Strategy C — Protection Tier {protection.tier}",
                f"Action: {protection.action}\nReason: {protection.reason}",
            )

    # Update goals with today's final P&L
    update_goal_progress(perf.realized_pnl)
    record_goal_snapshots(date.today(), perf.realized_pnl)
    tracer.log_decision("orchestrator", "goals_updated",
                        detail={"pnl": perf.realized_pnl})

    # Learning Agent
    learnings = None
    if config.get("enable_learning_agent", True) and perf.trades_total > 0:
        try:
            learnings = run_learning_agent(tracer, session_id, params)
            tracer.log_decision("orchestrator", "learning_completed",
                                detail={
                                    "learnings_written": learnings.get("learnings_written", 0),
                                    "params_adjusted":   learnings.get("params_adjusted", 0),
                                })
            if learnings.get("params_adjusted", 0) > 0:
                send_alert(
                    "Strategy C — Params Adjusted",
                    f"{learnings['params_adjusted']} param(s) changed.\n"
                    f"{learnings.get('top_finding', '')}",
                )
            if learnings.get("goal_recommended"):
                send_alert("Strategy C — Goal Recommended",
                           "New goal recommendation — review in c_goals.")
        except Exception as e:
            tracer.log_error("learning", f"Learning Agent failed: {e}")
            print(f"[eod] Learning Agent error (continuing): {e}")
    else:
        reason = "no_trades" if perf.trades_total == 0 else "disabled"
        tracer.log_decision("orchestrator", "learning_skipped", detail={"reason": reason})

    # Daily summary alert
    pnl_sign    = "+" if perf.realized_pnl >= 0 else ""
    alert_prefix = "[ALERT] " if perf.protection_tier >= 3 else ""
    subject = (
        f"{alert_prefix}Strategy C — EOD {now_et.strftime('%Y-%m-%d')} "
        f"({pnl_sign}${perf.realized_pnl:.2f})"
    )
    send_alert(subject, build_daily_summary(perf, trades, learnings))

    # Finalize session
    tracer.close_session(
        terminal_reason="eod_complete",
        trades_executed=perf.trades_total,
    )
    tracer.log_decision("orchestrator", "eod_complete",
                        detail={"pnl": perf.realized_pnl, "trades": perf.trades_total})
    print(f"[eod] Complete. P&L=${pnl_sign}{perf.realized_pnl:.2f}, "
          f"{perf.trades_total} trade(s).")


if __name__ == "__main__":
    main()
