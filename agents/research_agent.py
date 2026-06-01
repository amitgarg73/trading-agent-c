from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.market_tools import get_sector_rotation
from agents.tools.research_tools import (
    get_candidates,
    get_news,
    get_position_history,
    get_premarket_snapshot,
    get_ticker_fundamentals,
    get_ticker_market_data,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-haiku-4-5-20251001"

# Per-ticker agent cap: 4 tools × ~15s each + reasoning = ~90s is plenty
_TICKER_TIMEOUT_S  = 120
# Outer cap covers screening + parallel investigation + some buffer
_TOTAL_TIMEOUT_S   = 360
# Investigate more candidates than max_positions so we can select the best
_MAX_CANDIDATES    = 6


# ── Per-ticker investigation ──────────────────────────────────────────────────

_INVESTIGATE_SYSTEM = """
You are investigating a single stock for an intraday trading setup.
Call ALL FOUR tools in this order, then output your decision.

TOOL ORDER (mandatory):
1. get_news           — if blackout: true, return SKIP immediately.
2. get_ticker_fundamentals — float, short interest, PDH/PDL levels.
3. get_ticker_market_data  — ATR, volume conviction, VWAP, RS, ORB, live price.
4. get_position_history    — recent win rate on this ticker.

SKIP RULES (return SKIP if ANY apply):
- get_news: blackout is true
- get_ticker_market_data: atr_pct > 5 (stop too noisy)
- get_ticker_market_data: today_pct_change > 4 (already extended; 8% target unreachable)
- get_ticker_market_data: live_price is null and not pre-market (data issue)

CONFIDENCE RULES:
- HIGH:   score >= 7, above_vwap true, rs_vs_spy >= 1.5, today_pct_change <= 2
  (pre-market: score >= 8, premarket_change_pct > 0.5, conviction HIGH or MODERATE)
- MEDIUM: score 5-6, or above_vwap with rs_vs_spy >= 0.8, today_pct_change <= 4
  (pre-market: score 6-7, positive premarket_change, conviction not LOW)
- LOW:    score 5-6, mixed signals, or today_pct_change 2-4
- Skip if nothing reaches LOW.

On CAUTION days (passed in context): require score >= 7 and above_vwap true.

PROPOSAL RULES:
- entry_price  = live_price (or premarket_price if pre-market)
- target_price = round(entry_price * 1.08, 2)
- stop_loss: use atr_pct from get_ticker_market_data.
  stop_pct = max(atr_pct * 0.8, 0.5) / 100
  stop_loss = round(entry_price * (1 - stop_pct), 2)
  (0.8× ATR, minimum 0.5% floor — survives normal intraday noise)
- position_size: HIGH=$3,500  MEDIUM=$3,000  LOW=$2,500

Return JSON only — no prose:
{
  "action": "PROPOSE" | "SKIP",
  "ticker": str,
  "entry_price": float | null,
  "target_price": float | null,
  "stop_loss": float | null,
  "position_size": float | null,
  "confidence": "HIGH" | "MEDIUM" | "LOW" | null,
  "evidence": [str],
  "skip_reason": str | null
}
""".strip()

INVESTIGATE_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_news",
        "description": "Check earnings blackout and recent headlines. Call first — skip immediately if blackout.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ticker_fundamentals",
        "description": (
            "One call for float/short-interest and previous-day high/low/close. "
            "squeeze_potential: true when float < 20M and short > 15%."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ticker_market_data",
        "description": (
            "Batch-fetch ATR, 20d avg volume, pre-market volume/conviction, intraday VWAP, "
            "RS vs SPY, ORB %, today_pct_change, and live price — all in one call. "
            "Check today_pct_change: > 4% = skip (extended). > 5% ATR = skip (noisy stop)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_position_history",
        "description": "Recent closed-trade win rate and avg P&L for this ticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days":   {"type": "integer", "description": "Look-back window in days (default 30)"},
            },
            "required": ["ticker"],
        },
    },
]


def _investigate_dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_news":                return get_news(inp["ticker"])
    if name == "get_ticker_fundamentals": return get_ticker_fundamentals(inp["ticker"])
    if name == "get_ticker_market_data":  return get_ticker_market_data(inp["ticker"])
    if name == "get_position_history":    return get_position_history(inp["ticker"], inp.get("days", 30))
    return {"error": f"unknown tool: {name}"}


def _investigate_ticker(
    ticker: str,
    context: dict,
    market_report: dict,
    tracer: TraceLogger,
) -> dict:
    """Run a per-ticker mini-agent. Called from a thread pool."""
    client = anthropic.Anthropic()
    is_caution = market_report.get("decision") == "CAUTION"

    def _dispatch_with_breaker(name: str, inp: dict) -> dict | list:
        result = _investigate_dispatch(name, inp)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(
                f"circuit breaker: {name} error for {ticker} — {result['error']}"
            )
        return result

    msg = (
        f"Investigate {ticker} for an intraday setup.\n"
        f"Scanner: score={context['score']}, "
        f"premarket_change={context['premarket_change_pct']:+.2f}%, "
        f"scanner_price=${context.get('scanner_price', '?')}\n"
        f"Market: {'CAUTION' if is_caution else 'GO'} day, "
        f"bias={market_report.get('bias', 'NEUTRAL')}, "
        f"max_positions={market_report.get('max_positions', 2)}\n"
        "Call all 4 tools in order, then return the JSON decision."
    )

    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_INVESTIGATE_SYSTEM,
        tools=INVESTIGATE_TOOL_SCHEMAS,
        initial_message=msg,
        dispatch=_dispatch_with_breaker,
        tracer=tracer,
        agent_name=f"research_{ticker}",
        max_turns=12,
        wall_clock_timeout_s=_TICKER_TIMEOUT_S,
    )
    return parse_json_response(text) or {}


