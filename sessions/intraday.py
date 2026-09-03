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
from evals.business import write_funnel_evals
from trace.logger import TraceLogger

_ET          = pytz.timezone("America/New_York")
_POLL_START  = time(9, 15)
_POLL_END    = time(15, 50)
_ENTRY_CLOSE = time(13, 0)

_SCAN_OUTCOMES = {"no_intraday_candidates", "intraday_all_rejected",
                  "intraday_entry_gate_skipped", "intraday_entries_placed"}


def _entry_outcome(count: int) -> str:
    """Honest terminal_reason for the intraday entry step.

    ⛔ These are three different failures and must not share a name:

      no_intraday_candidates      research proposed nothing
      intraday_all_rejected       RISK rejected every proposal
      intraday_entry_gate_skipped risk APPROVED, the order gate skipped them anyway
                                  (chase / staleness / broker rejection)

    Until 2026-07-31 a zero-fill entry step returned 'intraday_all_rejected', so a run
    where risk approved three picks and the order gate dropped all three reported as
    though risk had turned them down. The daily email leads with terminal_reason, so it
    blamed the risk agent for two days while the actual cause was a stale price quote.
    The result_summary said the truth the whole time and nothing read it.

    See design/why-no-trades-2026-07-31.md.
    """
    return "intraday_entries_placed" if count > 0 else "intraday_entry_gate_skipped"


def _entry_rationale(proposals: dict, verdicts: dict, approved: list, count: int, outcome: str) -> str:
    """
    The intraday entry decision, in words, for quality scoring (argus#579).

    Pure and read-only: every figure is taken from the proposals and verdicts the session already
    produced, so this cannot describe a decision the run did not make. Kept out of the session body
    so it is testable without a broker, a market or a tracer.

    It answers `orchestrator_synthesis_completeness` on its own terms: the approved trades with their
    entry, size and confidence, and the terminal_reason for the session.
    """
    props    = proposals.get("proposals", []) or []
    verds    = verdicts.get("verdicts", []) or []
    rejected = [v for v in verds if v.get("verdict") != "APPROVED"]

    # ⛔ ENTRY, SIZE AND CONFIDENCE LIVE ON THE PROPOSAL, NOT ON THE VERDICT (argus#726).
    #
    # This read them off the verdict from 2026-08-14, and the risk agent's contract is only
    # {ticker, verdict, reason} — it has never carried a price, a size or a confidence. So all three
    # were None on every real session and the loop below dropped them silently, exactly as it is
    # meant to for a value the run did not produce. Measured on production: 0 passes in 128 runs
    # from 2026-08-17, against a 1.0 on 3 Aug, when this path did not yet exist.
    #
    # It read as correct because the test fixture put those keys on a verdict object, which no other
    # verdict fixture in the suite does and the risk agent never emits. The values were one argument
    # away the whole time.
    by_ticker = {p.get("ticker"): p for p in props if p.get("ticker")}

    lines = [
        f"Research proposed {len(props)}, risk approved {len(approved)} of {len(verds)}, "
        f"and {count} entered. Terminal reason: {outcome}."
    ]
    for v in approved:
        ticker = v.get("ticker", "?")
        # The verdict still wins where it carries a field, so a risk agent that starts returning one
        # overrides the proposal rather than being ignored.
        src = {**by_ticker.get(ticker, {}), **{k: x for k, x in v.items() if x is not None}}
        bits = [f"{ticker}"]
        for key, label in (("entry_price", "entry"), ("position_size", "size"), ("confidence", "confidence")):
            val = src.get(key)
            if val is not None:
                bits.append(f"{label} {val}")
        lines.append("Approved: " + ", ".join(bits) + ".")
    if rejected:
        why = ", ".join(
            f"{v.get('ticker', '?')} ({v.get('reason') or v.get('verdict') or 'no reason given'})"
            for v in rejected[:5]
        )
        lines.append(f"Rejected by risk: {why}.")
    if approved and count == 0:
        # The distinction #517 and the 31 Jul fix exist for: risk said yes and the gate still dropped
        # them. Saying so here keeps the reasoning honest about which step actually refused.
        lines.append(
            "Risk approved these and the order gate placed none, so the refusal was the gate "
            "rather than the risk assessment."
        )
    return " ".join(lines)


def get_premarket_session_id() -> Optional[str]:
    """Return today's premarket session_id from the agent's own run record, or None."""
    from core import run_state
    return run_state.today_premarket_run_id()


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


