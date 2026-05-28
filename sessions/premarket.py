from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytz

from agents.orchestrator import run_premarket_pipeline
from core.agent_config import is_trading_day, load_agent_config
from core.alerts import send_alert
from core.params import load_params
from core.protection import check_protection_status
from trace.logger import TraceLogger

_ET = pytz.timezone("America/New_York")


def _execute_trades(trades: list[dict], session_id: str) -> None:
    """Phase 0: write approved trades to c_positions (no broker calls)."""
    from core.db import get_client
    today = date.today().isoformat()
    now_  = datetime.utcnow().isoformat()
    client = get_client()
    for trade in trades:
        shares = trade.get("shares") or int(trade["position_size"] / trade["entry_price"])
        client.table("c_positions").insert({
            "session_id":    session_id,
            "ticker":        trade["ticker"],
            "action":        "BUY",
            "entry_price":   trade["entry_price"],
            "target_price":  trade["target_price"],
            "stop_loss":     trade["stop_loss"],
            "position_size": trade["position_size"],
            "shares":        shares,
            "confidence":    trade["confidence"],
            "status":        "open",
            "open_date":     today,
            "entry_time":    now_,
            "score_at_entry": trade.get("score_at_entry"),
        }).execute()


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


def main() -> None:
    now_et  = datetime.now(_ET)
    weekday = now_et.strftime("%a").upper()[:3]

    if not is_trading_day(weekday):
        print(f"[premarket] Not a trading day ({weekday}). Exiting.")
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

    params     = load_params()
    config     = load_agent_config()
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

        result   = run_premarket_pipeline(tracer, params)
        trades   = result.get("trades", [])
        terminal = result["session_meta"]["terminal_reason"]

        if trades:
            _execute_trades(trades, session_id)
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
