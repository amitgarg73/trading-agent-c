from __future__ import annotations

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.market_agent_v1 import check_circuit_breakers
from agents.tools.market_tools import (
    get_economic_calendar,
    get_fear_greed,
    get_futures,
    get_sector_rotation,
    get_treasury_yields,
    get_vix,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """
You are a macro market analyst for a day-trading system. Assess today's conditions
and decide whether the system should trade, and at what scale.

Call all 6 tools before forming any view. Do not skip any.

After calling all tools, return a JSON object:
{
  "decision": "GO | CAUTION | SKIP",
  "max_positions": int,
  "bias": "BULLISH | BEARISH | NEUTRAL",
  "skip_reason": null or str,
  "confidence": "HIGH | MEDIUM | LOW",
  "key_factors": [str, str, str],
  "summary": str
}

Weigh all signals holistically. No single metric should mechanically decide the outcome.
Consider how signals interact:
- A high-impact economic event (Fed, CPI, NFP) changes the entire session risk profile.
  On those days, lean toward SKIP or deep CAUTION regardless of other indicators.
- Rising yields compound risk on high-VIX days — factor this into max_positions.
- Sector rotation tells you where conviction is flowing — use it to calibrate bias.
- max_positions should reflect your conviction: strong GO = up to 15, weak GO = 5-8.
- CAUTION means trade but reduced size; SKIP means no trades today.
- key_factors: top 3 specific reasons driving your decision (be concrete, not generic).

Hard circuit breakers are applied externally in Python before you run.
Within those bounds, your judgment is fully trusted.
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
    {
        "name": "get_economic_calendar",
        "description": (
            "Fetch today's US economic events. High-impact events (Fed, CPI, NFP, GDP) "
            "fundamentally change session risk — call this before forming any view."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_treasury_yields",
        "description": (
            "Fetch 10-year Treasury yield and 1-day change in basis points. "
            "Rising yields on high-VIX days compound momentum headwinds."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_vix":                return get_vix()
    if name == "get_futures":            return get_futures()
    if name == "get_fear_greed":         return get_fear_greed()
    if name == "get_sector_rotation":    return get_sector_rotation()
    if name == "get_economic_calendar":  return get_economic_calendar()
    if name == "get_treasury_yields":    return get_treasury_yields()
    return {"error": f"unknown tool: {name}"}


def _cb_skip(reason: str) -> dict:
    return {
        "decision":         "SKIP",
        "max_positions":    0,
        "bias":             "BEARISH",
        "skip_reason":      reason,
        "confidence":       "HIGH",
        "key_factors":      ["circuit_breaker"],
        "summary":          f"Circuit breaker: {reason}",
        "circuit_breaker":  reason,
    }


def run_market_agent(tracer: TraceLogger, params: StrategyParams) -> dict:
    """
    Primary market agent. Assesses macro conditions via 6 tools and returns a
    market_report that drives all downstream pipeline decisions.

    Circuit breakers are checked first in Python. If triggered, returns SKIP
    immediately without calling Claude.
    """
    vix_data     = get_vix()
    futures_data = get_futures()
    triggered, reason = check_circuit_breakers(vix_data, futures_data)
    if triggered:
        tracer.log_decision("market", "skip", detail={"circuit_breaker": reason})
        return _cb_skip(reason)

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
        max_turns=8,
    )
    result = parse_json_response(text)
    result.setdefault("circuit_breaker", None)
    result.setdefault("confidence", "MEDIUM")
    result.setdefault("key_factors", [])
    return result
