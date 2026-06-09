from __future__ import annotations

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.scanner_tools import (
    filter_and_rank,
    get_gap_ups,
    get_premarket_snapshot,
    get_scan_results,
    get_sector_leaders,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TURNS        = 12
_WALL_CLOCK_S     = 90


_SYSTEM = """
You are the Scanner Agent for an intraday trading system. Your job is to select
the best candidate tickers from today's universe scan for deeper investigation.

TOOL SEQUENCE (call all 5 in order):
1. get_scan_results(min_score)        — read today's scored universe from DB
2. get_premarket_snapshot(tickers)    — enrich candidates with Alpaca premarket quotes
3. get_gap_ups(min_gap_pct)          — get additional gap-up movers from Alpaca screener
4. get_sector_leaders(n=5)           — sector context for bias-aware selection
5. filter_and_rank(candidates, ...)   — apply quality threshold and dynamic N

REGIME RULES (apply based on vix_level in session_params):
- LOW vix (<15):      momentum regime. min_score=5, max_n=25. Prefer volume surge.
- ELEVATED vix (15-25): neutral regime. min_score=5, max_n=20. Balanced selection.
- HIGH vix (>25):     defensive regime. min_score=7, max_n=15. High-quality only.
- CAUTION decision:   override all — caution_mode=true, max_n=15 regardless of VIX.

MERGE LOGIC (before calling filter_and_rank):
- Start with scan_results (all tickers from get_scan_results).
- Enrich with premarket_change_pct from get_premarket_snapshot (match by ticker).
- Add gap-up movers from get_gap_ups that are NOT already in scan_results (assign
  technical_score=6 for gap-up-only tickers).
- Deduplicate by ticker.
- Pass the merged list to filter_and_rank.

Return JSON only — no prose before or after:
{
  "candidates": [
    {
      "ticker": str,
      "technical_score": int,
      "premarket_change_pct": float,
      "price": float | null,
      "sector": str | null
    }
  ],
  "n_returned": int,
  "scan_rationale": str,
  "signals_used": [str],
  "regime": "low_vix" | "elevated_vix" | "high_vix" | "caution",
  "dropped_count": int
}

ALL SIX TOP-LEVEL FIELDS ARE REQUIRED. Never omit any:
- regime: set from vix_level in session_params ("low_vix", "elevated_vix", "high_vix", or "caution" if caution_mode)
- scan_rationale: 1-2 sentences explaining what signals drove selection (e.g. "High premarket momentum stocks in leading sectors; dropped 14 low-score tickers.")
- dropped_count: total candidates removed by filter_and_rank (the tool returns this value directly)
Omitting these fields fails downstream quality checks and breaks the audit trail.
""".strip()


_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_scan_results",
        "description": (
            "Read today's technical scores from c_scan_results. "
            "Call first — provides the base universe ranked by score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_score": {
                    "type":        "integer",
                    "description": "Minimum technical score to include (1-10). Default 1 to get all.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_premarket_snapshot",
        "description": (
            "Batch-fetch Alpaca premarket quote data for the given tickers. "
            "Returns premarket_change_pct and premarket_price. Call after get_scan_results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type":        "array",
                    "items":       {"type": "string"},
                    "description": "List of ticker symbols from scan_results.",
                }
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "get_gap_ups",
        "description": (
            "Fetch Alpaca market screener movers gapping >= min_gap_pct. "
            "Universe-filtered. Add these to the candidate list if not already present."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_gap_pct": {
                    "type":        "number",
                    "description": "Minimum gap % (e.g. 2.0). Use 1.5 on CAUTION days.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_sector_leaders",
        "description": "Return top N sector ETFs by 1-day performance for regime context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type":        "integer",
                    "description": "Number of top sectors to return. Default 5.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "filter_and_rank",
        "description": (
            "Apply quality threshold and dynamic N to the merged candidate list. "
            "Call last, after merging scan + gap-up candidates with premarket data. "
            "Returns final {candidates[], n_returned, dropped_count}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type":        "array",
                    "description": "Merged, enriched candidate list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker":               {"type": "string"},
                            "technical_score":      {"type": "number"},
                            "premarket_change_pct": {"type": "number"},
                            "price":                {"type": ["number", "null"]},
                            "sector":               {"type": ["string", "null"]},
                        },
                        "required": ["ticker"],
                    },
                },
                "max_n": {
                    "type":        "integer",
                    "description": "Maximum candidates to return. Regime-dependent.",
                },
                "min_score": {
                    "type":        "integer",
                    "description": "Minimum technical_score. Use 7 for HIGH VIX or CAUTION.",
                },
                "caution_mode": {
                    "type":        "boolean",
                    "description": "True when market decision is CAUTION. Overrides to min_score=7, max_n=15.",
                },
            },
            "required": ["candidates", "max_n", "min_score", "caution_mode"],
        },
    },
]


