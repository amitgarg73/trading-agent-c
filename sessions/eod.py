from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pytz

from core.agent_config import is_trading_day, load_agent_config
from core.alerts import send_alert
from core.goals import evaluate_goals, record_goal_snapshots, update_goal_progress
from core.params import load_params
from core.protection import check_protection_status
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
    """Return today's premarket session_id from the agent's own run record, or None."""
    from core import run_state
    return run_state.today_premarket_run_id()


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
        .select("id,ticker,shares,entry_price,entry_time,alpaca_order_id,trail_order_id")
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

    P&L source priority:
      1. fill_price returned by close_position() poll (most accurate)
      2. unrealized_pnl snapshot from Alpaca taken before cancellation (fallback
         when close order hasn't filled yet — common when EOD runs at 3:55 PM)
      3. 0.0 (last resort — signals the position needs manual reconciliation)

    Returns count of positions force-closed.
    """
    from core.alpaca import cancel_all_orders, close_all_strategy_positions, get_position_data
    from core.db import get_client

    positions = get_open_positions(session_id)
    if not positions:
        return 0

    # Snapshot unrealized P&L and current price before cancelling orders (prices still valid)
    alpaca_pnl:   dict[str, float] = {}
    alpaca_price: dict[str, float] = {}
    for pos in positions:
        data = get_position_data(pos["ticker"])
        if data:
            if data.get("unrealized_pnl") is not None:
                alpaca_pnl[pos["ticker"]] = data["unrealized_pnl"]
            if data.get("current_price") is not None:
                alpaca_price[pos["ticker"]] = data["current_price"]

    cancel_all_orders()
    closed = close_all_strategy_positions()
    fills  = {r["ticker"]: r.get("fill_price") for r in closed}

    today  = date.today().isoformat()
    now_   = datetime.utcnow().isoformat()
    client = get_client()
    for pos in positions:
        try:
            fill_price = fills.get(pos["ticker"])
            if fill_price:
                entry            = pos.get("entry_price") or 0.0
                realized         = round((fill_price - float(entry)) * int(pos["shares"]), 2)
                exit_price_store = fill_price
                src              = "fill"
            elif pos["ticker"] in alpaca_pnl:
                realized         = alpaca_pnl[pos["ticker"]]
                exit_price_store = alpaca_price.get(pos["ticker"])
                src              = "alpaca_snapshot"
            else:
                realized         = 0.0
                exit_price_store = None
                src              = "unavailable"

            client.table("c_positions").update({
                "status":       "closed",
                "exit_reason":  "eod_forced",
                "exit_price":   exit_price_store,
                "close_date":   today,
                "close_time":   now_,
                "realized_pnl": realized,
            }).eq("id", pos["id"]).execute()
            print(f"  [eod] {pos['ticker']} realized P&L: ${realized:+.2f} (source: {src})")
        except Exception as e:
            print(f"  [eod] {pos['ticker']} DB update failed: {e}")

    return len(positions)


def reconcile_positions(session_id: str) -> dict:
    """
    Reconcile open DB positions against Alpaca fills — runs BEFORE force_close so that
    positions already exited via bracket or trailing stop are captured with correct
    exit_reason and exit_price rather than being force-closed with eod_forced.

    Steps per position:
      1. Backfill actual entry_price if bracket filled at a different price.
      2. Check bracket exit legs (take-profit / stop).
      3. If bracket hasn't exited, check standalone trailing stop (trail_order_id).

    Returns {"entry_updated": int, "exits_synced": int, "errors": int}.
    """
    from core.alpaca import get_bracket_status, get_order_fill
    from core.db import get_client

    positions = get_open_positions(session_id)
    if not positions:
        return {"entry_updated": 0, "exits_synced": 0, "errors": 0}

    today         = date.today().isoformat()
    now_          = datetime.utcnow().isoformat()
    client        = get_client()
    entry_updated = exits_synced = errors = 0

    for pos in positions:
        order_id       = pos.get("alpaca_order_id")
        trail_order_id = pos.get("trail_order_id")
        updates: dict  = {}

        if order_id:
            status = get_bracket_status(order_id)
            if "error" in status:
                print(f"  [reconcile] {pos['ticker']} bracket_status error: {status['error']}")
                errors += 1
                continue

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
                    "exit_reason":  status["exit_reason"] or "bracket_exit",
                    "exit_price":   status["exit_price"],
                    "close_date":   today,
                    "close_time":   now_,
                    "realized_pnl": realized,
                })
                exits_synced += 1
                print(f"  [reconcile] {pos['ticker']} bracket exit: "
                      f"{status['exit_reason']} @ ${status['exit_price']}, P&L ${realized:+.2f}")

        # Trailing stop check — only if position not already resolved above
        if updates.get("status") != "closed" and trail_order_id:
            fill_price, exit_reason = get_order_fill(trail_order_id)
            if fill_price is not None:
                effective_entry = updates.get("entry_price") or float(pos.get("entry_price") or 0)
                shares          = int(pos.get("shares") or 0)
                realized        = round(
                    (fill_price - effective_entry) * shares, 2
                ) if effective_entry and shares else 0.0
                updates.update({
                    "status":       "closed",
                    "exit_reason":  exit_reason or "NATIVE_TRAIL",
                    "exit_price":   fill_price,
                    "close_date":   today,
                    "close_time":   now_,
                    "realized_pnl": realized,
                })
                exits_synced += 1
                print(f"  [reconcile] {pos['ticker']} trail exit: "
                      f"{exit_reason} @ ${fill_price}, P&L ${realized:+.2f}")

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
    row: dict = {
        "session_id":      perf.session_id,
        "date":            perf.date,
        "realized_pnl":    perf.realized_pnl,
        "trades_total":    perf.trades_total,
        "trades_won":      perf.trades_won,
        "trades_lost":     perf.trades_lost,
        "win_rate":        perf.win_rate,
        "largest_win":     perf.largest_win,
        "largest_loss":    perf.largest_loss,
        "avg_hold_min":    perf.avg_hold_min,
        "protection_tier": perf.protection_tier,
    }
    if perf.vix_at_open is not None:
        row["vix_at_open"] = perf.vix_at_open
    if perf.market_signal is not None:
        row["market_signal"] = perf.market_signal
    get_client().table("c_daily_performance").upsert(row).execute()


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


def _opening_entry_report(trades: list[dict]) -> str:
    """Proof metric for the entry redesign (design/entry-redesign-premarket-open.md): average entry
    price vs the day's open across today's fills. In opening-entry mode every entry is an opening
    auction, so the basis should trend to ~0 (chasing ran ~+1.6%). Returns '' when the flag is off or
    no basis can be computed. Best-effort; never blocks the EOD alert."""
    try:
        from sessions.premarket import _opening_entry_enabled
        if not _opening_entry_enabled():
            return ""
        from core.alpaca import get_day_open
        bases = []
        for t in trades:
            ep = t.get("entry_price")
            if not ep:
                continue
            op = get_day_open(t["ticker"])
            if op:
                bases.append(float(ep) / op - 1.0)
        if not bases:
            return ""
        avg = sum(bases) / len(bases)
        return (f"\n\nEntry basis vs open: {avg * 100:+.2f}% across {len(bases)} fill(s)  "
                f"[target ~0; the old chase ran ~+1.6% above open]")
    except Exception as e:
        return f"\n\n(entry-basis report unavailable: {e})"


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
    tracer = TraceLogger(session_id, session_type="eod")
    print(f"[eod] Session {session_id} — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    # Reconcile first — catches positions that exited via bracket/trail between last poll and now
    recon = reconcile_positions(session_id)
    tracer.log_decision("orchestrator", "reconcile_complete", detail=recon)
    if recon["exits_synced"] or recon["entry_updated"]:
        print(f"[eod] Reconciled: {recon['entry_updated']} entry fill(s), "
              f"{recon['exits_synced']} exit(s).")

    # Force-close anything still open after reconcile
    n_forced = force_close_positions(session_id)
    tracer.log_decision(
        "orchestrator",
        "forced_close",
        detail={"count": n_forced} if n_forced else {"status": "nothing_open"},
    )
    if n_forced:
        print(f"[eod] Force-closed {n_forced} position(s).")

    # Daily performance
    trades = get_today_trades(session_id)
    perf   = compute_performance(session_id, trades)
    save_performance(perf)
    tracer.log_decision("orchestrator", "performance_saved",
                        detail={"pnl": perf.realized_pnl, "trades": perf.trades_total})

    # Post-session decision quality scoring
    from core.scoring import score_trades
    scoring = score_trades(session_id)
    tracer.log_decision("orchestrator", "trades_scored", detail=scoring)

    # Write outcome metrics to ag_outcomes for quality-vs-P&L correlation in Argus
    from evals.outcomes import (
        write_eod_outcome_metrics, push_trade_outcomes, push_outcome_signals, _NO_TRADE_EXITS,
    )
    today_trades = get_today_trades(session_id)
    write_eod_outcome_metrics(
        session_id, perf.realized_pnl, perf.win_rate, perf.trades_total,
        trades=today_trades,
    )
    # Push each closed trade's realized P&L to the Argus Outcome Ledger so the trace-based
    # prediction for that ticker reconciles against the real result.
    # Deliberately NOT pinning session_id. It looks like the safer call and it is not: Argus
    # writes ledger predictions from whichever session judged the ticker, and 45 of 288 rows
    # on production belong to INTRADAY sessions, including 21 of the 29 that have ever
    # settled. `session_id` here is today's PREMARKET session, so pinning it would fail to
    # find precisely the rows that currently reconcile. The server's fallback (most recent
    # unanswered row for the entity) is load-bearing until Argus matches on entity plus
    # business date instead.
    pushed = push_trade_outcomes(today_trades)
    reportable = sum(
        1 for t in today_trades
        if (t.get("exit_reason") or "") not in _NO_TRADE_EXITS and t.get("realized_pnl") is not None
    )
    # Record what was accepted AND what was owed, so a shortfall is visible in the trace
    # rather than only in the ledger weeks later.
    if reportable or pushed:
        tracer.log_decision("orchestrator", "ledger_outcomes_pushed",
                            detail={"count": pushed, "reportable": reportable,
                                    "dropped": reportable - pushed})

    # Report the SESSION's settled risk signals, which the per-trade push above cannot carry.
    # Without this the contract's risk conditions grade from nothing and its P&L conditions grade
    # from the agents' own trace payloads, i.e. from an estimate rather than from what settled.
    # Runs on every EOD including a zero-trade day, where "no drawdown, within limits" is a real
    # result and not an absence of one.
    signals_ok = push_outcome_signals(
        session_id, perf.realized_pnl, perf.trades_total, trades=today_trades,
    )
    tracer.log_decision("orchestrator", "outcome_signals_pushed", detail={"accepted": signals_ok})

    # Principal protection check
    # check_protection_status() records its own event internally when a tier fires,
    # so EOD only acts on the returned status — do not re-record it here.
    protection = check_protection_status()
    if protection.tier and protection.tier > 0:
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
        tracer.flush_cost_breakdown()
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
    send_alert(subject, build_daily_summary(perf, trades, learnings) + _opening_entry_report(trades))

    # Finalize session
    pnl_sign = "+" if perf.realized_pnl >= 0 else ""
    tracer.close_session(
        terminal_reason="eod_complete",
        trades_executed=perf.trades_total,
        result_summary=f"{perf.trades_total} trade(s), P&L {pnl_sign}${perf.realized_pnl:.2f}",
    )
    tracer.log_decision("orchestrator", "eod_complete",
                        detail={"pnl": perf.realized_pnl, "trades": perf.trades_total})

    # Safety net: judge the day's sessions server-side. trigger_server_judge only fires on the
    # premarket / intraday-entry close paths, so EOD and stand-down sessions would otherwise never
    # get L4 quality scored (this silently dropped quality coverage after the open-entry redesign).
    # Idempotent and best-effort; a failure must not affect EOD.
    try:
        from evals.outcomes import backfill_server_judge
        backfill_server_judge()
    except Exception as e:
        print(f"[eod] server judge backfill error (continuing): {e}")

    print(f"[eod] Complete. P&L=${pnl_sign}{perf.realized_pnl:.2f}, "
          f"{perf.trades_total} trade(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback as _tb
        try:
            from core.alerts import send_alert
            send_alert("Strategy C — EOD Crashed", f"{e}\n\n{_tb.format_exc()[-2000:]}")
        except Exception:
            pass
        raise