def get_open_positions(session_id: str) -> list[dict]:
    """Return open positions for today with entry_price."""
    from core.db import get_client
    rows = (
        get_client()
        .table("c_positions")
        .select("ticker,entry_price")
        .eq("session_id", session_id)
        .eq("status", "open")
        .eq("open_date", date.today().isoformat())
        .execute()
        .data
    ) or []
    return [r for r in rows if r.get("ticker")]


def get_last_entry_scan_time(premarket_session_id: str) -> Optional[datetime]:
    """Return the aware-UTC time of the last intraday entry scan, or None.

    This used to reconstruct the answer from Provy: find every intraday session whose parent was
    today's premarket session, then search their trace rows for the most recent scan-outcome
    decision. Two queries against the observability platform to answer a question about the
    agent's own pacing, which is why an outage there let the rate limit lapse.

    The agent now stamps the time on its own run record when it scans, so this is one local read.
    """
    from core import run_state
    return run_state.last_entry_scan_at(premarket_session_id)


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
                    _time.sleep(3)  # wait for Alpaca to release bracket leg qty
                    new_trail_id = None
                    for attempt in range(3):  # retry up to 3 times with 5s gaps
                        new_trail_id = submit_trailing_stop(ticker, shares, trail_pct)
                        if new_trail_id:
                            break
                        print(f"  [intraday] {ticker} trail submit attempt {attempt+1} failed — retrying in 5s")
                        _time.sleep(5)
                    if new_trail_id:
                        update["trail_order_id"] = new_trail_id
                        print(f"  [intraday] {ticker} trailing stop submitted (post-fill): {new_trail_id[:8]}")
                    else:
                        print(f"  [intraday] ⚠️  Trail still failing for {ticker} after 3 attempts — will retry next cycle")
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
                    # IMPORTANT: only mark unfilled when entry_filled is explicitly False.
                    # If get_bracket_status returns {"error": "..."} the key is absent (None).
                    # not None = True would incorrectly cancel filled orders — must check False.
                    if bracket.get("entry_filled") is False:
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
                    if "error" in bracket:
                        print(f"  [intraday] {ticker} bracket status error — skipping unfill check: {bracket['error']}")
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


def _trace_order(tracer, ticker: str, p: dict, shares: int,
                 order_id: Optional[str], fill_price: Optional[float]) -> None:
    """Record what the order call did, whatever it did.

    ⛔ EVERY ORDER LEAVES A TRACE, NOT ONLY THE ONES THAT FAIL. Until argus#679 this path traced
    `submit_bracket_order` ONLY on rejection, so the presence of an order trace meant the order was
    REFUSED and its absence meant nothing at all. Two things followed. Read literally, the trace
    stream said the orchestrator's work was rejections; and the agent looked bimodal to any
    baseline, because sessions split into "had a rejection" (57, averaging 3.67 steps) and "did not"
    (60, averaging 1.47) with a mean of 2.54 that no session ever takes.

    An agent whose failures are better instrumented than its successes cannot be judged on its
    method, because the record is a sample of its bad days.

    Three states, because the call genuinely has three. An accepted order that has not filled yet is
    not a fill: `submit_bracket_order` polls for one and gives up, so `order_id` without
    `fill_price` is a live working order and saying "filled" would be a claim nothing confirmed.

    Telemetry never breaks a trade. This sits between a live order and its `c_positions` row, so a
    raise here would leave a placed order with nothing recording it.
    """
    if not tracer:
        return
    try:
        if order_id is None:
            outcome = "rejected"
            output = {"error": f"order rejected or staleness gate: proposal=${p['entry_price']:.2f}"}
        else:
            outcome = "filled" if fill_price is not None else "accepted"
            output = {"order_id": order_id, "fill_price": fill_price}
        tracer.log_tool_call(
            "orchestrator", "submit_bracket_order",
            {"ticker": ticker, "shares": shares, "entry_price": p["entry_price"],
             "target_price": p["target_price"], "stop_price": p["stop_loss"]},
            {"outcome": outcome, **output},
            entity_id=ticker,
            outcome=outcome,
        )
    except Exception as exc:                                   # never let telemetry break a trade
        print(f"  [intraday] order trace failed for {ticker}: {exc}")


