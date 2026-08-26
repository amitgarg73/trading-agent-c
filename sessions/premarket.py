from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timezone
from uuid import uuid4

import pytz

from agents.market_agent import run_market_agent
from agents.orchestrator import run_premarket_pipeline
from core.agent_config import is_trading_day, load_agent_config
from core.alerts import send_alert
from core.params import load_params
from core.protection import check_protection_status
from evals.business import write_funnel_evals
from trace.logger import TraceLogger

_ET              = pytz.timezone("America/New_York")
_PREMARKET_START = time(6, 0)   # default — overridable via c_agent_config.premarket_window_start
_PREMARKET_END   = time(10, 30) # default — overridable via c_agent_config.premarket_window_end
_MARKET_OPEN     = time(9, 30)  # orders only submitted after market opens


def _opening_entry_enabled() -> bool:
    """Entry-redesign rollout flag (default OFF). When ON, premarket decides pre-open and submits
    opening orders so the fill is the day's open, and intraday runs management-only.
    Old chase path stays intact when OFF. See design/entry-redesign-premarket-open.md."""
    return os.environ.get("OPENING_ENTRY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _use_opg_orders() -> bool:
    """Whether opening entries use OPG (opening-auction) orders.

    OPG fills at the regular-session open, but Alpaca's PAPER environment does not simulate
    the opening auction, so OPG orders there always expire unfilled (observed 2026-07-02).
    On paper (the default) submit a market order at the open instead: it fills in continuous
    trading and still enters at ~the open, which is the entry basis the backtests measured.
    A live account uses true OPG. Force either way with USE_OPG=1 / USE_OPG=0."""
    override = os.environ.get("USE_OPG", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    from core.alpaca import _PAPER
    return not _PAPER


def _execute_trades(trades: list[dict], session_id: str, trail_pct: float, max_entry_premium: float = 0.0, tracer=None) -> None:
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
            max_entry_premium=max_entry_premium,
        )
        if order_id is None:
            print(f"  [premarket] {trade['ticker']} order rejected or staleness gate fired — skipping")
            if tracer:
                tracer.log_tool_call(
                    "orchestrator", "submit_bracket_order",
                    {"ticker": trade["ticker"], "entry_price": trade["entry_price"]},
                    {"outcome": "rejected", "error": f"order rejected or staleness gate: proposal=${trade['entry_price']:.2f}"},
                )
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


def _execute_opening_orders(trades: list[dict], session_id: str, tracer=None, on_open: bool = True,
                            entry_context: str | None = None) -> int:
    """
    Entry redesign (design/entry-redesign-premarket-open.md): submit market-on-open (OPG) orders
    for the decided shortlist BEFORE the ~09:28 ET auction cutoff, so the fill is the day's open —
    the entry basis the backtests showed is worth the whole edge. No chase or staleness gate.

    OPG cannot be a bracket, so the position is written now with the opening order id and NO fill /
    trailing stop yet; the post-open reconcile (position watchdog) backfills the real open fill and
    attaches the trailing stop once the auction has printed. Returns the count submitted.
    """
    from core.alpaca import submit_opening_order, cancel_order
    from core.db import get_client
    today  = date.today().isoformat()
    now_   = datetime.utcnow().isoformat()
    client = get_client()
    submitted = 0
    for trade in trades:
        shares = trade.get("shares") or int(trade["position_size"] / trade["entry_price"])
        if shares <= 0:
            continue
        order_id = submit_opening_order(trade["ticker"], shares, on_open=on_open)  # MOO at open, else near-open market
        if order_id is None:
            print(f"  [premarket] {trade['ticker']} opening order failed — skipping")
            if tracer:
                tracer.log_tool_call(
                    "orchestrator", "submit_opening_order",
                    {"ticker": trade["ticker"], "shares": shares},
                    {"outcome": "failed", "error": "opening order not accepted"},
                )
            continue
        # The order is now live at Alpaca. Record the tracking row so the watchdog can adopt it.
        # If the insert fails, cancel the order rather than leave a live, untracked position.
        try:
            client.table("c_positions").insert({
                "session_id":      session_id,
                "ticker":          trade["ticker"],
                "action":          "BUY",
                "entry_price":     None,               # backfilled with the real open fill post-open
                "target_price":    trade["target_price"],
                "stop_loss":       trade["stop_loss"],
                "position_size":   trade["position_size"],
                "shares":          shares,
                "confidence":      trade["confidence"],
                "status":          "pending_open",     # post-open reconcile flips to open + attaches trail
                "open_date":       today,
                "entry_time":      now_,
                "entry_context":   entry_context or ("opening_auction" if on_open else "opening_fallback"),
                "score_at_entry":  trade.get("score_at_entry"),
                "alpaca_order_id": order_id,
                "trail_order_id":  None,
            }).execute()
        except Exception as exc:
            cancelled = cancel_order(order_id)
            print(f"  [premarket] {trade['ticker']} position insert failed ({exc}); "
                  f"cancelled opening order {order_id} (ok={cancelled})")
            if tracer:
                tracer.log_tool_call(
                    "orchestrator", "submit_opening_order",
                    {"ticker": trade["ticker"], "shares": shares, "order_id": order_id},
                    {"outcome": "cancelled", "error": f"position insert failed: {exc}"},
                )
            continue
        submitted += 1
    return submitted


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


def _write_session_evals(
    session_id: str,
    trades_proposed: int,
    trades_approved: int,
    terminal_reason: str,
) -> None:
    """Write L5 business evals for this session.

    L4 quality scoring is no longer done here: it moved to the canonical server-side judge,
    triggered after close_session (it scores per ticker and writes the Outcome Ledger
    predictions, which the old local judge could not).
    """
    write_funnel_evals(
        session_id=session_id,
        trades_proposed=trades_proposed,
        trades_approved=trades_approved,
        terminal_reason=terminal_reason,
    )


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
    Skip if today already has a completed premarket session or one in_progress started < 60 min ago.
    Prevents concurrent runs when the cron fires twice or intraday triggers premarket.
    Reads the agent's own run record, so a second run is still prevented when Provy is
    unreachable -- the guard failing open would place the day's trades twice.
    """
    from core import run_state
    sess = run_state.today_premarket_run(today)
    if not sess:
        return False, ""
    term   = sess.get("terminal_reason") or ""
    status = sess.get("status") or ""
    sid    = sess["id"]
    _COMPLETE = {
        "no_opportunity", "converged", "error", "eod_complete", "no_candidates",
        "risk_rejected", "manual_stop", "watchdog_timeout", "circuit_breaker",
        "deferred_to_open",
    }
    if term in _COMPLETE or status == "completed":
        return True, f"Session {sid[:8]} already completed ({term}). Skipping."
    if status == "in_progress" or term in ("in_progress", ""):
        # Both sides aware UTC. The old code stripped a trailing "Z" and subtracted from a naive
        # utcnow(); against an offset-aware timestamp that raises TypeError, which the except
        # below swallowed into "no existing session" -- a concurrency guard that fails OPEN and
        # lets the day's trades be placed twice. parse_ts normalises instead.
        started = run_state.parse_ts(sess.get("started_at"))
        if started:
            age_s = (datetime.now(timezone.utc) - started).total_seconds()
            if age_s < 3600:
                return True, (
                    f"Session {sid[:8]} in_progress ({int(age_s / 60)}m old). "
                    "Skipping concurrent run."
                )
    return False, ""


def main(bypass_checks: bool = False) -> None:
    now_et  = datetime.now(_ET)
    now_t   = now_et.time()
    weekday = now_et.strftime("%a").upper()[:3]

    if not bypass_checks:
        if not is_trading_day(weekday):
            print(f"[premarket] Not a trading day ({weekday}). Exiting.")
            return

        config        = load_agent_config()
        window_start  = _window_time(config, "premarket_window_start", _PREMARKET_START)
        window_end    = _window_time(config, "premarket_window_end",   _PREMARKET_END)

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
    else:
        print(f"[premarket] --bypass-checks active: skipping trading day, window, protection, and dedup guards.")

    params     = load_params()
    session_id = str(uuid4())
    tracer     = TraceLogger(session_id, session_type="premarket")
    print(f"[premarket] Session {session_id} — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    try:
        # Guards terminal_reason: once the session is closed successfully, a later failure
        # (a post-close side-effect) must never re-close it as "error" (Provy #288 mislabel).
        session_closed = False
        from scanner.scanner import run_scanner
        print("[premarket] Running scanner...")
        candidate_count = run_scanner(scan_date=date.today(), tracer=tracer)
        print(f"[premarket] Scanner: {candidate_count} candidates ready")

        if candidate_count == 0:
            print("[premarket] No scanner candidates today. Exiting.")
            tracer.close_session(
                terminal_reason="no_candidates",
                result_summary="Scanner returned 0 candidates. No pipeline run.",
            )
            session_closed = True
            send_alert(
                f"Strategy C — No Candidates {now_et.strftime('%Y-%m-%d')}",
                "Scanner returned 0 results. Market data issue or all tickers filtered.",
            )
            return

        # Defer to open: before market open the IEX feed has no live/premarket quotes, so
        # the research agent's gates receive null intraday signals (rs_vs_spy, above_vwap,
        # premarket_change) for every candidate and skip them all — a daily false
        # "no_viable_proposals". Scan to populate today's candidates and defer the entry
        # decision to the open; the intraday session runs the full research + entry pipeline
        # after 9:30 with live data. (To trade premarket, move the data client to the SIP
        # feed and run the pipeline here instead of deferring.)
        if now_t < _MARKET_OPEN and _opening_entry_enabled():
            # Entry redesign: decide pre-open and submit opening orders so the fill is the day's open
            # (design/entry-redesign-premarket-open.md). The funnel must produce a shortlist pre-open
            # (scanner-conviction gate); the post-open watchdog backfills the real open fill and
            # attaches the trailing stop. No chase; no staleness/chase gate. On paper we use a
            # market-at-open order (OPG is not filled in Alpaca paper); live uses true OPG.
            result   = run_premarket_pipeline(tracer, params)
            result.pop("_v2_market_report", {})
            trades   = result.get("trades", [])
            terminal = result["session_meta"]["terminal_reason"]
            use_opg  = _use_opg_orders()
            submitted = _execute_opening_orders(
                trades, session_id, tracer=tracer, on_open=use_opg,
                entry_context="opening_auction" if use_opg else "opening_market",
            ) if trades else 0
            kind = "OPG" if use_opg else "market-at-open"
            print(f"[premarket] Opening-entry mode: {submitted} {kind} order(s) submitted for the 9:30 open.")
            tracer.close_session(
                terminal_reason="opening_orders_submitted" if submitted else terminal,
                trades_proposed=len(trades),
                trades_executed=submitted,
                result_summary=(
                    f"{submitted} opening order(s) submitted for the open: "
                    f"{', '.join(t['ticker'] for t in trades)}" if submitted
                    else f"No opening orders. {terminal}"
                ),
            )
            session_closed = True
            send_alert(
                f"Strategy C — Premarket {now_et.strftime('%Y-%m-%d')}",
                (f"{submitted} opening order(s) submitted for the 9:30 open: "
                 f"{', '.join(t['ticker'] for t in trades)}." if submitted
                 else f"No opening orders ({terminal}). {candidate_count} candidates scanned."),
            )
            return

        if now_t < _MARKET_OPEN:
            # OBSERVATION ONLY (4 Aug 2026). The macro read stopped running on 27 Jul, when this
            # branch began returning before run_premarket_pipeline, which is the only thing that
            # calls the market agent. Intraday does not call it either: it hand-builds the report
            # with decision "GO", bias "NEUTRAL" and the static max_positions, so the gate that
            # stood the system down on 4 days between 17 Jun and 6 Jul has not run since.
            #
            # ⛔ THIS DOES NOT TRADE ON IT. The decision is recorded and nothing reads it, on
            # purpose: restoring the gate and restoring the signal are two separate decisions, and
            # this is only the second. Once there is a fortnight of "what it would have said", the
            # first becomes an evidence question. Non-fatal by construction, so a macro failure can
            # never stop a scan from being recorded.
            try:
                market_report = run_market_agent(tracer, params)
                decision = market_report.get("decision")
                print(f"[premarket] Market read (observation only): {decision} "
                      f"— {market_report.get('summary', '')[:120]}")
                tracer.log_decision("market", "observation_only", detail={
                    "decision":      decision,
                    "bias":          market_report.get("bias"),
                    "max_positions": market_report.get("max_positions"),
                    "skip_reason":   market_report.get("skip_reason"),
                    "confidence":    market_report.get("confidence"),
                    "key_factors":   market_report.get("key_factors"),
                    "would_have_blocked_trading": decision == "SKIP",
                    "acted_on": False,
                })
            except Exception as e:
                print(f"  [premarket] Market read failed (non-fatal, observation only): {e}")
                tracer.log_error("market", f"observation-only read failed: {e}")

            print(f"[premarket] Pre-open ({now_et.strftime('%H:%M ET')}) — {candidate_count} candidates "
                  f"scanned; deferring entry decision to market open (intraday).")
            # ⛔ THIS VALUE NEVER SURVIVES THE DAY, AND THAT IS THE SESSION MODEL, NOT A BUG.
            # sessions/eod.py reuses THIS session_id (TraceLogger(session_id, session_type="eod"))
            # and closes it "eod_complete" at 15:55, so every premarket session in the database reads
            # eod_complete and "deferred_to_open" has never once appeared there. Checked on prod
            # 25 Aug: 17 of 17 premarket sessions since 1 Aug are eod_complete.
            #
            # Consequence worth knowing before you rely on it: you CANNOT ask the database how
            # premarket ended. Chased down under argus#675; recorded here so nobody chases it twice.
            tracer.close_session(
                terminal_reason="deferred_to_open",
                result_summary=(
                    f"{candidate_count} candidates scanned. Entry decision deferred to market open; "
                    "intraday runs research with live data."
                ),
            )
            session_closed = True
            # ⛔ NO FUNNEL EVAL HERE, DELIBERATELY, AND SAYING SO IS THE POINT (argus#675). This path
            # scans candidates and stops: research and risk do not run, so research_yield and
            # risk_approval_rate have nothing to measure and a zero would be a fabrication. The
            # funnel now lives in sessions/intraday.py::_close_intraday, which records it on EVERY
            # exit including the two that mean zero.
            #
            # ⛔ THE ORIGINAL BUG WAS AN UNREMARKED RETURN ABOVE THE WRITER. It looked exactly like
            # this line does, which is why this one carries a reason and that one did not.
            send_alert(
                f"Strategy C — Premarket {now_et.strftime('%Y-%m-%d')}",
                f"{candidate_count} candidates scanned. Entries deferred to market open — intraday "
                "runs the research + entry pipeline with live data after 9:30 ET.",
            )
            return

        result    = run_premarket_pipeline(tracer, params)
        v2_report = result.pop("_v2_market_report", {})
        trades    = result.get("trades", [])
        terminal  = result["session_meta"]["terminal_reason"]

        if trades and now_t >= _MARKET_OPEN:
            _execute_trades(trades, session_id, params.trail_pct, params.max_entry_premium, tracer=tracer)
        elif trades:
            print(f"[premarket] Market not yet open ({now_et.strftime('%H:%M ET')}) "
                  f"— storing {len(trades)} pending trade(s) for 9:30 AM execution")
            tracer.set_pending_trades(trades)

        # V1 shadow eval — non-blocking, does not affect trades
        try:
            v1_report = run_market_agent(tracer, params)
            _log_market_eval(session_id, v1_report, v2_report)
        except Exception as e:
            print(f"  [premarket] Shadow eval failed (non-fatal): {e}")

        if trades:
            print(f"[premarket] {len(trades)} trade(s): "
                  f"{', '.join(t['ticker'] for t in trades)}")
        else:
            print(f"[premarket] No trades. Terminal: {terminal}")

        if trades:
            tickers = ", ".join(t["ticker"] for t in trades)
            summary = f"{len(trades)} trade(s) executed: {tickers}"
        else:
            summary = f"No trades. {terminal}"
        _write_session_evals(
            session_id=session_id,
            trades_proposed=len(result.get("trades", [])),
            trades_approved=len(trades),
            terminal_reason=terminal,
        )
        tracer.close_session(
            terminal_reason=terminal,
            trades_proposed=len(result.get("trades", [])),
            trades_approved=len(trades),
            trades_executed=len(trades),
            retry_triggered=result["session_meta"].get("retry_triggered", False),
            result_summary=summary,
        )
        session_closed = True
        # After close (so terminal_reason is set for the ledger skip-exclusion), run the post-close
        # side-effects: the canonical server judge and the alert. The session is already closed, so a
        # failure here must not re-close it or flip terminal_reason to "error" (Provy #288).
        try:
            from evals.outcomes import trigger_server_judge
            trigger_server_judge(session_id)
            subject, body = _build_premarket_alert(result, session_id, now_et)
            send_alert(subject, body)
        except Exception as e:
            print(f"  [premarket] Post-close side-effect failed (non-fatal, session already closed): {e}")

    except Exception as e:
        tracer.log_error("orchestrator", str(e))
        # Only mark the session errored if it was not already closed successfully. A failure after a
        # good close (e.g. a post-close side-effect) must never overwrite a valid terminal_reason
        # such as skip_propagated with "error" (Provy #288).
        if not session_closed:
            tracer.close_session(terminal_reason="error", result_summary=f"Error: {e}")
        send_alert("Strategy C — Premarket Error", str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bypass-checks", action="store_true",
                        help="Skip trading-day, window, protection, and dedup guards (for local E2E testing)")
    args = parser.parse_args()
    main(bypass_checks=args.bypass_checks)
