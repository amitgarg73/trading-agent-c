from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from typing import Optional

import anthropic

from agents.base import parse_json_response, run_tool_loop
from agents.tools.market_tools import get_sector_rotation
from agents.tools.research_tools import (
    batch_fetch_news,
    get_candidates,
    get_position_history,
    get_premarket_snapshot,
    get_ticker_fundamentals,
    get_ticker_market_data,
)
from core.params import StrategyParams
from trace.logger import TraceLogger

_MODEL = "claude-sonnet-4-6"

# Per-ticker agent cap: 4 tools × ~15s each + reasoning = ~90s is plenty
_TICKER_TIMEOUT_S  = 120
# Outer cap covers screening + parallel investigation + some buffer
_TOTAL_TIMEOUT_S   = 360
# Investigate more candidates than max_positions so we can select the best
_MAX_CANDIDATES    = 6


# ── Per-ticker investigation ──────────────────────────────────────────────────

_INVESTIGATE_SYSTEM = """
You are investigating a single stock for an intraday trading setup.
News context is provided in the initial message — do NOT call any news tool.
Call ALL THREE tools in this order, then output your decision.

TOOL ORDER (mandatory):
1. get_ticker_fundamentals — PDH/PDL levels (float/short unavailable via Alpaca).
2. get_ticker_market_data  — ATR, volume conviction, VWAP, RS, ORB, live price.
3. get_position_history    — recent win rate on this ticker.

SKIP RULES (return SKIP if ANY apply):
- News context: blackout is true
- get_ticker_market_data: atr_pct > 10 (stop too noisy — raised from 5 to match scanner)
- get_ticker_market_data: today_pct_change > 8 (already extended — raised from 4; sector leaders can run 5-8% and still have upside)
- get_ticker_market_data: available=true AND live_price is null (intraday data missing — real problem)

PRE-MARKET DATA RULE (critical — read carefully):
If get_ticker_market_data returns available=false, that means it is the pre-market period.
This is NORMAL and EXPECTED. Do NOT skip for this reason.
When available=false: live_price, VWAP, RS, ORB, today_pct_change are all null — that is fine.
Use premarket_change_pct from the scanner context as your primary signal instead.
ATR is still returned and valid even when available=false — use it for stop sizing.

POSITION HISTORY RULE:
If trade_count < 5, the win_rate is too small a sample to be meaningful. Ignore it entirely.
Only factor win_rate into your decision when trade_count >= 5.

CONFIDENCE RULES:
- HIGH:   score >= 7, above_vwap true, rs_vs_spy >= 1.5, today_pct_change <= 2
  (pre-market: score >= 7, premarket_change_pct > 0.5, conviction HIGH or MODERATE)
- MEDIUM: score 5-6, above_vwap with rs_vs_spy >= 0.8, today_pct_change <= 4
  (pre-market: score 6-7, positive premarket_change,
   conviction MODERATE or higher,
   OR conviction LOW with above_vwap true AND rs_vs_spy >= 1.0)
- LOW:    score 5-6, mixed signals, or today_pct_change 2-4
  conviction LOW is acceptable at LOW confidence if above_vwap true OR rs_vs_spy >= 0.8
- Skip if nothing reaches LOW.

On CAUTION days (passed in context): require score >= 7 and above_vwap true.

CONVICTION NOTE: In low-volatility or Fear-regime markets, premarket volume is
structurally thin across the board. conviction LOW does not mean the setup is weak —
it means institutions are waiting. When price action confirms (above VWAP, positive RS),
treat conviction LOW the same as MODERATE for the purpose of MEDIUM confidence.

PROPOSAL RULES:
- entry_price  = live_price (or premarket_price if pre-market)
- target_price = round(entry_price * 1.08, 2)
- stop_loss: use atr_pct from get_ticker_market_data.
  stop_pct = max(atr_pct * 0.8, 0.5) / 100
  stop_loss = round(entry_price * (1 - stop_pct), 2)
  (0.8× ATR, minimum 0.5% floor — survives normal intraday noise)
- position_size: HIGH=$3,500  MEDIUM=$3,000  LOW=$2,500

Return JSON only — no prose:
{
  "action": "PROPOSE" | "SKIP",
  "ticker": str,
  "entry_price": float | null,
  "target_price": float | null,
  "stop_loss": float | null,
  "position_size": float | null,
  "confidence": "HIGH" | "MEDIUM" | "LOW" | null,
  "evidence": [str],
  "skip_reason": str | null
}
""".strip()

