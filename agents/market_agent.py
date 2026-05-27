from __future__ import annotations

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.market_tools import (
    get_fear_greed,
    get_futures,
    get_sector_rotation,
    get_vix,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """
You are a macro market analyst. Your job is to assess today's market conditions
and recommend a position count ceiling for a day-trading system.

You have 4 tools available. Call all 4 before forming any view. Do not skip any.

After calling all tools, return a JSON object:
{
  "decision": "GO | CAUTION | SKIP",
  "max_positions": int,
  "bias": "BULLISH | BEARISH | NEUTRAL",
  "skip_reason": null,
  "summary": str
}

Decision rules:
- SKIP if avg_futures_change < -1.5%
- CAUTION if VIX > 20, or Fear&Greed < 25 with bearish futures confirmation
- GO otherwise
- max_positions scales with VIX: <20=15, 20-25=10, 25-30=5, 30-45=3, >45=2

Always respond with valid JSON only.
""".strip()

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_vix",
        "description": "Fetch current VIX index value and level classification.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_futures",
        "description": "Fetch pre-market futures % change for S&P500, Nasdaq, and Dow.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_fear_greed",
        "description": "Fetch CNN Fear & Greed index value and classification.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sector_rotation",
        "description": "Fetch 1-day % change for all 11 sector ETFs, sorted best to worst.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_vix":             return get_vix()
    if name == "get_futures":         return get_futures()
    if name == "get_fear_greed":      return get_fear_greed()
    if name == "get_sector_rotation": return get_sector_rotation()
    return {"error": f"unknown tool: {name}"}


def run_market_agent(tracer: TraceLogger, params: StrategyParams) -> dict:
    """
    Run Market Agent. Calls all 4 market data tools then synthesizes
    a market_report with decision, max_positions, bias, and summary.
    Returns the market_report dict.
    """
    tracer.start_agent_span("market")
    client = anthropic.Anthropic()
    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_SYSTEM,
        tools=TOOL_SCHEMAS,
        initial_message="Assess today's market conditions and return your market_report JSON.",
        dispatch=_dispatch,
        tracer=tracer,
        agent_name="market",
        max_turns=6,
    )
    return parse_json_response(text)