# ── Phase 1: deterministic screening (no LLM needed) ─────────────────────────

def _screen_candidates(
    market_report: dict,
    sector_data: list[dict],
    rejected_tickers: list[str],
) -> list[dict]:
    """
    Select up to 4 tickers to investigate.
    Done deterministically so we save a full LLM call and 2-3 turns.
    """
    candidates = get_candidates(min_score=5)
    valid = [c for c in candidates if "error" not in c and c["ticker"] not in rejected_tickers]
    if not valid:
        return []

    tickers   = [c["ticker"] for c in valid]
    snapshots = get_premarket_snapshot(tickers)
    snap_map  = {s["ticker"]: s for s in snapshots if "error" not in s}

    is_caution    = market_report.get("decision") == "CAUTION"
    leading_etfs  = {s.get("etf", "") for s in (sector_data or []) if (s.get("change_pct") or 0) > 0}
    lagging_etfs  = {s.get("etf", "") for s in (sector_data or []) if (s.get("change_pct") or 0) < -0.5}

    scored = []
    for c in valid:
        ticker = c["ticker"]
        snap   = snap_map.get(ticker, {})
        score  = c["technical_score"]
        pct    = snap.get("premarket_change_pct") or 0

        if is_caution and (score < 7 or pct <= 0.3):
            continue

        scored.append({
            "ticker":               ticker,
            "score":                score,
            "premarket_change_pct": pct,
            "scanner_price":        snap.get("scanner_price"),
            "premarket_price":      snap.get("premarket_price"),
            "sector":               c.get("sector"),
        })

    # Sort: score first, premarket momentum second
    scored.sort(key=lambda x: (x["score"], x["premarket_change_pct"]), reverse=True)
    return scored[:_MAX_CANDIDATES]


# ── Public entry point ────────────────────────────────────────────────────────

def run_research_agent(
    tracer: TraceLogger,
    market_report: dict,
    params: StrategyParams,
    rejected_context: Optional[list[dict]] = None,
) -> dict:
    """
    Run the Research Agent pipeline.

    Phase 1 (deterministic, ~2s):
      Screen candidates via get_candidates + get_premarket_snapshot.
      Select up to max_positions tickers — no LLM call needed.

    Phase 2 (parallel, ~40-90s wall clock):
      Spawn one mini-agent per ticker in a thread pool (up to _MAX_CANDIDATES).
      Each mini-agent: 4 tools, 12 turn cap, 120s timeout.
      Parallel execution means 6 tickers take as long as 1.
      Post-investigation cap trims proposals to max_positions — more candidates
      means better selection, not more trades.
    """
    # Guard: no candidates
    candidates = get_candidates(min_score=5)
    valid = [c for c in candidates if "error" not in c]
    if not valid:
        tracer.log_decision("research", "no_candidates", detail={"raw": len(candidates)})
        return {"proposals": [], "skipped": [], "summary": "No scan candidates today."}

    try:
        sector_data = get_sector_rotation()
    except Exception:
        sector_data = []

    rejected_tickers = [r["ticker"] for r in (rejected_context or [])]

    tracer.start_agent_span("research")

    # Phase 1: deterministic screening
    selected = _screen_candidates(market_report, sector_data, rejected_tickers)
    if not selected:
        tracer.log_decision("research", "no_candidates_after_screen")
        return {"proposals": [], "skipped": [], "summary": "No candidates passed screening."}

    # Phase 2: parallel per-ticker investigation
    proposals: list[dict] = []
    skipped:   list[dict] = []

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(_investigate_ticker, s["ticker"], s, market_report, tracer): s["ticker"]
            for s in selected
        }
        try:
            for fut in as_completed(futures, timeout=_TOTAL_TIMEOUT_S - 30):
                ticker = futures[fut]
                try:
                    result = fut.result(timeout=5)
                    if result.get("action") == "PROPOSE":
                        proposals.append({
                            "ticker":        result["ticker"],
                            "entry_price":   result["entry_price"],
                            "target_price":  result["target_price"],
                            "stop_loss":     result["stop_loss"],
                            "position_size": result["position_size"],
                            "confidence":    result["confidence"],
                            "evidence":      result.get("evidence", []),
                        })
                    else:
                        skipped.append({
                            "ticker": ticker,
                            "reason": result.get("skip_reason") or "agent rejected",
                        })
                except Exception as e:
                    skipped.append({"ticker": ticker, "reason": f"investigation error: {e}"})
        except FuturesTimeout:
            for fut, ticker in futures.items():
                if not fut.done():
                    skipped.append({"ticker": ticker, "reason": "investigation timed out"})

    max_pos = market_report.get("max_positions") or 2
    # Sort proposals: HIGH > MEDIUM > LOW
    _conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    proposals.sort(key=lambda p: _conf_order.get(p.get("confidence", "LOW"), 2))
    proposals = proposals[:max_pos]

    summary = (
        f"Investigated {len(selected)} ticker(s) in parallel. "
        f"{len(proposals)} proposal(s), {len(skipped)} skipped."
    )
    return {"proposals": proposals, "skipped": skipped, "summary": summary}
