from __future__ import annotations

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.learning_tools import (
    adjust_param,
    read_recent_learnings,
    read_session_context,
    read_strategy_params,
    read_today_trades,
    recommend_goal,
    write_learning,
)
from core.params import StrategyParams
from trace.logger import TraceLogger, traced_agent

# Haiku, not Sonnet (17 Aug 2026, Amit's call). This agent was 87% of the fleet's LLM spend —
# $12.89 of $14.88 over 30 days — while producing no output at all since 7 Aug (argus#583). Paying
# Sonnet rates for silence is the worst of both, so it runs on the cheap model until it is producing
# something worth paying more for.
#
# ⛔ THIS DOES NOT FIX THE SILENCE, and must not be read as having fixed it. argus#583 is a tool-loop
# failure, not a model choice; the fail-fast guard in run_tool_loop is what will name the cause on
# the next EOD. Changing the model here does mean that run tests two things at once, so if it fails
# again, separate them before concluding anything about Haiku.
#
# No free-tier option exists in this repo: every agent goes through anthropic.Anthropic() and the
# shared run_tool_loop, so a non-Anthropic provider would be a new client and a new tool-calling
# translation, not a model swap.
_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """
You are an EOD performance analyst for an autonomous trading system.

After each trading day you receive today's trade record, market context,
current strategy parameters, and recent learnings from the past 14 days.

Your job:
1. Analyze trade outcomes across 5 dimensions: entry quality, exit quality,
   market regime correlation, ticker patterns, parameter effectiveness.
2. Write findings to c_learnings using write_learning.
3. Adjust parameters using adjust_param only when ALL checks pass:
   - Sample size >= 3 matching trades
   - Not in cooldown
   - New value within [min_bound, max_bound]
   - At most 2 adjustments per day
   - No false positive for this parameter in last 30 days
4. Recommend new goals with recommend_goal only if a persistent winning
   pattern spans 5+ sessions. At most 1 recommendation per day.

NEVER call adjust_param for: daily_loss_limit, account_drawdown_thresholds,
session_time_limit, session_tool_cap, max_consecutive_losing_days.

After all tool calls, return a JSON summary:
{
  "session_date": "YYYY-MM-DD",
  "trades_analyzed": int,
  "win_rate": float,
  "total_pnl": float,
  "learnings_written": int,
  "params_adjusted": int,
  "goal_recommended": bool,
  "top_finding": str,
  "context_for_tomorrow": str
}
""".strip()

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_today_trades",
        "description": "Fetch all trades closed today for analysis.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_session_context",
        "description": "Fetch the session row (market context, token usage, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "read_strategy_params",
        "description": "Fetch all active strategy params with current values and bounds.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_recent_learnings",
        "description": "Fetch learnings written in the past N days, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Look-back window (default 14)"}},
            "required": [],
        },
    },
    {
        "name": "write_learning",
        "description": "Persist a structured learning or observation to c_learnings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "learning_type": {
                    "type": "string",
                    "enum": ["observation", "adjustment", "false_positive_detected", "goal_recommendation"],
                },
                "dimension": {
                    "type": "string",
                    "enum": ["entry_quality", "exit_quality", "market_regime", "ticker_pattern", "parameter"],
                },
                "finding":        {"type": "string"},
                "param_adjusted": {"type": "string"},
                "old_value":      {"type": "number"},
                "new_value":      {"type": "number"},
                "sample_size":    {"type": "integer"},
                "confidence":     {"type": "number"},
            },
            "required": ["learning_type", "dimension", "finding"],
        },
    },
    {
        "name": "adjust_param",
        "description": "Adjust a strategy parameter within its bounds and cooldown rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "param_name": {"type": "string"},
                "new_value":  {"type": "number"},
                "reason":     {"type": "string"},
            },
            "required": ["param_name", "new_value", "reason"],
        },
    },
    {
        "name": "recommend_goal",
        "description": "Write a goal recommendation (requires human approval to activate).",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_type":     {"type": "string"},
                "target_value":  {"type": "number"},
                "rationale":     {"type": "string"},
            },
            "required": ["goal_type", "target_value", "rationale"],
        },
    },
]


def _make_dispatch(session_id: str):
    """Return a dispatcher closure that injects session_id into write/adjust calls."""
    def dispatch(name: str, inp: dict):
        if name == "read_today_trades":    return read_today_trades()
        if name == "read_session_context": return read_session_context(inp["session_id"])
        if name == "read_strategy_params": return read_strategy_params()
        if name == "read_recent_learnings":
            return read_recent_learnings(inp.get("days", 14))
        if name == "write_learning":
            return write_learning(
                learning_type=inp["learning_type"],
                dimension=inp["dimension"],
                finding=inp["finding"],
                param_adjusted=inp.get("param_adjusted"),
                old_value=inp.get("old_value"),
                new_value=inp.get("new_value"),
                sample_size=inp.get("sample_size", 0),
                confidence=inp.get("confidence", 0.0),
                session_id=session_id,
            )
        if name == "adjust_param":
            return adjust_param(
                param_name=inp["param_name"],
                new_value=inp["new_value"],
                reason=inp["reason"],
                session_id=session_id,
            )
        if name == "recommend_goal":
            return recommend_goal(
                goal_type=inp["goal_type"],
                target_value=inp["target_value"],
                rationale=inp["rationale"],
                session_id=session_id,
            )
        return {"error": f"unknown tool: {name}"}
    return dispatch


@traced_agent("learner")
def run_learning_agent(
    tracer: TraceLogger,
    session_id: str,
    params: StrategyParams,
) -> dict:
    """
    Run Learning Agent after EOD reconciliation. Analyzes today's trades,
    writes structured learnings, and optionally adjusts parameters.
    Returns a summary dict with context_for_tomorrow.
    """
    tracer.start_agent_span("learner")
    client = anthropic.Anthropic()
    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_SYSTEM,
        tools=TOOL_SCHEMAS,
        initial_message=(
            f"Analyze today's trading session (session_id: {session_id}). "
            "Read today's trades, session context, current params, and recent learnings. "
            "Write your findings and return the summary JSON."
        ),
        dispatch=_make_dispatch(session_id),
        tracer=tracer,
        agent_name="learner",
        max_turns=20,
    )
    return parse_json_response(text)
