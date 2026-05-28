from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

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


def get_investigated_tickers(session_id: str) -> list[str]:
    """Return tickers already investigated in this session's research tool calls."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_traces")
        .select("entity_id")
        .eq("session_id", session_id)
        .eq("agent", "research")
        .eq("step_type", "tool_call")
        .execute()
        .data
    ) or []
    return list({r["entity_id"] for r in rows if r.get("entity_id")})


def get_last_entry_scan_time(session_id: str) -> Optional[datetime]:
    """Return UTC datetime of the last intraday Research Agent scan, or None."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_traces")
        .select("created_at, outcome")
        .eq("session_id", session_id)
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
    if order_type == "limit"  and side == "sell": return "target"
    if order_type == "stop"   and side == "sell": return "stop"
    if order_type == "market" and side == "sell": return "eod_forced"
    return "manual"


def _place_intraday_trades(
    proposals: dict,
    approved_tickers: set[str],
    session_id: str,
) -> int:
    """Phase 0: write approved intraday trades to c_positions."""
    from core.db import get_client
    today = date.today().isoformat()
    now_  = datetime.utcnow().isoformat()
    count = 0
    for p in proposals.get("proposals", []):
        if p["ticker"] not in approved_tickers:
            continue
        shares = p.get("shares") or int(p["position_size"] / p["entry_price"])
        get_client().table("c_positions").insert({
            "session_id":    session_id,
            "ticker":        p["ticker"],
            "action":        "BUY",
            "entry_price":   p["entry_price"],
            "target_price":  p["target_price"],
            "stop_loss":     p["stop_loss"],
            "position_size": p["position_size"],
            "shares":        shares,
            "confidence":    p["confidence"],
            "status":        "open",
            "open_date":     today,
            "entry_time":    now_,
            "entry_context": "intraday",
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

    session_id = get_today_session_id()
    if not session_id:
        print("[intraday] No premarket session today. Exiting.")
        return

    protection = check_protection_status()
    if protection.suspended:
        print(f"[intraday] Protection suspended: {protection.reason}")
        return

    config = load_agent_config()
    params = load_params()
    tracer = TraceLogger(session_id)

    # Phase 0: position sync is DB-only (no Alpaca calls needed)

    daily_pnl   = get_daily_pnl(session_id)
    goal_status = evaluate_goals(daily_pnl)

    if goal_status.lock_in_mode:
        tracer.log_decision("orchestrator", "lock_in_mode",
                            detail={"daily_pnl": daily_pnl, "target": goal_status.daily_target})
        print(f"[intraday] Lock-in mode active. P&L {daily_pnl:.2f}.")
        return

    if goal_status.pnl_floor_hit:
        tracer.log_decision("orchestrator", "pnl_floor_hit", detail={"daily_pnl": daily_pnl})
        print(f"[intraday] P&L floor hit ({daily_pnl:.2f}). No new entries.")
        return

    if not config.get("enable_intraday_entries", True):
        tracer.log_decision("orchestrator", "normal_no_new_entries",
                            detail={"daily_pnl": daily_pnl})
        print(f"[intraday] Poll complete. P&L {daily_pnl:.2f}. Entries disabled.")
        return

    min_interval = config.get("intraday_entry_min_interval_mins", 55)
    last_scan = get_last_entry_scan_time(session_id)
    if last_scan is not None:
        elapsed_mins = (datetime.utcnow() - last_scan).total_seconds() / 60
        if elapsed_mins < min_interval:
            tracer.log_decision("orchestrator", "entry_scan_too_recent",
                                detail={"elapsed_mins": round(elapsed_mins, 1), "min_interval": min_interval})
            print(f"[intraday] Entry scan too recent ({elapsed_mins:.0f}m ago, min {min_interval}m). Skipping.")
            return

    if now_t >= _ENTRY_CLOSE:
        tracer.log_decision("orchestrator", "past_entry_window", detail={"time": str(now_t)})
        print(f"[intraday] Past entry window. No new entries.")
        return

    open_count = count_open_positions(session_id)
    available  = params.max_positions - open_count
    if available <= 0:
        tracer.log_decision("orchestrator", "no_capacity",
                            detail={"open": open_count, "max": params.max_positions})
        print(f"[intraday] No capacity ({open_count}/{params.max_positions}).")
        return

    min_score_bonus = config.get("intraday_min_score_bonus", 1)
    exclude         = get_investigated_tickers(session_id)
    max_new         = min(2, available, config.get("intraday_max_new_positions", 2))

    synthetic_report = {
        "decision": "GO",
        "max_positions": max_new,
        "bias": "NEUTRAL",
        "skip_reason": None,
        "summary": (
            f"Intraday scan {now_et.strftime('%H:%M')} ET. "
            f"Score >={params.strategy_min_score + min_score_bonus}. "
            f"Avoid: {exclude}."
        ),
    }

    try:
        proposals = run_research_agent(tracer, synthetic_report, params)
        if not proposals.get("proposals"):
            tracer.log_decision("orchestrator", "no_intraday_candidates")
            print("[intraday] No candidates found.")
            return

        verdicts = run_risk_agent(tracer, proposals, params)
        approved = [v for v in verdicts.get("verdicts", []) if v.get("verdict") == "APPROVED"]
        if not approved:
            tracer.log_decision("orchestrator", "intraday_all_rejected")
            print("[intraday] All proposals rejected.")
            return

        count = _place_intraday_trades(proposals, {v["ticker"] for v in approved}, session_id)
        tracer.log_decision("orchestrator", "intraday_entries_placed", detail={"count": count})
        print(f"[intraday] {count} trade(s) placed: "
              f"{', '.join(v['ticker'] for v in approved)}")

    except Exception as e:
        tracer.log_error("orchestrator", f"intraday error: {e}")
        print(f"[intraday] Error: {e}")
        raise


if __name__ == "__main__":
    main()