def _place_intraday_trades(
    proposals: dict,
    approved_tickers: set[str],
    session_id: str,
    trail_pct: float,
    max_entry_premium: float = 0.02,
    today_tickers: set[str] | None = None,
    tracer=None,
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
            max_entry_premium=max_entry_premium,
        )
        _trace_order(tracer, ticker, p, shares, order_id, fill_price)
        if order_id is None:
            print(f"  [intraday] {p['ticker']} order rejected or staleness gate fired — skipping")
            continue

        trail_order_id = None
        if fill_price is not None:
            trail_order_id = submit_trailing_stop(p["ticker"], shares, trail_pct)

        # ⛔ SAY WHAT THIS ENTRY IS EXPECTED TO EARN, AT THE MOMENT IT IS BOUGHT (argus#602).
        #
        # Intraday placed real orders and stated no expectation, so 28 of the 36 sessions carrying a
        # settled outcome on prod had no claim anything could be reconciled against. Provy filled the
        # gap by forecasting from layer-4 judge scores, which on this fleet do not separate outcomes
        # at all (held mean 0.925, failed 0.946 over 73 settled), and that degenerated into a constant.
        #
        # Keyed by entity_id = ticker, which is how the ledger settles (2.03 rows per session), so the
        # claim and the outcome meet at the same grain. Premarket's session-level total cannot do that.
        #
        # Uses the FILL, not the proposal: the claim must describe what was actually bought. A rejected
        # order returns above and emits nothing, because nothing was bought and nothing was promised.
        entry = fill_price or p["entry_price"]
        if tracer and entry:
            claim = {
                "estimated_profit": round((p["target_price"] - entry) * shares, 2),
                "max_loss":         round((entry - p["stop_loss"]) * shares, 2),
                "entry_price":      round(entry, 2),
                "target_price":     p["target_price"],
                "stop_loss":        p["stop_loss"],
                "shares":           shares,
            }
            if claim["max_loss"] > 0:
                claim["reward_risk"] = round(claim["estimated_profit"] / claim["max_loss"], 2)
            if p.get("confidence"):
                claim["confidence"] = p["confidence"]
            tracer.log_agent_message(
                "orchestrator",
                f"Entered {ticker} at ${entry:.2f}, target ${p['target_price']:.2f}, "
                f"stop ${p['stop_loss']:.2f}. Expected ${claim['estimated_profit']:.2f}.",
                "entered",
                entity_id=ticker,
                payload=claim,
            )

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


