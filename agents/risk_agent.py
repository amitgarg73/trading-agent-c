from __future__ import annotations

import json

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.risk_tools import (
    get_buying_power,
    get_open_positions,
    get_portfolio_exposure,
    get_today_pnl,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """
You are a portfolio risk manager. Your job is to review proposed trades against
current portfolio constraints and approve or reject each one.

You have 4 tools. Call all 4 before reviewing any proposals. Do not skip any.

CONSTRAINTS (apply in order):
1. If today_pnl.limit_hit == true: reject ALL trades, reason = "daily loss limit hit"
2. If buying_power < proposal.position_size: reject — "insufficient capital"
3. Sector concentration: reject if adding a trade would push any sector above 35%
   of total_capital. Use get_portfolio_exposure to check current state.
4. Duplicate: reject if ticker already in open_positions
5. Position count: reject proposals beyond (max_positions - positions_open)
   where max_positions comes from market_report embedded in the proposals context

For each proposal: APPROVED or REJECTED with a reason that cites the specific
constraint(s) checked AND their concrete values, so the decision is auditable from
the verdict alone. Never use a generic reason like "all constraints passed".
- REJECTED: name the violated constraint with the numbers, e.g.
  "sector concentration would hit 38% > 35% cap" or
  "buying power $2,100 < position_size $3,000" or "daily P&L -$520 hit -$500 limit".
- APPROVED: cite the binding checks with their headroom, e.g.
  "BP $50,000 >= $3,500 needed; Technology 7% < 35% cap; P&L -$28.67 within -$500 limit".
You may not propose alternative trades or suggest modifications.

Return JSON only:
{
  "verdicts": [{
    "ticker": str,
    "verdict": "APPROVED|REJECTED",
    "reason": str
  }],
  "portfolio_state": {
    "buying_power": float,
    "positions_open": int,
    "today_pnl": float,
    "limit_hit": bool
  }
}
""".strip()

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_open_positions",
        "description": "Fetch currently open positions from the database.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_today_pnl",
        "description": "Fetch today's realized P&L and check if daily loss limit is hit.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_buying_power",
        "description": "Fetch available buying power and total capital deployed.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_portfolio_exposure",
        "description": "Fetch open position count, total deployed, and sector concentration.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_open_positions":     return get_open_positions()
    if name == "get_today_pnl":          return get_today_pnl()
    if name == "get_buying_power":       return get_buying_power()
    if name == "get_portfolio_exposure": return get_portfolio_exposure()
    return {"error": f"unknown tool: {name}"}


def run_risk_agent(
    tracer: TraceLogger,
    trade_proposals: dict,
    params: StrategyParams,
) -> dict:
    """
    Run Risk Agent. Calls all 4 portfolio tools then reviews each proposal
    against portfolio constraints. Returns risk_verdicts dict.
    """
    tracer.start_agent_span("risk")
    client = anthropic.Anthropic()
    user_msg = (
        "Proposed trades for today (from Research Agent):\n"
        f"{json.dumps(trade_proposals, indent=2)}\n\n"
        "Review against portfolio constraints and return your verdicts."
    )
    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_SYSTEM,
        tools=TOOL_SCHEMAS,
        initial_message=user_msg,
        dispatch=_dispatch,
        tracer=tracer,
        agent_name="risk",
        max_turns=6,
    )
    return parse_json_response(text)
