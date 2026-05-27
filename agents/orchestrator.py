from __future__ import annotations

import json
import time
from datetime import date
from typing import Optional

import anthropic

from agents.base import parse_json_response
from agents.market_agent import run_market_agent
from agents.research_agent import run_research_agent
from agents.risk_agent import run_risk_agent
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-sonnet-4-6"

_SYSTEM = """
You are a trading session coordinator. You receive reports from three specialized
agents and produce the final trade list for execution.

Do not second-guess the agents' findings. Your job is synthesis, not re-analysis.

DECISION RULES:
1. If market_report.decision == SKIP: return trades: [] immediately,
   terminal_reason = "skip_propagated".
2. Build approved_trades from proposals where risk_verdicts.verdict == APPROVED.
3. If len(approved_trades) == 0:
   - If ALL rejections are structural (daily loss limit, no capital, already in positions):
     return trades: [], terminal_reason = "structural_block".
   - If ANY rejection is fixable (sector concentration, count limit exceeded):
     set retry_needed = true.
4. If len(approved_trades) > 0: return them, terminal_reason = "converged".

For each approved trade compute:
- shares = floor(position_size / entry_price)
- estimated_profit = round((target_price - entry_price) * shares, 2)
- max_loss = round((entry_price - stop_loss) * shares, 2)
- reward_risk = round(estimated_profit / max_loss, 2) if max_loss > 0 else 0

Return JSON only — schema must match exactly:
{
  "date": "YYYY-MM-DD",
  "market_context": str,
  "trades": [{
    "ticker": str, "action": "BUY", "entry_price": float,
    "target_price": float, "stop_loss": float, "position_size": float,
    "shares": int, "confidence": str, "estimated_profit": float,
    "max_loss": float, "reward_risk": float, "reasoning": str
  }],
  "total_estimated_profit": float,
  "total_max_loss": float,
  "risk_note": str,
  "retry_needed": false,
  "session_meta": {
    "loop_iterations": int,
    "retry_triggered": bool,
    "retry_reason": null,
    "terminal_reason": str
  }
}
""".strip()


def _build_synthesis_message(
    market_report: dict,
    trade_proposals: dict,
    risk_verdicts: dict,
    loop_iteration: int,
) -> str:
    return (
        f"Session reports (loop iteration {loop_iteration}):\n\n"
        f"MARKET AGENT:\n{json.dumps(market_report, indent=2)}\n\n"
        f"RESEARCH AGENT:\n{json.dumps(trade_proposals, indent=2)}\n\n"
        f"RISK AGENT:\n{json.dumps(risk_verdicts, indent=2)}\n\n"
        "Produce the final trade list."
    )


def _run_synthesis_call(
    client: anthropic.Anthropic,
    market_report: dict,
    trade_proposals: dict,
    risk_verdicts: dict,
    tracer: TraceLogger,
    loop_iteration: int,
) -> dict:
    """Single synthesis call to Orchestrator Agent (no tools)."""
    user_msg = _build_synthesis_message(
        market_report, trade_proposals, risk_verdicts, loop_iteration
    )
    t0 = time.monotonic()
    response = client.messages.create(
        model=_MODEL,
        max_tokens=3000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    api_ms = int((time.monotonic() - t0) * 1000)
    tracer.log_tokens("orchestrator", response.usage)
    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    result = parse_json_response(text)
    tracer.log_agent_message(
        "orchestrator", text,
        result.get("session_meta", {}).get("terminal_reason", "synthesized"),
        tokens_input=response.usage.input_tokens,
        tokens_output=response.usage.output_tokens,
        model=_MODEL,
        latency_ms=api_ms,
    )
    return result


def _empty_session_output(market_report: dict, terminal_reason: str) -> dict:
    return {
        "date": date.today().isoformat(),
        "market_context": market_report.get("summary", ""),
        "trades": [],
        "total_estimated_profit": 0.0,
        "total_max_loss": 0.0,
        "risk_note": terminal_reason,
        "retry_needed": False,
        "session_meta": {
            "loop_iterations": 0,
            "retry_triggered": False,
            "retry_reason": None,
            "terminal_reason": terminal_reason,
        },
    }


def run_premarket_pipeline(
    tracer: TraceLogger,
    params: StrategyParams,
    news_signals: Optional[list[dict]] = None,
) -> dict:
    """
    Full premarket agent pipeline.
    Order: Market → Research → Risk → Orchestrator synthesis.
    One retry allowed if fixable rejections exist.
    Returns the final session output dict (retry_needed stripped).
    """
    client = anthropic.Anthropic()
    tracer.start_agent_span("orchestrator")

    # Step 1: Market Agent
    market_report = run_market_agent(tracer, params)
    if market_report.get("decision") == "SKIP":
        tracer.log_decision(
            "orchestrator", "skip_propagated",
            detail={"skip_reason": market_report.get("skip_reason")},
        )
        return _empty_session_output(market_report, "skip_propagated")

    # Step 2: Research Agent
    trade_proposals = run_research_agent(tracer, market_report, params)

    # Step 3: Risk Agent
    risk_verdicts = run_risk_agent(tracer, trade_proposals, params)

    # Step 4: Orchestrator synthesis
    result = _run_synthesis_call(
        client, market_report, trade_proposals, risk_verdicts, tracer, loop_iteration=1
    )

    if not result.get("retry_needed"):
        result.pop("retry_needed", None)
        tracer.log_decision(
            "orchestrator",
            result["session_meta"]["terminal_reason"],
            detail={"trades": len(result["trades"])},
        )
        return result

    # Step 5: One retry on fixable rejections
    rejected = [
        v for v in risk_verdicts.get("verdicts", [])
        if v.get("verdict") == "REJECTED"
    ]
    trade_proposals_retry = run_research_agent(
        tracer, market_report, params, rejected_context=rejected
    )
    risk_verdicts_retry = run_risk_agent(tracer, trade_proposals_retry, params)
    result = _run_synthesis_call(
        client, market_report, trade_proposals_retry, risk_verdicts_retry,
        tracer, loop_iteration=2
    )
    result.pop("retry_needed", None)
    result["session_meta"]["retry_triggered"] = True

    tracer.log_decision(
        "orchestrator",
        result["session_meta"]["terminal_reason"],
        detail={"trades": len(result["trades"]), "retry": True},
    )
    return result
