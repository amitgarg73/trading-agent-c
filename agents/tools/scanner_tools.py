from __future__ import annotations

from datetime import date
from typing import Any

from agents.tools.market_tools import get_sector_rotation
from core.alpaca import get_gap_up_tickers
from scanner.universe import get_tickers as _get_universe_tickers


def get_scan_results(min_score: int = 1) -> list[dict[str, Any]]:
    """
    Read today's scanner results from c_scan_results.
    Fast DB read — no external API calls. Returns all scored tickers above
    min_score, ordered by score desc.
    """
    from core.db import get_client

    today = date.today().isoformat()
    rows = (
        get_client()
        .table("c_scan_results")
        .select("ticker,score,price,sector")
        .eq("date", today)
        .gte("score", min_score)
        .order("score", desc=True)
        .execute()
        .data
        or []
    )
    return [
        {
            "ticker":          r["ticker"],
            "technical_score": r["score"],
            "price":           r["price"],
            "sector":          r["sector"],
        }
        for r in rows
    ]


def get_premarket_snapshot(tickers: list[str]) -> list[dict[str, Any]]:
    """
    Batch-fetch premarket quote data from Alpaca for the given tickers.
    Returns premarket_change_pct and premarket_price per ticker.
    Delegates to the shared research tool implementation.
    """
    from agents.tools.research_tools import get_premarket_snapshot as _snap

    return _snap(tickers)


def get_gap_ups(min_gap_pct: float = 2.0) -> list[dict[str, Any]]:
    """
    Fetch market movers from Alpaca screener gapping >= min_gap_pct.
    Restricted to the strategy universe so micro-caps and warrants cannot enter.
    """
    universe = set(_get_universe_tickers())
    movers = get_gap_up_tickers(min_gap_pct=min_gap_pct)
    return [m for m in movers if m.get("ticker") in universe]


def get_sector_leaders(n: int = 5) -> list[dict[str, Any]]:
    """
    Return the top N sector ETFs by 1-day performance.
    Gives the scanner agent regime context for sector-biased selection.
    """
    rows = get_sector_rotation()
    valid = [r for r in rows if "error" not in r]
    valid.sort(key=lambda r: r.get("change_pct", 0), reverse=True)
    return valid[:n]


def filter_and_rank(
    candidates: list[dict[str, Any]],
    max_n: int = 25,
    min_score: int = 5,
    caution_mode: bool = False,
) -> dict[str, Any]:
    """
    Apply quality threshold and dynamic N to a merged candidate list.
    candidates: [{ticker, technical_score, premarket_change_pct, sector?, price?}]
    Returns {candidates[], n_returned, dropped_count, threshold_applied}.
    """
    _CAUTION_MIN_SCORE = 7
    _CAUTION_MIN_PCT   = 0.3
    _CAUTION_MAX_N     = 15

    effective_min_score = _CAUTION_MIN_SCORE if caution_mode else min_score
    effective_max_n     = min(max_n, _CAUTION_MAX_N) if caution_mode else max_n

    qualified = []
    dropped   = 0
    for c in candidates:
        score = c.get("technical_score") or 0
        pct   = c.get("premarket_change_pct") or 0.0
        if score < effective_min_score:
            dropped += 1
            continue
        if caution_mode and pct < _CAUTION_MIN_PCT:
            dropped += 1
            continue
        qualified.append(c)

    qualified.sort(
        key=lambda x: (x.get("technical_score", 0), x.get("premarket_change_pct", 0)),
        reverse=True,
    )
    selected       = qualified[:effective_max_n]
    total_dropped  = dropped + (len(qualified) - len(selected))

    threshold_desc = (
        f"score>={effective_min_score}"
        + (f", premarket>={_CAUTION_MIN_PCT}%" if caution_mode else "")
        + f", max_n={effective_max_n}"
    )

    return {
        "candidates": [
            {
                "ticker":               c["ticker"],
                "technical_score":      c.get("technical_score", 5),
                "premarket_change_pct": c.get("premarket_change_pct", 0.0),
                "price":                c.get("price"),
                "sector":               c.get("sector"),
            }
            for c in selected
        ],
        "n_returned":        len(selected),
        "dropped_count":     total_dropped,
        "threshold_applied": threshold_desc,
    }
