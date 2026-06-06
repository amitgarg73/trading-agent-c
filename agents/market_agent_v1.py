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


def check_circuit_breakers(vix_data: dict, futures_data: dict) -> tuple[bool, str | None]:
    """
    Hard SKIP conditions applied before any agent runs.
    These override Claude's judgment — they cannot be reasoned away.

    Breakers:
      - VIX > 35: volatility regime where ATR-based stops become noise
      - avg futures < -2.0%: gap-down open makes entries and fills unreliable
      - All three indices individually < -1.0%: coordinated market selloff
    """
    vix = vix_data.get("value", 0)
    if vix > 35:
        return True, f"vix_extreme: VIX={vix} > 35"

    avg_fut = futures_data.get("avg_change_pct", 0)
    if avg_fut < -2.0:
        return True, f"futures_crash: avg={avg_fut}% < -2.0%"

    sp = futures_data.get("S&P500", {}).get("change_pct", 0)
    nq = futures_data.get("Nasdaq",  {}).get("change_pct", 0)
    dw = futures_data.get("Dow",     {}).get("change_pct", 0)
    if sp < -1.0 and nq < -1.0 and dw < -1.0:
        return True, f"coordinated_selloff: SP={sp}%,NQ={nq}%,Dow={dw}%"

    return False, None

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
- CAUTION if VIX > 20, or Fear&Greed < 15 with bearish futures confirmation
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
    V1 market agent — runs in comparison mode only. Decision does not affect execution.
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
