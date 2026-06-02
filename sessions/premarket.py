from __future__ import annotations

from datetime import date, datetime, time
from uuid import uuid4

import pytz

from agents.market_agent import run_market_agent as run_market_agent_v1
from agents.orchestrator import run_premarket_pipeline
from core.agent_config import is_trading_day, load_agent_config
from core.alerts import send_alert
from core.params import load_params
from core.protection import check_protection_status
from trace.logger import TraceLogger

_ET              = pytz.timezone("America/New_York")
_PREMARKET_START = time(6, 0)   # default — overridable via c_agent_config.premarket_window_start
_PREMARKET_END   = time(10, 30) # default — overridable via c_agent_config.premarket_window_end
_MARKET_OPEN     = time(9, 30)  # orders only submitted after market opens


def _execute_trades(trades: list[dict], session_id: str, trail_pct: float) -> None:
    """Submit bracket orders to Alpaca and write confirmed positions to c_positions."""
    from core.alpaca import submit_bracket_order, submit_trailing_stop
    from core.db import get_client
    today  = date.today().isoformat()
    now_   = datetime.utcnow().isoformat()
    client = get_client()
    for trade in trades:
        shares = trade.get("shares") or int(trade["position_size"] / trade["entry_price"])
        order_id, fill_price = submit_bracket_order(
            ticker=trade["ticker"],
            shares=shares,
            entry_price=trade["entry_price"],
            target_price=trade["target_price"],
            stop_price=trade["stop_loss"],
        )
        if order_id is None:
            print(f"  [premarket] {trade['ticker']} order rejected — skipping")
            continue

        trail_order_id = None
        if fill_price is not None:
            trail_order_id = submit_trailing_stop(trade["ticker"], shares, trail_pct)

        client.table("c_positions").insert({
            "session_id":      session_id,
            "ticker":          trade["ticker"],
            "action":          "BUY",
            "entry_price":     fill_price or trade["entry_price"],
            "target_price":    trade["target_price"],
            "stop_loss":       trade["stop_loss"],
            "position_size":   trade["position_size"],
            "shares":          shares,
            "confidence":      trade["confidence"],
            "status":          "open",
            "open_date":       today,
            "entry_time":      now_,
            "score_at_entry":  trade.get("score_at_entry"),
            "alpaca_order_id": order_id,
            "trail_order_id":  trail_order_id,
        }).execute()


def _log_market_eval(session_id: str, v1: dict, v2: dict) -> None:
    """Persist side-by-side V1 (baseline) vs V2 (shadow) market agent results."""
    from core.db import get_client
    try:
        row = {
            "eval_date":          date.today().isoformat(),
            "session_id":         session_id,
            "v1_decision":        v1.get("decision", "SKIP"),
            "v1_max_positions":   v1.get("max_positions"),
            "v1_bias":            v1.get("bias"),
            "v1_summary":         v1.get("summary"),
            "v2_decision":        v2.get("decision", "SKIP"),
            "v2_max_positions":   v2.get("max_positions"),
            "v2_bias":            v2.get("bias"),
            "v2_confidence":      v2.get("confidence"),
            "v2_key_factors":     v2.get("key_factors") or [],
            "v2_summary":         v2.get("summary"),
            "v2_circuit_breaker": v2.get("circuit_breaker"),
            "decisions_agree":    v1.get("decision") == v2.get("decision"),
            "v1_more_aggressive": (v1.get("max_positions") or 0) > (v2.get("max_positions") or 0),
        }
        get_client().table("c_market_evals").upsert(row, on_conflict="eval_date").execute()
    except Exception as e:
        print(f"  [premarket] _log_market_eval failed (non-fatal): {e}")


def _run_news_analyst(_candidates: list[dict]) -> list[dict]:
    """Phase 0 stub. Phase 1: spawn agents/ts/news_analyst.js subprocess."""
    return []


def _build_premarket_alert(result: dict, session_id: str, now_et: datetime) -> tuple[str, str]:
    trades   = result.get("trades", [])
    terminal = result["session_meta"]["terminal_reason"]
    meta     = result["session_meta"]
    subject  = f"Strategy C — Premarket {now_et.strftime('%Y-%m-%d')}"
    lines = [
        f"Session: {session_id[:8]}",
        f"Result:  {terminal}",
        f"Trades:  {len(trades)} — "
        + (", ".join(t["ticker"] for t in trades) if trades else "none"),
        f"Est. profit: ${result.get('total_estimated_profit', 0):.2f}",
        f"Max loss:    ${result.get('total_max_loss', 0):.2f}",
        f"Retry:  {'yes' if meta.get('retry_triggered') else 'no'}",
    ]
    return subject, "\n".join(lines)


def _window_time(config: dict, key: str, default: time) -> time:
    """Parse HH:MM string from config, fall back to default on missing/invalid."""
    val = config.get(key)
    if not val:
        return default
    try:
        h, m = str(val).split(":")
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return default


