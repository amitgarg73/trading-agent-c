from __future__ import annotations

import json
from typing import Optional

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.research_tools import (
    get_atr,
    get_candidates,
    get_intraday_signals,
    get_live_price,
    get_news,
    get_position_history,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-sonnet-4-6"

_SYSTEM = """
You are a quantitative stock analyst. Your job is to identify the best intraday
trading setups from today's universe and propose specific trades.

You receive today's market conditions as context. Use them to calibrate your
selectivity — on CAUTION days, only the strongest setups qualify.

PHASE 1 — SCREEN
Call get_candidates() once. You receive ticker, score, and price only.
Read the scores. Choose at most 5 tickers to investigate further.
Prefer high scores (7+). Do not call any other tools yet.

PHASE 2 — INVESTIGATE
For each chosen ticker, call tools to build your evidence:
- get_news: REQUIRED first. If blackout: true, drop this ticker immediately.
- get_intraday_signals: is it above VWAP? outperforming SPY?
  If response has available: false, you are running pre-market. VWAP and RS
  data do not exist yet. Use score, ATR, news, and position history instead.
- get_live_price: is the price still near the expected entry?
- get_atr: is ATR compatible with a 0.67% stop? (above 5% is usually not)
- get_position_history: has this ticker worked recently?

PROPOSAL RULES
- target_price = round(entry_price * 1.04, 2)
- stop_loss = round(entry_price * 0.9933, 2)
- position_size: HIGH=$3,500  MEDIUM=$3,000  LOW=$2,500
- confidence HIGH: score >=7, above_vwap, rs_vs_spy >= 1.5
  (pre-market: score >=8, strong ATR profile, clean news)
- confidence MEDIUM: score 5-6, or above_vwap with rs_vs_spy >= 0.8
  (pre-market: score 6-7, no blackout, ATR in range)
- confidence LOW: score 5-6, mixed signals
- max proposals = market_report.max_positions
- On CAUTION days: only propose tickers with score >= 7 and above_vwap = true
  (pre-market CAUTION: score >= 8 only)

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
        "description": "Fetch today's scan results with scores. Call once at the start.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_score": {"type": "integer", "description": "Minimum technical score (default 5)"},
            },
            "required": [],
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
    if name == "get_candidates":        return get_candidates(inp.get("min_score", 5))
    if name == "get_news":              return get_news(inp["ticker"])
    if name == "get_live_price":        return get_live_price(inp["ticker"])
    if name == "get_intraday_signals":  return get_intraday_signals(inp["ticker"])
    if name == "get_atr":               return get_atr(inp["ticker"])
    if name == "get_position_history":  return get_position_history(inp["ticker"], inp.get("days", 30))
    return {"error": f"unknown tool: {name}"}


def _build_user_message(
    market_report: dict,
    rejected_context: Optional[list[dict]] = None,
) -> str:
    msg = (
        "Today's market conditions (from Market Agent):\n"
        f"{json.dumps(market_report, indent=2)}\n\n"
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
    Run Research Agent. Screens candidates, investigates up to 5 tickers,
    returns trade_proposals dict. On retry, receives rejected_context.
    """
    candidates = get_candidates(min_score=5)
    valid = [c for c in candidates if "error" not in c]
    if not valid:
        tracer.log_decision("research", "no_candidates", detail={"raw": len(candidates)})
        return {"proposals": [], "skipped": [], "summary": "No scan candidates today."}

    tracer.start_agent_span("research")
    client = anthropic.Anthropic()
    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_SYSTEM,
        tools=TOOL_SCHEMAS,
        initial_message=_build_user_message(market_report, rejected_context),
        dispatch=_dispatch,
        tracer=tracer,
        agent_name="research",
        max_turns=30,
    )
    return parse_json_response(text)
