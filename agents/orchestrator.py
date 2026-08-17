from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import date
from subprocess import DEVNULL
from typing import Optional

import anthropic

from agents.base import parse_json_response
from agents.market_agent import run_market_agent
from agents.market_agent_v1 import run_market_agent as run_market_agent_v1
from agents.research_agent import run_research_agent
from agents.risk_agent import run_risk_agent
from agents.scanner_agent import run_scanner_agent
from core.params import StrategyParams
from evals.judge import evaluate_session_outputs
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
5. For each trade's reasoning field you MUST include BOTH:
   - Entry rationale: why this setup is valid now (catalyst, momentum, regime alignment)
   - Exit rationale: why stop_loss and target_price are at those specific levels
     Example: "stop at $174 = 2×ATR below entry per risk agent; target at $183 = prior
     resistance from April 14 at 1:2 R:R; do not adjust these levels."

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
    "max_loss": float, "reward_risk": float,
    "reasoning": str    // entry thesis + why stop_loss and target_price are at these levels
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
    tracer.log_tokens("orchestrator", response.usage, getattr(response, "model", None) or _MODEL)
    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    result = parse_json_response(text)
    # ⛔ THE SESSION'S OWN FORECAST, AS A SIGNAL AND NOT ONLY AS PROSE (argus#601).
    #
    # total_estimated_profit is what this session CLAIMS it will earn, computed per trade as
    # (target_price - entry_price) * shares. It lived only inside the JSON text blob above, so Provy
    # could never read it: its trace registry carried entry_price but not the estimate. With no claim
    # to compare against the settled realized_pnl, Provy fell back to forecasting outcomes from
    # layer-4 judge scores, which on this fleet do not separate wins from losses at all (held mean
    # 0.925, failed 0.946, measured over 73 settled outcomes).
    #
    # Emitted at SESSION grain to match how the outcome is reported: evals/outcomes.py posts one
    # realized_pnl per session, so a per-trade estimate would have nothing to reconcile against.
    #
    # Only numbers that are really numbers. A missing or unparseable estimate is left out rather than
    # sent as 0, because a claim of zero profit and no claim at all are different things.
    claim: dict = {}
    for key in ("total_estimated_profit", "total_max_loss"):
        val = result.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            claim[key] = float(val)

    tracer.log_agent_message(
        "orchestrator", text,
        result.get("session_meta", {}).get("terminal_reason", "synthesized"),
        tokens_input=response.usage.input_tokens,
        tokens_output=response.usage.output_tokens,
        model=_MODEL,
        latency_ms=api_ms,
        payload=claim or None,
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


def _fire_news_analyst(tickers: list[str], session_id: str) -> None:
    """Start the TypeScript news analyst as a fire-and-forget subprocess for Argus OTel observability."""
    try:
        ts_dir = os.path.join(os.path.dirname(__file__), "ts")
        payload = json.dumps({
            "tickers": tickers,
            "date": date.today().isoformat(),
            "session_id": session_id,
        })
        subprocess.Popen(
            ["node", "dist/news_analyst.js", payload],
            cwd=ts_dir,
            stdout=DEVNULL,
            stderr=DEVNULL,
            env=os.environ.copy(),
        )
    except Exception:
        pass  # observability demo — never block the pipeline


def run_premarket_pipeline(
    tracer: TraceLogger,
    params: StrategyParams,
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
    tracer.flush_cost_breakdown()
    if market_report.get("decision") == "SKIP":
        tracer.log_decision(
            "orchestrator", "skip_propagated",
            detail={"skip_reason": market_report.get("skip_reason")},
        )
        # Market signaled skip — downstream agents never ran
        for agent in ("scanner", "news", "research", "risk"):
            tracer.log_skip(agent, reason="market_skip", skip_type="error")
        # Still evaluate market agent quality on skip sessions
        _run_semantic_evals(
            tracer.session_id, market_report, {}, {}, {}, {"terminal_reason": "skip_propagated"},
        )
        out = _empty_session_output(market_report, "skip_propagated")
        out["_v2_market_report"] = market_report
        return out

    # Step 2: Scanner Agent — regime-aware ticker selection
    scanner_result = run_scanner_agent(tracer, market_report, params)
    tracer.flush_cost_breakdown()
    scanner_candidates = scanner_result.get("candidates") or []

    # Fire TypeScript news analyst for Argus OTel multi-language observability demo
    if scanner_candidates:
        _fire_news_analyst([c["ticker"] for c in scanner_candidates], tracer.session_id)

    if not scanner_candidates:
        # A scanner that errored is a real failure, not the benign "nothing to trade" routing.
        # Record it honestly so the session does not read as a clean skip (the mislabel that hid
        # three weeks of scanner timeouts). scanner_status is set by run_scanner_agent.
        scanner_failed = scanner_result.get("scanner_status") == "error"
        reason    = "scanner_error" if scanner_failed else "no_viable_candidates"
        skip_type = "error" if scanner_failed else "design"
        tracer.log_decision(
            "orchestrator", reason,
            detail={
                "scanner_rationale": scanner_result.get("scan_rationale", ""),
                "regime":            scanner_result.get("regime"),
                "scanner_status":    scanner_result.get("scanner_status"),
            },
        )
        # Downstream agents did not run. Propagate the failure type: a scanner error marks the
        # skipped agents as error-caused, not skipped-by-design.
        for agent in ("news", "research", "risk"):
            tracer.log_skip(agent, reason=reason, skip_type=skip_type)
        out = _empty_session_output(market_report, reason)
        out["_v2_market_report"] = market_report
        return out

    # Step 3: Research Agent — receives curated list from Scanner Agent
    trade_proposals = run_research_agent(
        tracer, market_report, params, candidates=scanner_candidates,
    )
    tracer.flush_cost_breakdown()

    if not trade_proposals.get("proposals"):
        tracer.log_decision(
            "orchestrator", "no_viable_proposals",
            detail={"summary": trade_proposals.get("summary", "")},
        )
        # Research found nothing viable — risk skipped by design
        tracer.log_skip("risk", reason="no_viable_proposals", skip_type="design")
        out = _empty_session_output(market_report, "no_viable_proposals")
        out["_v2_market_report"] = market_report
        return out

    # Step 4: Risk Agent
    risk_verdicts = run_risk_agent(tracer, trade_proposals, params)
    tracer.flush_cost_breakdown()

    # Step 5: Orchestrator synthesis
    result = _run_synthesis_call(
        client, market_report, trade_proposals, risk_verdicts, tracer, loop_iteration=1
    )
    tracer.flush_cost_breakdown()

    if not result.get("retry_needed"):
        result.pop("retry_needed", None)
        result["_v2_market_report"] = market_report
        tracer.log_decision(
            "orchestrator",
            result["session_meta"]["terminal_reason"],
            detail={"trades": len(result["trades"])},
        )
        _run_semantic_evals(tracer.session_id, market_report, scanner_result,
                            trade_proposals, risk_verdicts, result)
        return result

    # CAUTION days: suppress retry — market already signaled reduced risk appetite.
    # Allow synthesis to run (it may still find 1-2 viable trades at reduced size),
    # but do not loop again on CAUTION.
    if market_report.get("decision") == "CAUTION":
        result.pop("retry_needed", None)
        result["session_meta"]["terminal_reason"] = "caution_no_retry"
        result["_v2_market_report"] = market_report
        tracer.log_decision("orchestrator", "caution_no_retry", detail={"trades": len(result.get("trades", []))})
        _run_semantic_evals(tracer.session_id, market_report, scanner_result,
                            trade_proposals, risk_verdicts, result)
        return result

    # Step 5: One retry on fixable rejections.
    # If proposals remain after removing rejected tickers, skip re-running the
    # research agent — its findings are still valid. Pass the filtered proposals
    # directly to the risk agent instead.
    rejected = [
        v for v in risk_verdicts.get("verdicts", [])
        if v.get("verdict") == "REJECTED"
    ]
    rejected_tickers = {v["ticker"] for v in rejected}
    remaining_proposals = [
        p for p in trade_proposals.get("proposals", [])
        if p["ticker"] not in rejected_tickers
    ]

    if remaining_proposals:
        # Re-use first-run research results with rejected tickers removed.
        # No need to re-investigate tickers the research agent already analysed.
        trade_proposals_retry = {
            "proposals": remaining_proposals,
            "skipped": trade_proposals.get("skipped", []),
            "summary": (
                f"Retry (filtered): {len(remaining_proposals)} proposal(s) "
                f"after removing {len(rejected_tickers)} rejected ticker(s)."
            ),
        }
    else:
        # All proposals were rejected — re-synthesize with rejection reasons.
        # Pass same scanner candidates; rejected tickers filtered inside research agent.
        trade_proposals_retry = run_research_agent(
            tracer, market_report, params,
            candidates=scanner_candidates,
            rejected_context=rejected,
        )
        tracer.flush_cost_breakdown()

    risk_verdicts_retry = run_risk_agent(tracer, trade_proposals_retry, params)
    tracer.flush_cost_breakdown()
    result = _run_synthesis_call(
        client, market_report, trade_proposals_retry, risk_verdicts_retry,
        tracer, loop_iteration=2
    )
    tracer.flush_cost_breakdown()
    result.pop("retry_needed", None)
    result["_v2_market_report"] = market_report
    result["session_meta"]["retry_triggered"] = True

    tracer.log_decision(
        "orchestrator",
        result["session_meta"]["terminal_reason"],
        detail={"trades": len(result["trades"]), "retry": True},
    )

    _run_semantic_evals(tracer.session_id, market_report, scanner_result,
                        trade_proposals_retry, risk_verdicts_retry, result)
    return result


def _prepare_scanner_for_judge(r: dict) -> dict:
    """Put summary fields first so they survive the 3000-char judge window.

    Full candidates JSON pushes regime/scan_rationale/dropped_count past 3000 chars.
    Judge only needs the summary + top 5 candidates to evaluate criterion quality.
    """
    candidates = r.get("candidates") or []
    return {
        "regime":         r.get("regime"),
        "scan_rationale": r.get("scan_rationale"),
        "dropped_count":  r.get("dropped_count", 0),
        "n_returned":     r.get("n_returned", len(candidates)),
        "top_candidates": candidates[:5],
    }


def _prepare_risk_for_judge(r: dict) -> dict:
    """Put portfolio_state first; cap verdicts at 8 to avoid truncation."""
    verdicts = r.get("verdicts") or []
    return {
        "portfolio_state": r.get("portfolio_state"),
        "verdict_count":   len(verdicts),
        "verdicts":        verdicts[:8],
    }


def _run_semantic_evals(
    session_id: str,
    market_report: dict,
    scanner_result: dict,
    trade_proposals: dict,
    risk_verdicts: dict,
    orchestrator_result: dict,
) -> None:
    """
    Deprecated: L4 quality scoring moved to the canonical server-side judge, triggered at
    session close (evals.outcomes.trigger_server_judge). The server judge scores per ENTITY
    (per ticker) and writes the Outcome Ledger predictions, which this local collapsed judge
    could not do. Kept as a no-op so the existing call sites stay harmless; remove later.
    """
    return
