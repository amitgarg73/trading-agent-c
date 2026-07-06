from __future__ import annotations

import time

import anthropic

from agents.base import parse_json_response
from agents.tools.scanner_tools import (
    filter_and_rank,
    get_gap_ups,
    get_premarket_snapshot,
    get_scan_results,
    get_sector_leaders,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

# ── Design (Option B, 2026-07-06) ──────────────────────────────────────────────
# The scanner's data path is deterministic quant screening, so it runs in plain Python:
# gather (DB + Alpaca reads) -> merge/enrich/dedup -> filter_and_rank. That path cannot time
# out and costs no tokens. An LLM is used for exactly one thing it is good at: a final
# qualitative pick of the best thesis setups from the pre-ranked shortlist, plus a rationale.
# The LLM call is NON-CRITICAL — any failure falls back to the deterministic ranking, so a slow
# or failed model can never kill the trading day. This replaces the old 5-tool Haiku loop that
# spent ~90s hand-merging ~125 candidates and timed out (see git history / ce539d9).

_MODEL              = "claude-haiku-4-5-20251001"
_SELECT_MAX_TOKENS  = 1024
_MIN_SCORE_FLOOR    = 5     # regime rules never scan below score 5; floor bounds the universe
_SCAN_TOP_N         = 40    # cap the ranked-input set the merge/rank works over
_SNAPSHOT_CAP       = 40    # cap premarket enrichment breadth
_LLM_SHORTLIST      = 15    # how many pre-ranked candidates the LLM chooses among


_SELECT_SYSTEM = """
You are the selection step of a trading scanner. You are given a pre-screened, pre-ranked
shortlist of candidate tickers plus the market regime and today's leading sectors. The data
work (screening, enrichment, ranking) is already done.

Your job: pick the best thesis setups from the shortlist — never more than max_n — and say why
in one or two sentences. Prefer names aligned with the leading sectors and the market bias, and
favour real premarket momentum over a bare technical score. You may return fewer than max_n if
only a few are genuinely worth investigating.

Rules:
- Choose ONLY from the shortlist tickers. Never invent a ticker.
- Return JSON only, no prose before or after:
{
  "selected": [str, ...],          // tickers from the shortlist, best first, <= max_n
  "scan_rationale": str,           // 1-2 sentences on what drove the selection
  "signals_used": [str, ...]       // e.g. ["technical_score", "premarket_momentum", "sector_rotation"]
}
""".strip()


def _regime_bounds(market_report: dict) -> tuple[int, int, bool, str]:
    """
    Deterministic regime -> (min_score, max_n, caution_mode, regime_label), mirroring the old
    prompt's REGIME RULES. Drives both the screening thresholds and the ranking.
    """
    if (market_report.get("decision") or "").upper() == "CAUTION":
        return 7, 15, True, "caution"
    level = (market_report.get("vix_level") or "ELEVATED").upper()
    if level == "HIGH":
        return 7, 15, False, "high_vix"
    if level == "LOW":
        return 5, 25, False, "low_vix"
    return 5, 20, False, "elevated_vix"


def _timed(tracer: TraceLogger, name: str, inp: dict, fn):
    """Call a data tool and log it as a scanner tool_call so the trace stays informative."""
    t0 = time.monotonic()
    res = fn()
    tracer.log_tool_call("scanner", name, inp, res, latency_ms=int((time.monotonic() - t0) * 1000))
    return res


def _gather_and_rank(tracer: TraceLogger, market_report: dict) -> dict:
    """
    The deterministic critical path: gather the universe, enrich with premarket + gap-ups,
    merge/dedup, and rank. No LLM, no unbounded loop. Returns the ranked candidate list plus
    the regime bounds and the sector context for the selection step.
    """
    min_score, max_n, caution, regime = _regime_bounds(market_report)

    scan = _timed(tracer, "get_scan_results", {"min_score": min_score},
                  lambda: get_scan_results(min_score))[:_SCAN_TOP_N]
    tickers = [c["ticker"] for c in scan][:_SNAPSHOT_CAP]
    premarket = _timed(tracer, "get_premarket_snapshot", {"tickers": tickers},
                       lambda: get_premarket_snapshot(tickers))
    pm = {p["ticker"]: p for p in premarket if isinstance(p, dict) and p.get("ticker")}

    min_gap = 1.5 if caution else 2.0
    gaps = _timed(tracer, "get_gap_ups", {"min_gap_pct": min_gap},
                  lambda: get_gap_ups(min_gap))
    sectors = _timed(tracer, "get_sector_leaders", {"n": 5}, lambda: get_sector_leaders(5))

    # Merge: start with the scored universe, enrich with premarket by ticker, add gap-up movers
    # not already present (assign technical_score=6), dedup by ticker.
    merged: dict[str, dict] = {}
    for c in scan:
        tk = c.get("ticker")
        if not tk:
            continue
        merged[tk] = {
            "ticker":               tk,
            "technical_score":      c.get("technical_score", 0),
            "premarket_change_pct": (pm.get(tk) or {}).get("premarket_change_pct") or 0.0,
            "price":                c.get("price"),
            "sector":               c.get("sector"),
        }
    for g in gaps:
        tk = g.get("ticker") if isinstance(g, dict) else None
        if tk and tk not in merged:
            merged[tk] = {
                "ticker":               tk,
                "technical_score":      6,
                "premarket_change_pct": g.get("gap_pct") or 0.0,
                "price":                g.get("price"),
                "sector":               g.get("sector"),
            }

    ranked = filter_and_rank(
        candidates=list(merged.values()), max_n=max_n, min_score=min_score, caution_mode=caution,
    )
    return {
        "ranked":   ranked["candidates"],
        "dropped":  ranked.get("dropped_count", 0),
        "regime":   regime,
        "max_n":    max_n,
        "sectors":  sectors,
    }


def _select_message(shortlist: list[dict], sectors: list[dict], market_report: dict,
                    regime: str, max_n: int) -> str:
    lines = "\n".join(
        f"  {c['ticker']:6s} score={c.get('technical_score', 0)} "
        f"premkt={c.get('premarket_change_pct', 0.0):+.2f}% sector={c.get('sector') or '?'}"
        for c in shortlist
    )
    sect = ", ".join(
        f"{s.get('etf', '?')} {s.get('change_pct', 0):+.2f}%"
        for s in sectors if isinstance(s, dict)
    ) or "n/a"
    return (
        f"Market: decision={market_report.get('decision', 'GO')}, "
        f"bias={market_report.get('bias', 'NEUTRAL')}, regime={regime}, max_n={max_n}\n"
        f"Leading sectors: {sect}\n\n"
        f"Shortlist (pre-ranked, best first):\n{lines}\n\n"
        "Pick the best thesis setups (<= max_n) and return the JSON."
    )


def _llm_select(client: anthropic.Anthropic, tracer: TraceLogger, ranked: list[dict],
                sectors: list[dict], market_report: dict, regime: str, max_n: int) -> dict:
    """One bounded LLM call: qualitative pick + rationale over the pre-ranked shortlist."""
    shortlist = ranked[:_LLM_SHORTLIST]
    t0 = time.monotonic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=_SELECT_MAX_TOKENS,
        system=_SELECT_SYSTEM,
        messages=[{"role": "user", "content": _select_message(shortlist, sectors, market_report, regime, max_n)}],
    )
    latency = int((time.monotonic() - t0) * 1000)
    tracer.log_tokens("scanner", resp.usage)
    text = next((b.text for b in resp.content if hasattr(b, "text")), "")
    tracer.log_agent_message(
        "scanner", text, "completed",
        tokens_input=resp.usage.input_tokens, tokens_output=resp.usage.output_tokens,
        model=_MODEL, latency_ms=latency,
    )
    parsed = parse_json_response(text)
    return {
        "selected":  [t for t in (parsed.get("selected") or []) if isinstance(t, str)],
        "rationale": parsed.get("scan_rationale") or "",
        "signals":   [s for s in (parsed.get("signals_used") or []) if isinstance(s, str)],
    }