def _existing_session_guard(today: str) -> tuple[bool, str]:
    """
    Returns (should_skip, reason_msg).
    Skip if today already has a completed session or an in_progress one started < 60 min ago.
    Prevents concurrent runs when the cron fires twice or intraday triggers premarket.
    """
    from core.db import get_client
    rows = (
        get_client()
        .table("c_sessions")
        .select("id,terminal_reason,started_at")
        .eq("date", today)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
        .data or []
    )
    if not rows:
        return False, ""
    sess = rows[0]
    term = sess.get("terminal_reason") or ""
    sid  = sess["id"]
    _COMPLETE = {
        "no_opportunity", "converged", "error", "eod_complete", "no_candidates",
        "risk_rejected", "manual_stop", "watchdog_timeout", "circuit_breaker",
    }
    if term in _COMPLETE:
        return True, f"Session {sid[:8]} already completed ({term}). Skipping."
    if term in ("in_progress", ""):
        try:
            started = sess.get("started_at") or ""
            if started:
                age_s = (datetime.utcnow() - datetime.fromisoformat(started.replace("Z", ""))).total_seconds()
                if age_s < 3600:
                    return True, (
                        f"Session {sid[:8]} in_progress ({int(age_s / 60)}m old). "
                        "Skipping concurrent run."
                    )
        except (ValueError, TypeError):
            pass
    return False, ""


def main() -> None:
    now_et  = datetime.now(_ET)
    weekday = now_et.strftime("%a").upper()[:3]

    if not is_trading_day(weekday):
        print(f"[premarket] Not a trading day ({weekday}). Exiting.")
        return

    config        = load_agent_config()
    window_start  = _window_time(config, "premarket_window_start", _PREMARKET_START)
    window_end    = _window_time(config, "premarket_window_end",   _PREMARKET_END)

    now_t = now_et.time()
    if not (window_start <= now_t <= window_end):
        print(f"[premarket] Outside premarket window ({now_et.strftime('%H:%M ET')}, "
              f"allowed {window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')} ET). Exiting.")
        return

    protection = check_protection_status()
    if protection.suspended:
        send_alert(
            "Strategy C — Suspended",
            f"Tier {protection.tier} protection active: {protection.reason}\n"
            f"Resume: {protection.resume_at}",
        )
        print(f"[premarket] Protection tier {protection.tier} suspended.")
        return

    should_skip, skip_msg = _existing_session_guard(date.today().isoformat())
    if should_skip:
        print(f"[premarket] {skip_msg}")
        return

    params     = load_params()
    session_id = str(uuid4())
    tracer     = TraceLogger(session_id)
    print(f"[premarket] Session {session_id} — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    try:
        from scanner.scanner import run_scanner
        print("[premarket] Running scanner...")
        candidate_count = run_scanner(scan_date=date.today())
        print(f"[premarket] Scanner: {candidate_count} candidates ready")

        if candidate_count == 0:
            print("[premarket] No scanner candidates today. Exiting.")
            tracer.close_session(terminal_reason="no_candidates")
            send_alert(
                f"Strategy C — No Candidates {now_et.strftime('%Y-%m-%d')}",
                "Scanner returned 0 results. Market data issue or all tickers filtered.",
            )
            return

        result    = run_premarket_pipeline(tracer, params)
        v2_report = result.pop("_v2_market_report", {})
        trades    = result.get("trades", [])
        terminal  = result["session_meta"]["terminal_reason"]

        if trades and now_t >= _MARKET_OPEN:
            _execute_trades(trades, session_id, params.trail_pct)
        elif trades:
            print(f"[premarket] Market not yet open ({now_et.strftime('%H:%M ET')}) "
                  f"— storing {len(trades)} pending trade(s) for 9:30 AM execution")
            from core.db import get_client
            get_client().table("c_sessions").update(
                {"pending_trades": trades}
            ).eq("id", session_id).execute()

        # V1 shadow eval — non-blocking, does not affect trades
        try:
            v1_report = run_market_agent_v1(tracer, params)
            _log_market_eval(session_id, v1_report, v2_report)
        except Exception as e:
            print(f"  [premarket] Shadow eval failed (non-fatal): {e}")

        if trades:
            print(f"[premarket] {len(trades)} trade(s): "
                  f"{', '.join(t['ticker'] for t in trades)}")
        else:
            print(f"[premarket] No trades. Terminal: {terminal}")

        tracer.close_session(
            terminal_reason=terminal,
            trades_proposed=len(result.get("trades", [])),
            trades_approved=len(trades),
            trades_executed=len(trades),
            retry_triggered=result["session_meta"].get("retry_triggered", False),
        )
        subject, body = _build_premarket_alert(result, session_id, now_et)
        send_alert(subject, body)

    except Exception as e:
        tracer.log_error("orchestrator", str(e))
        tracer.close_session(terminal_reason="error")
        send_alert("Strategy C — Premarket Error", str(e))
        raise


if __name__ == "__main__":
    main()
