from __future__ import annotations

import json
from typing import Optional

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.market_tools import get_sector_rotation
from agents.tools.research_tools import (
    get_atr,
    get_candidates,
    get_float_short_interest,
    get_intraday_signals,
    get_live_price,
    get_news,
    get_position_history,
    get_premarket_snapshot,
    get_premarket_volume,
    get_prev_day_levels,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-sonnet-4-6"
_WALL_CLOCK_TIMEOUT_S = 240  # 4-minute hard cap on the full tool loop

_SYSTEM = """
You are a quantitative stock analyst. Your job is to identify the best intraday
trading setups from today's universe and propose specific trades.

You receive today's market conditions and sector rotation as context.
Use them to calibrate your selectivity — on CAUTION days, only the strongest
setups qualify.

PHASE 1 — SCREEN AND SNAPSHOT
1. Call get_candidates() once. You receive ticker, score, and price.
2. Call get_premarket_snapshot() with ALL tickers returned. This shows
   overnight price moves vs yesterday's close in one call.
3. Using score AND premarket_change_pct together, select at most 10 tickers
   to investigate. Prefer score >= 7 with positive pre-market movement.
   On CAUTION days: score >= 8 AND premarket_change_pct > 0.3% only.

SECTOR CONTEXT
Today's sector rotation is included in your market context above.
Prefer tickers in sectors with positive 1-day change.
Deprioritize tickers in sectors down more than 0.5% — even high scores
in weak sectors are lower-probability setups.

PHASE 2 — INVESTIGATE
For each chosen ticker, call tools in this order:
- get_news: REQUIRED first. If blackout: true, drop immediately.
- get_float_short_interest: flag squeeze setups. squeeze_potential: true on a
  pre-market gap up = highest-priority setup regardless of score.
- get_prev_day_levels: where is current price vs PDH/PDL?
  Above PDH = breakout setup (bullish). Below PDL = distribution (avoid).
  Entry near PDH with positive pre-market momentum is the strongest signal.
- get_premarket_volume: HIGH conviction confirms the pre-market move is real.
  LOW conviction on a big pre-market move = likely fades at open, downgrade confidence.
- get_intraday_signals: VWAP and RS vs SPY. Returns available: false pre-market —
  use score + pre-market move + PDH/PDL position instead.
- get_live_price: confirm price is still near expected entry.
- get_atr: ATR > 5% = skip. Our 0.67% stop gets hit by normal noise.
- get_position_history: how has this ticker performed for us recently?

PROPOSAL RULES
- target_price = round(entry_price * 1.04, 2)
- stop_loss = round(entry_price * 0.9933, 2)
- position_size: HIGH=$3,500  MEDIUM=$3,000  LOW=$2,500
- confidence HIGH: score >=7, above_vwap, rs_vs_spy >= 1.5
  (pre-market: score >=8, premarket_change_pct > 0.5%, clean ATR and news)
- confidence MEDIUM: score 5-6, or above_vwap with rs_vs_spy >= 0.8
  (pre-market: score 6-7, positive premarket move, no blackout)
- confidence LOW: score 5-6, mixed signals
- max proposals = market_report.max_positions
- On CAUTION days: score >= 7 and above_vwap = true required
  (pre-market CAUTION: score >= 8 and premarket_change_pct > 0.3%)

Return JSON only:
{
  "proposals": [{
    "ticker": str, "entry_price": float, "target_price": float,
    "stop_loss": float, "position_size": float,
    "confidence": "HIGH|MEDIUM|LOW", "evidence": [str]
  }],
  "skipped": [{"ticker": str, "reason": str}],
  "summary": str
}
""".strip()

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_candidates",
        "description": "Fetch today's scan results with scores. Call once at the start of Phase 1.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_score": {"type": "integer", "description": "Minimum technical score (default 5)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_premarket_snapshot",
        "description": (
            "Fetch current pre-market quotes and overnight % change for a list of tickers "
            "in one batch call. Call immediately after get_candidates() with all returned tickers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All ticker symbols from get_candidates",
                },
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "get_float_short_interest",
        "description": (
            "Fetch float shares (millions), short % of float, and days-to-cover. "
            "squeeze_potential: true when float < 20M and short > 15% — highest priority on gap-ups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_prev_day_levels",
        "description": (
            "Return previous day's high (PDH), low (PDL), and close. "
            "Entry above PDH on volume = breakout. Entry below PDL = avoid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_premarket_volume",
        "description": (
            "Pre-market volume (4 AM-9:25 AM ET) as % of 20-day avg daily volume. "
            "HIGH (>= 15%) confirms institutional conviction. LOW (< 5%) = move may fade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_news",
        "description": "Check earnings blackout and recent headlines for a ticker.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_live_price",
        "description": "Fetch best available current price for a ticker.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_intraday_signals",
        "description": "Compute VWAP position, relative strength vs SPY, and today's % change.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_atr",
        "description": "Compute 14-day ATR as % of price and opening range breakout %.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_position_history",
        "description": "Fetch recent trade history for a ticker from the database.",
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


def _dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_candidates":           return get_candidates(inp.get("min_score", 5))
    if name == "get_premarket_snapshot":   return get_premarket_snapshot(inp["tickers"])
    if name == "get_float_short_interest": return get_float_short_interest(inp["ticker"])
    if name == "get_prev_day_levels":      return get_prev_day_levels(inp["ticker"])
    if name == "get_premarket_volume":     return get_premarket_volume(inp["ticker"])
    if name == "get_news":                 return get_news(inp["ticker"])
    if name == "get_live_price":           return get_live_price(inp["ticker"])
    if name == "get_intraday_signals":     return get_intraday_signals(inp["ticker"])
    if name == "get_atr":                  return get_atr(inp["ticker"])
    if name == "get_position_history":     return get_position_history(inp["ticker"], inp.get("days", 30))
    return {"error": f"unknown tool: {name}"}


def _build_user_message(
    market_report: dict,
    sector_rotation: Optional[list[dict]] = None,
    rejected_context: Optional[list[dict]] = None,
) -> str:
    sector_block = ""
    if sector_rotation and not (len(sector_rotation) == 1 and "error" in sector_rotation[0]):
        leading = [s for s in sector_rotation if (s.get("change_pct") or 0) > 0][:3]
        lagging = [s for s in sector_rotation if (s.get("change_pct") or 0) < 0][-3:]
        if leading or lagging:
            sector_block = "\n\nSector rotation (1-day):\n"
            if leading:
                sector_block += "  Leading: " + "  ".join(
                    f"{s['etf']} {s.get('name', s['etf'])} {s['change_pct']:+.1f}%" for s in leading
                ) + "\n"
            if lagging:
                sector_block += "  Lagging: " + "  ".join(
                    f"{s['etf']} {s.get('name', s['etf'])} {s['change_pct']:+.1f}%" for s in lagging
                )

    msg = (
        "Today's market conditions (from Market Agent):\n"
        f"{json.dumps(market_report, indent=2)}"
        f"{sector_block}\n\n"
    )

    if rejected_context:
        tickers = [r["ticker"] for r in rejected_context]
        reasons = "\n".join(f"  {r['ticker']}: {r['reason']}" for r in rejected_context)
        msg += (
            "Previous proposals were rejected by the risk review:\n"
            f"{reasons}\n\n"
            f"Investigate new candidates. Avoid: {tickers}.\n"
            "Return alternative proposals.\n"
        )
    else:
        msg += "Now investigate candidates and return trade proposals."
    return msg


def run_research_agent(
    tracer: TraceLogger,
    market_report: dict,
    params: StrategyParams,
    rejected_context: Optional[list[dict]] = None,
) -> dict:
    """
    Run Research Agent. Screens candidates via pre-market snapshot and sector
    context, investigates up to 5 tickers, returns trade_proposals dict.
    On retry, receives rejected_context.
    """
    candidates = get_candidates(min_score=5)
    valid = [c for c in candidates if "error" not in c]
    if not valid:
        tracer.log_decision("research", "no_candidates", detail={"raw": len(candidates)})
        return {"proposals": [], "skipped": [], "summary": "No scan candidates today."}

    try:
        sector_data = get_sector_rotation()
    except Exception:
        sector_data = []

    tracer.start_agent_span("research")
    client = anthropic.Anthropic()
    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_SYSTEM,
        tools=TOOL_SCHEMAS,
        initial_message=_build_user_message(market_report, sector_data, rejected_context),
        dispatch=_dispatch,
        tracer=tracer,
        agent_name="research",
        max_turns=60,
        wall_clock_timeout_s=_WALL_CLOCK_TIMEOUT_S,
    )
    return parse_json_response(text)