INVESTIGATE_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_ticker_fundamentals",
        "description": (
            "Fetch previous-day high/low/close from Alpaca snapshot. "
            "Float/short interest not available — those fields return None."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ticker_market_data",
        "description": (
            "Batch-fetch ATR, 20d avg volume, pre-market volume/conviction, intraday VWAP, "
            "RS vs SPY, ORB %, today_pct_change, and live price — all in one call. "
            "Check today_pct_change: > 4% = skip (extended). > 5% ATR = skip (noisy stop)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_position_history",
        "description": "Recent closed-trade win rate and avg P&L for this ticker.",
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


def _investigate_dispatch(name: str, inp: dict) -> dict | list:
    if name == "get_ticker_fundamentals": return get_ticker_fundamentals(inp["ticker"])
    if name == "get_ticker_market_data":  return get_ticker_market_data(inp["ticker"])
    if name == "get_position_history":    return get_position_history(inp["ticker"], inp.get("days", 30))
    return {"error": f"unknown tool: {name}"}


def _investigate_ticker(
    ticker: str,
    context: dict,
    market_report: dict,
    tracer: TraceLogger,
    news_context: dict | None = None,
) -> dict:
    """Run a per-ticker mini-agent. Called from a thread pool."""
    client = anthropic.Anthropic()
    is_caution = market_report.get("decision") == "CAUTION"

    news_summary = ""
    if news_context:
        headlines = news_context.get("headlines", [])
        blackout  = news_context.get("blackout", False)
        reason    = news_context.get("reason", "")
        news_summary = (
            f"News context (pre-fetched): blackout={blackout}"
            + (f", reason={reason}" if reason else "")
            + (f", headlines={headlines[:3]}" if headlines else ", no headlines")
            + "\n"
        )

    open_ctx = market_report.get("open_positions_context", "")
    msg = (
        f"Investigate {ticker} for an intraday setup.\n"
        + news_summary
        + (f"{open_ctx}\n" if open_ctx else "")
        + f"Scanner: score={context['score']}, "
        f"premarket_change={context['premarket_change_pct']:+.2f}%, "
        f"scanner_price=${context.get('scanner_price', '?')}\n"
        f"Market: {'CAUTION' if is_caution else 'GO'} day, "
        f"bias={market_report.get('bias', 'NEUTRAL')}, "
        f"max_positions={market_report.get('max_positions', 2)}\n"
        "Call all 3 tools in order, then return the JSON decision."
    )

    text = run_tool_loop(
        client=client,
        model=_MODEL,
        system=_INVESTIGATE_SYSTEM,
        tools=INVESTIGATE_TOOL_SCHEMAS,
        initial_message=msg,
        dispatch=_investigate_dispatch,
        tracer=tracer,
        agent_name=f"research_{ticker}",
        max_turns=10,
        wall_clock_timeout_s=_TICKER_TIMEOUT_S,
    )
    return parse_json_response(text) or {}


# ── Phase 1: deterministic screening (no LLM needed) ─────────────────────────

def _compute_composite_score(
    baseline: int,
    gap_pct: float,
    sector_etf_pct: float | None,
    rs_vs_spy: float | None = None,
    above_vwap: bool | None = None,
    orb_pct: float | None = None,
) -> int:
    """
    Composite score = baseline (scanner) + premarket momentum + sector ETF + intraday signals.

    Pre-market additions (max +5):
      gap >= 3%: +3   gap >= 1.5%: +2   gap >= 0.5%: +1
      sector ETF >= 2%: +2   sector ETF >= 0.75%: +1

    Intraday additions (max +5, only when rs_vs_spy/above_vwap available):
      RS vs SPY >= 2.0: +3   >= 1.0: +2   >= 0.3: +1   <= -1.0: -1
      above VWAP: +1
      above ORB high: +1
    """
    score = baseline

    # Gap bonus
    if gap_pct >= 3.0:
        score += 3
    elif gap_pct >= 1.5:
        score += 2
    elif gap_pct >= 0.5:
        score += 1
    elif gap_pct <= -1.0:
        score -= 1

    # Sector ETF momentum
    if sector_etf_pct is not None:
        if sector_etf_pct >= 2.0:
            score += 2
        elif sector_etf_pct >= 0.75:
            score += 1

    # Intraday signals (only present during intraday scan path)
    if rs_vs_spy is not None:
        if rs_vs_spy >= 2.0:
            score += 3
        elif rs_vs_spy >= 1.0:
            score += 2
        elif rs_vs_spy >= 0.3:
            score += 1
        elif rs_vs_spy <= -1.0:
            score -= 1

    if above_vwap is True:
        score += 1

    if orb_pct is not None and orb_pct >= 0:
        score += 1

    return score


def _screen_candidates(
    market_report: dict,
    sector_data: list[dict],
    rejected_tickers: list[str],
    min_score: int = 5,
    is_intraday: bool = False,
) -> list[dict]:
    """
    Select up to _MAX_CANDIDATES tickers to investigate.
    Done deterministically — saves a full LLM call and 2-3 turns.

    Sources (merged, deduped):
      1. Scanner candidates (get_candidates) — wider net: max(2, min_score - 2)
      2. Gap-up movers (get_gap_up_tickers) — Alpaca market movers >= 2%

    Enrichment (all in-memory, no LLM):
      Pre-market: Alpaca latest quotes → gap%, premarket_change_pct
      Sector ETF: batch snapshot → sector tailwind/headwind
      Intraday only: batch minute bars → VWAP, RS vs SPY, ORB status

    Final ranking: composite score = baseline + gap + sector + intraday signals
    """
    from core.alpaca import get_gap_up_tickers, get_sector_etf_changes
    from scanner.universe import get_tickers as _get_universe_tickers, get_sector_etf, SECTOR_ETF_MAP

    # Wider baseline net — enrichment will re-rank by composite score
    effective_min = max(2, min_score - 2)
    candidates    = get_candidates(min_score=effective_min)
    valid         = [c for c in candidates if "error" not in c and c["ticker"] not in rejected_tickers]

    # Merge gap-up movers — universe-restricted
    universe_set    = set(_get_universe_tickers())
    scanner_tickers = {c["ticker"] for c in valid}
    gap_ups         = get_gap_up_tickers(min_gap_pct=2.0)
    for g in gap_ups:
        t = g["ticker"]
        if t not in scanner_tickers and t not in rejected_tickers and t in universe_set:
            valid.append({
                "ticker":          t,
                "technical_score": 5,
                "sector":          None,
                "_source":         "gap_up",
            })
            scanner_tickers.add(t)

    if not valid:
        return []

    tickers = [c["ticker"] for c in valid]

    # Pre-market snapshots (gap%, premarket price)
    snapshots = get_premarket_snapshot(tickers)
    snap_map  = {s["ticker"]: s for s in snapshots if "error" not in s}
    gap_pct_map = {g["ticker"]: g["gap_pct"] for g in gap_ups}

    # Sector ETF changes — fetch unique ETFs only
    needed_etfs = list(set(
        get_sector_etf(c["ticker"]) for c in valid
    ))
    needed_etfs += ["SMH"]  # always fetch SMH for semi sector
    sector_etf_changes: dict[str, float] = {}
    try:
        sector_etf_changes = get_sector_etf_changes(list(set(needed_etfs)))
    except Exception:
        pass

    # Intraday signals (only during intraday scan path)
    intraday_signals: dict[str, dict] = {}
    if is_intraday:
        try:
            from core.alpaca import batch_get_intraday_signals
            intraday_signals = batch_get_intraday_signals(tickers)
        except Exception:
            pass

    is_caution = market_report.get("decision") == "CAUTION"

    scored = []
    for c in valid:
        ticker  = c["ticker"]
        snap    = snap_map.get(ticker, {})
        baseline = c["technical_score"]
        gap_pct  = gap_pct_map.get(ticker) or snap.get("premarket_change_pct") or 0.0

        # CAUTION: only high-conviction setups
        if is_caution and (baseline < 7 or gap_pct <= 0.3):
            continue

        etf_symbol  = get_sector_etf(ticker)
        etf_pct     = sector_etf_changes.get(etf_symbol)

        intra = intraday_signals.get(ticker, {})
        rs_vs_spy  = intra.get("rs_vs_spy")
        above_vwap = intra.get("above_vwap")
        orb_pct    = intra.get("orb_pct")

        composite = _compute_composite_score(
            baseline, gap_pct, etf_pct, rs_vs_spy, above_vwap, orb_pct
        )

        scored.append({
            "ticker":               ticker,
            "score":                composite,
            "baseline_score":       baseline,
            "premarket_change_pct": gap_pct,
            "scanner_price":        snap.get("scanner_price"),
            "premarket_price":      snap.get("premarket_price") or c.get("price"),
            "sector":               c.get("sector") or snap.get("sector"),
            "sector_etf":           etf_symbol,
            "sector_etf_pct":       etf_pct,
            "rs_vs_spy":            rs_vs_spy,
            "above_vwap":           above_vwap,
            "orb_pct":              orb_pct,
        })

    scored.sort(key=lambda x: (x["score"], x["premarket_change_pct"]), reverse=True)
    return scored[:_MAX_CANDIDATES]


# ── Public entry point ────────────────────────────────────────────────────────

def run_research_agent(
    tracer: TraceLogger,
    market_report: dict,
    params: StrategyParams,
    candidates: Optional[list[dict]] = None,
    rejected_context: Optional[list[dict]] = None,
) -> dict:
    """
    Run the Research Agent pipeline.

    When candidates is provided (premarket path via Scanner Agent):
      Skip Phase 1 entirely. candidates is the pre-curated list from Scanner Agent.

    When candidates is None (intraday path):
      Phase 1 (deterministic, ~2s): screen via get_candidates + get_premarket_snapshot.

    Phase 2 (parallel, ~40-90s wall clock):
      Spawn one mini-agent per ticker in a thread pool (up to _MAX_CANDIDATES).
      Each mini-agent: 4 tools, 12 turn cap, 120s timeout.
      Parallel execution means 6 tickers take as long as 1.
    """
    rejected_tickers = [r["ticker"] for r in (rejected_context or [])]

    tracer.start_agent_span("research")

    if candidates is not None:
        # Premarket path: Scanner Agent already selected and ranked candidates.
        # Normalize to the context format expected by _investigate_ticker.
        selected = [
            {
                "ticker":               c["ticker"],
                "score":                c.get("technical_score", c.get("score", 5)),
                "premarket_change_pct": c.get("premarket_change_pct", 0.0),
                "scanner_price":        c.get("price"),
                "premarket_price":      c.get("price"),
                "sector":               c.get("sector"),
            }
            for c in candidates
            if c.get("ticker") not in rejected_tickers
        ][:_MAX_CANDIDATES]
        if not selected:
            tracer.log_decision("research", "no_candidates_after_screen")
            return {"proposals": [], "skipped": [], "summary": "No candidates after rejected-ticker filter."}
    else:
        # Intraday / fallback path: deterministic Phase 1 screening.
        raw = get_candidates(min_score=params.strategy_min_score)
        valid = [c for c in raw if "error" not in c]
        if not valid:
            tracer.log_decision("research", "no_candidates", detail={"raw": len(raw)})
            return {"proposals": [], "skipped": [], "summary": "No scan candidates today."}

        try:
            sector_data = get_sector_rotation()
        except Exception:
            sector_data = []

        selected = _screen_candidates(
            market_report, sector_data, rejected_tickers,
            min_score=params.strategy_min_score,
            is_intraday=True,
        )
        if not selected:
            tracer.log_decision("research", "no_candidates_after_screen")
            return {"proposals": [], "skipped": [], "summary": "No candidates passed screening."}

    # Pre-fetch news for all selected tickers (4 workers, done before investigation pool)
    news_batch = batch_fetch_news([s["ticker"] for s in selected])

    # Filter blackout tickers before spawning any LLM calls
    clean: list[dict] = []
    prefilter_skipped: list[dict] = []
    for s in selected:
        nc = news_batch.get(s["ticker"], {})
        if nc.get("blackout"):
            prefilter_skipped.append({"ticker": s["ticker"], "reason": nc.get("reason") or "earnings blackout"})
        else:
            clean.append(s)

    if not clean:
        tracer.log_decision("research", "all_tickers_in_blackout")
        return {
            "proposals": [],
            "skipped": prefilter_skipped,
            "summary": "All candidates in earnings blackout.",
        }

    # Phase 2: parallel per-ticker investigation
    proposals: list[dict] = []
    skipped: list[dict] = list(prefilter_skipped)

    with ThreadPoolExecutor(max_workers=len(clean)) as pool:
        futures = {
            pool.submit(_investigate_ticker, s["ticker"], s, market_report, tracer, news_batch.get(s["ticker"])): s["ticker"]
            for s in clean
        }
        try:
            for fut in as_completed(futures, timeout=_TOTAL_TIMEOUT_S - 30):
                ticker = futures[fut]
                try:
                    result = fut.result(timeout=5)
                    if result.get("action") == "PROPOSE":
                        proposals.append({
                            "ticker":        result["ticker"],
                            "entry_price":   result["entry_price"],
                            "target_price":  result["target_price"],
                            "stop_loss":     result["stop_loss"],
                            "position_size": result["position_size"],
                            "confidence":    result["confidence"],
                            "evidence":      result.get("evidence", []),
                        })
                    else:
                        skipped.append({
                            "ticker": ticker,
                            "reason": result.get("skip_reason") or "agent rejected",
                        })
                except Exception as e:
                    skipped.append({"ticker": ticker, "reason": f"investigation error: {e}"})
        except FuturesTimeout:
            for fut, ticker in futures.items():
                if not fut.done():
                    skipped.append({"ticker": ticker, "reason": "investigation timed out"})

    max_pos = market_report.get("max_positions") or 2
    # Sort proposals: HIGH > MEDIUM > LOW
    _conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    proposals.sort(key=lambda p: _conf_order.get(p.get("confidence", "LOW"), 2))
    proposals = proposals[:max_pos]

    summary = (
        f"Investigated {len(clean)} ticker(s) in parallel "
        f"({len(prefilter_skipped)} blackout-filtered). "
        f"{len(proposals)} proposal(s), {len(skipped)} skipped."
    )
    return {"proposals": proposals, "skipped": skipped, "summary": summary}