def _close_intraday(
    tracer, session_id: str, terminal: str, *,
    proposals: Optional[dict] = None,
    verdicts: Optional[dict] = None,
    trades_executed: int = 0,
    result_summary: str = "",
) -> None:
    """Close the session and record the funnel, in that order, from ONE place.

    ⛔ EVERY EXIT FROM main() GOES THROUGH HERE, AND THAT IS THE ENTIRE POINT (argus#675). premarket
    wrote its funnel checks on the main path only. On 27 Jul 2026 a redesign added an early return
    above that call, and research_yield / risk_approval_rate silently stopped grading for four weeks.
    No error, no gap, no empty state: the checks just stopped.

    ⛔ A GUARD SPREAD ACROSS EVERY RETURN PATH IS NOT A GUARD, because the next return path added
    will not have it. Putting the measurement at the close means a new exit inherits it by
    construction rather than by someone remembering.

    ⛔ AND THE PATHS THAT LOOK SKIPPABLE ARE THE ONES THAT MATTER MOST. `no_intraday_candidates` IS a
    research_yield of zero; `intraday_all_rejected` IS a risk_approval_rate of zero. Those two early
    returns were exactly the runs where these checks would have failed, so skipping them did not just
    lose data, it lost the failing half of it.
    """
    proposed = len((proposals or {}).get("proposals", []) or [])
    approved = len([v for v in (verdicts or {}).get("verdicts", []) or [] if v.get("verdict") == "APPROVED"])

    tracer.close_session(
        terminal, trades_proposed=proposed, trades_executed=trades_executed,
        result_summary=result_summary,
    )

    # ⛔ A CHECK THAT CANNOT RUN WRITES NO ROW. `proposals is None` means the research agent never
    # returned, so there is no funnel to report on: writing research_yield=0 there would blame
    # research for an orchestrator crash. Same rule the structural checks in provy-sim follow.
    if proposals is None:
        return
    try:
        write_funnel_evals(
            session_id=session_id, trades_proposed=proposed,
            trades_approved=approved, terminal_reason=terminal,
        )
    except Exception as exc:                                   # never let telemetry break a session
        print(f"[intraday] funnel eval write failed: {exc}")


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

    from sessions.premarket import _opening_entry_enabled
    if _opening_entry_enabled():
        # Entry redesign: entries are placed at the open via premarket OPG orders; intraday no longer
        # opens new positions. Position sync + trailing management run in the watchdog. Management-only.
        print("[intraday] Opening-entry mode: entries happen at the open (premarket). No intraday "
              "entries. Exiting.")
        return

    # premarket_session_id is the day-level data key: used for all c_positions
    # reads/writes, PnL, capacity, and deferred trade metadata. It never changes
    # within a trading day. Each intraday poll gets its own session_id for traces.
    premarket_session_id = get_premarket_session_id()
    if not premarket_session_id:
        # No premarket session yet — run the full pipeline now regardless of time.
        # bypass_checks=True skips the time-window gate while keeping market agent,
        # scanner, news analyst, research, and risk agents intact.
        print(f"[intraday] No premarket session at {now_et.strftime('%H:%M ET')} — running full pipeline")
        from sessions.premarket import main as _premarket_main
        _premarket_main(bypass_checks=True)
        premarket_session_id = get_premarket_session_id()
        if not premarket_session_id:
            print("[intraday] Premarket pipeline produced no session — exiting.")
            return

    protection = check_protection_status()
    if protection.suspended:
        print(f"[intraday] Protection suspended: {protection.reason}")
        return

    config = load_agent_config()
    params = load_params()

    # Deferred trade execution is handled by position_watchdog (runs every 15 min).
    # Sync positions here so capacity and P&L checks reflect current Alpaca state.
    _sync_positions(premarket_session_id, params.trail_pct)

    daily_pnl   = get_daily_pnl(premarket_session_id)
    goal_status = evaluate_goals(daily_pnl)

    if goal_status.lock_in_mode:
        print(f"[intraday] Lock-in mode active. P&L {daily_pnl:.2f}.")
        return

    if goal_status.pnl_floor_hit:
        print(f"[intraday] P&L floor hit ({daily_pnl:.2f}). No new entries.")
        return

    if not config.get("enable_intraday_entries", True):
        print(f"[intraday] Poll complete. P&L {daily_pnl:.2f}. Entries disabled.")
        return

    min_interval = config.get("intraday_entry_min_interval_mins", 55)
    last_scan = get_last_entry_scan_time(premarket_session_id)
    if last_scan is not None:
        # Aware UTC on both sides: the run record hands back an offset-aware timestamp, and
        # subtracting a naive utcnow() from it raises rather than returning a wrong number.
        elapsed_mins = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60
        if elapsed_mins < min_interval:
            print(f"[intraday] Entry scan too recent ({elapsed_mins:.0f}m ago, min {min_interval}m). Skipping.")
            return

    if now_t >= _ENTRY_CLOSE:
        print(f"[intraday] Past entry window. No new entries.")
        return

    open_count = count_open_positions(premarket_session_id)
    available  = params.max_positions - open_count
    if available <= 0:
        print(f"[intraday] No capacity ({open_count}/{params.max_positions}).")
        return

    # Only open an Argus session when agents will actually run.
    # Pass-through exits (past entry window, scan too recent, no capacity, etc.)
    # produce no agent output and pollute quality metrics if traced.
    intraday_session_id = str(uuid4())
    # Stamp the pacing clock here rather than after the scan finishes. This is the point past
    # every pass-through exit, so it is the point an entry scan genuinely begins; stamping now
    # also means a run that dies mid-scan still holds the rate limit instead of freeing it.
    from core import run_state
    run_state.stamp_entry_scan(premarket_session_id)
    tracer = TraceLogger(
        intraday_session_id,
        session_type="intraday",
        parent_session_id=premarket_session_id,
    )

    min_score_bonus = config.get("intraday_min_score_bonus", 1)
    today_tickers   = get_today_tickers(premarket_session_id)
    max_new         = min(2, available, config.get("intraday_max_new_positions", 2))

    open_pos_rows = get_open_positions(premarket_session_id)
    open_pos_context = ""
    if open_pos_rows:
        lines = [
            f"{p['ticker']} (entry ${float(p.get('entry_price') or 0):.2f}, "
            f"${float(p.get('unrealized_pnl') or 0):+.0f} unrealized)"
            for p in open_pos_rows
        ]
        open_pos_context = f"OPEN POSITIONS (do NOT propose these tickers): {', '.join(lines)}"

    synthetic_report = {
        "decision": "GO",
        "max_positions": params.max_positions,
        "bias": "NEUTRAL",
        "skip_reason": None,
        "open_positions_context": open_pos_context,
        "summary": (
            f"Intraday scan {now_et.strftime('%H:%M')} ET. "
            f"Score >={params.strategy_min_score + min_score_bonus}. "
            f"Avoid (already entered today): {sorted(today_tickers)}."
            + (f" {open_pos_context}" if open_pos_context else "")
        ),
    }

    try:
        from agents.orchestrator import _run_semantic_evals
        proposals = run_research_agent(tracer, synthetic_report, params)
        if not proposals.get("proposals"):
            tracer.log_decision("orchestrator", "no_intraday_candidates")
            _close_intraday(tracer, intraday_session_id, "no_intraday_candidates",
                            proposals=proposals, verdicts=None,
                            result_summary="No candidates from research agent")
            print("[intraday] No candidates found.")
            return

        verdicts = run_risk_agent(tracer, proposals, params)
        # Evals belong to this intraday session, not the premarket session.
        _run_semantic_evals(intraday_session_id, {}, {}, proposals, verdicts, {})

        approved = [v for v in verdicts.get("verdicts", []) if v.get("verdict") == "APPROVED"]
        if not approved:
            tracer.log_decision("orchestrator", "intraday_all_rejected")
            _close_intraday(tracer, intraday_session_id, "intraday_all_rejected",
                            proposals=proposals, verdicts=verdicts,
                            result_summary="All proposals rejected by risk")
            print("[intraday] All proposals rejected.")
            return

        # Positions are written under premarket_session_id — the day-level data key.
        count = _place_intraday_trades(
            proposals, {v["ticker"] for v in approved}, premarket_session_id, params.trail_pct,
            params.max_entry_premium,
            today_tickers=today_tickers,
            tracer=tracer,
        )
        outcome = _entry_outcome(count)
        tracer.log_decision("orchestrator", outcome, detail={"count": count})
        # ⛔ THE ORCHESTRATOR'S ONLY VOICE USED TO LIVE IN THE PREMARKET SYNTHESIS CALL, WHICH HAS NOT
        # RUN SINCE 27 JUL (the branch defers entry to the open by design). So the agent has been
        # routing every entry decision the fleet makes while `orchestrator_synthesis_completeness`
        # had nothing to read, and its last quality score is 3 Aug 2026. argus#579.
        #
        # ⚠️ THIS REPORTS, IT DOES NOT NARRATE. Intraday is a deterministic router, not an LLM
        # synthesiser, and pretending otherwise would be fabricating reasoning to satisfy a check.
        # It does not have to pretend: the criterion asks for "approved trades with entry price,
        # position size and confidence, plus a clear terminal_reason explaining the session outcome",
        # and that is precisely what this path decides. Every value below is read back from the
        # verdicts and proposals the session actually produced.
        tracer.log_agent_message("orchestrator", _entry_rationale(proposals, verdicts, approved, count, outcome), outcome)
        _close_intraday(
            tracer, intraday_session_id, outcome,
            proposals=proposals, verdicts=verdicts, trades_executed=count,
            result_summary=(
                f"{count} trade(s): {', '.join(v['ticker'] for v in approved)}"
                if count > 0
                else f"All {len(approved)} approved pick(s) skipped at entry gate: "
                     f"{', '.join(v['ticker'] for v in approved)}"
            ),
        )
        # ⛔ NO EXPLICIT JUDGE TRIGGER HERE (Provy #730). close_session above already grades the
        # session: /api/ingest/session/close runs the same canonical batch — the L4 judge and the
        # Outcome Ledger predictions — in the background. This used to ask a second time, one line
        # after the close, and the two gradings raced: both read "already scored" as empty and both
        # wrote. Measured on production from 2026-08-17, 46% of quality rows were the second copy,
        # a median 1.4s apart, and 17 slots recorded the same work as both a pass and a failure.
        # Provy now refuses the duplicate row, but the wasted judge calls were ours to stop.
        print(f"[intraday] {count} trade(s) placed: "
              f"{', '.join(v['ticker'] for v in approved)}")

    except Exception as e:
        tracer.log_error("orchestrator", f"intraday error: {e}")
        # ⛔ proposals IS DELIBERATELY NOT PASSED. If research never returned there is no funnel to
        # report, and a fabricated research_yield=0 would blame research for an orchestrator crash.
        _close_intraday(tracer, intraday_session_id, "error",
                        proposals=locals().get("proposals"), result_summary=f"Error: {e}")
        print(f"[intraday] Error: {e}")
        raise


if __name__ == "__main__":
    main()