def _dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_scan_results":
        return get_scan_results(inp.get("min_score", 1))
    if name == "get_premarket_snapshot":
        return get_premarket_snapshot(inp.get("tickers", []))
    if name == "get_gap_ups":
        return get_gap_ups(inp.get("min_gap_pct", 2.0))
    if name == "get_sector_leaders":
        return get_sector_leaders(inp.get("n", 5))
    if name == "filter_and_rank":
        return filter_and_rank(
            candidates=inp.get("candidates", []),
            max_n=inp.get("max_n", 25),
            min_score=inp.get("min_score", 5),
            caution_mode=inp.get("caution_mode", False),
        )
    return {"error": f"unknown tool: {name}"}


def _build_message(market_report: dict, params: StrategyParams) -> str:
    vix   = market_report.get("vix_value") or "unknown"
    level = market_report.get("vix_level") or "ELEVATED"
    dec   = market_report.get("decision", "GO")
    bias  = market_report.get("bias", "NEUTRAL")
    max_p = market_report.get("max_positions", params.max_positions)
    return (
        f"Session params:\n"
        f"  decision={dec}, vix={vix} ({level}), bias={bias}, max_positions={max_p}\n"
        f"  strategy_min_score={params.strategy_min_score}\n\n"
        "Call all 5 tools in order, then return the JSON candidate list."
    )


def run_scanner_agent(
    tracer: TraceLogger,
    market_report: dict,
    params: StrategyParams,
) -> dict:
    """
    Run the Scanner Agent (claude-haiku-4-5).
    Returns {candidates[], n_returned, scan_rationale, signals_used, regime, dropped_count}.
    On any failure returns an empty candidates dict so the pipeline exits cleanly.
    """
    client = anthropic.Anthropic()
    tracer.start_agent_span("scanner")

    try:
        text = run_tool_loop(
            client=client,
            model=_MODEL,
            system=_SYSTEM,
            tools=_TOOL_SCHEMAS,
            initial_message=_build_message(market_report, params),
            dispatch=_dispatch,
            tracer=tracer,
            agent_name="scanner",
            max_turns=_MAX_TURNS,
            wall_clock_timeout_s=_WALL_CLOCK_S,
        )
        result = parse_json_response(text)
        n = result.get("n_returned", len(result.get("candidates", [])))
        tracer.log_decision(
            "scanner",
            "candidates_selected" if n > 0 else "low_quality_halt",
            detail={
                "n_returned":    n,
                "regime":        result.get("regime"),
                "dropped_count": result.get("dropped_count", 0),
                "scan_rationale": result.get("scan_rationale"),
                "signals_used":   result.get("signals_used"),
            },
        )
        return result

    except Exception as e:
        tracer.log_error("scanner", str(e))
        return {
            "candidates":    [],
            "n_returned":    0,
            "scan_rationale": f"scanner_agent error: {e}",
            "signals_used":  [],
            "regime":        "unknown",
            "dropped_count": 0,
        }