def _default_rationale(regime: str, n: int) -> str:
    return (f"Selected {n} top-ranked candidate(s) for the {regime} regime by technical score and "
            f"premarket momentum.")


def _empty_result(rationale: str, regime: str, status: str) -> dict:
    return {
        "candidates":     [],
        "n_returned":     0,
        "scan_rationale": rationale,
        "signals_used":   [],
        "regime":         regime,
        "dropped_count":  0,
        "scanner_status": status,
    }


def run_scanner_agent(
    tracer: TraceLogger,
    market_report: dict,
    params: StrategyParams,
) -> dict:
    """
    Run the Scanner (Option B: deterministic screening + one qualitative LLM pick).
    Returns {candidates[], n_returned, scan_rationale, signals_used, regime, dropped_count,
    scanner_status}. scanner_status: 'ok' (LLM pick used), 'llm_fallback' (deterministic ranking
    used because the LLM pick failed), 'error' (the deterministic data path itself failed).
    """
    client = anthropic.Anthropic()
    tracer.start_agent_span("scanner")

    # 1. Deterministic gather + rank — the critical path. Cannot time out; no tokens.
    try:
        g = _gather_and_rank(tracer, market_report)
    except Exception as e:
        tracer.log_error("scanner", f"scan/rank failed: {e}")
        return _empty_result(f"scanner scan/rank error: {e}", "unknown", "error")

    ranked, regime, max_n = g["ranked"], g["regime"], g["max_n"]
    if not ranked:
        tracer.log_decision(
            "scanner", "low_quality_halt",
            detail={"n_returned": 0, "regime": regime, "dropped_count": g["dropped"]},
        )
        return _empty_result("No candidates passed screening.", regime, "ok")

    # 2. Qualitative LLM pick — non-critical. Any failure falls back to the deterministic ranking,
    # so a slow or failed model can never kill the day.
    by_ticker = {c["ticker"]: c for c in ranked}
    try:
        pick     = _llm_select(client, tracer, ranked, g["sectors"], market_report, regime, max_n)
        selected = [by_ticker[t] for t in pick["selected"] if t in by_ticker][:max_n]
        if not selected:
            raise ValueError("LLM selected no valid tickers")
        rationale = pick["rationale"] or _default_rationale(regime, len(selected))
        signals   = pick["signals"] or ["technical_score", "premarket_momentum"]
        status    = "ok"
    except Exception as e:
        tracer.log_error("scanner", f"LLM selection failed, using deterministic ranking: {e}")
        selected  = ranked[:max_n]
        rationale = _default_rationale(regime, len(selected))
        signals   = ["technical_score", "premarket_momentum"]
        status    = "llm_fallback"

    tracer.log_decision(
        "scanner", "candidates_selected",
        detail={
            "n_returned":     len(selected),
            "regime":         regime,
            "dropped_count":  g["dropped"],
            "scan_rationale": rationale,
            "signals_used":   signals,
            "scanner_status": status,
        },
    )
    return {
        "candidates":     selected,
        "n_returned":     len(selected),
        "scan_rationale": rationale,
        "signals_used":   signals,
        "regime":         regime,
        "dropped_count":  g["dropped"],
        "scanner_status": status,
    }
