"""
Shadow comparison: Strategy C vs Strategy A.

Shows what C proposed/executed on a given date alongside A's actual trades.
Useful during the 10-day shadow evaluation period before paper capital.

Usage:
    python3 scripts/shadow_compare.py                    # today
    python3 scripts/shadow_compare.py --date 2026-05-27  # specific date
    python3 scripts/shadow_compare.py --days 10          # last 10 trading days

Strategy A connection requires SUPABASE_URL_A + SUPABASE_KEY_A env vars.
C uses the standard SUPABASE_URL + SUPABASE_KEY (same as core/db.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from typing import Optional


# ── DB connections ─────────────────────────────────────────────────────────────

def _make_client(url: str, key: str):
    from supabase import create_client
    return create_client(url, key)


def _c_client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    return _make_client(url, key)


def _a_client():
    url = os.environ.get("SUPABASE_URL_A", "")
    key = os.environ.get("SUPABASE_KEY_A", "")
    if not url or not key:
        return None
    return _make_client(url, key)


# ── Data loaders ───────────────────────────────────────────────────────────────

def load_c_day(client, target_date: str) -> dict:
    """Load C's session, scan candidates, and executed positions for a date."""
    session_rows = (
        client.table("c_sessions")
        .select("id,terminal_reason,trades_executed")
        .eq("date", target_date)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not session_rows:
        return {"session": None, "candidates": [], "positions": []}

    session    = session_rows[0]
    session_id = session["id"]

    candidates = (
        client.table("c_scan_results")
        .select("ticker,score,price")
        .eq("date", target_date)
        .order("score", desc=True)
        .execute()
        .data
    ) or []

    positions = (
        client.table("c_positions")
        .select("ticker,entry_price,target_price,stop_loss,position_size,"
                "shares,confidence,realized_pnl,exit_reason,status")
        .eq("session_id", session_id)
        .execute()
        .data
    ) or []

    return {"session": session, "candidates": candidates, "positions": positions}


def load_a_day(client, target_date: str) -> dict:
    """Load Strategy A's positions for a date, normalized to C's field names."""
    rows = (
        client.table("positions")
        .select("ticker,entry_price,target_price,stop_loss,position_size,"
                "realized_pnl,close_reason,status,fill_price")
        .gte("opened_at", f"{target_date}T00:00:00")
        .lt("opened_at", f"{target_date}T23:59:59")
        .execute()
        .data
    ) or []

    normalized = [
        {
            "ticker":        r["ticker"],
            "entry_price":   r.get("fill_price") or r.get("entry_price"),
            "target_price":  r.get("target_price"),
            "stop_loss":     r.get("stop_loss"),
            "position_size": r.get("position_size"),
            "realized_pnl":  r.get("realized_pnl"),
            "exit_reason":   r.get("close_reason"),
            "status":        (r.get("status") or "").lower(),
        }
        for r in rows
    ]
    return {"positions": normalized}


# ── Pure analysis functions ────────────────────────────────────────────────────

def _avg_planned_rr(positions: list[dict]) -> float:
    """Compute average planned R:R = (target - entry) / (entry - stop) across positions."""
    rrs = []
    for p in positions:
        try:
            entry  = float(p.get("entry_price") or 0)
            target = float(p.get("target_price") or 0)
            stop   = float(p.get("stop_loss") or 0)
            if entry and target and stop and entry > stop:
                rrs.append((target - entry) / (entry - stop))
        except Exception:
            pass
    return round(sum(rrs) / len(rrs), 2) if rrs else 0.0


def compare_day(
    target_date: str,
    c_data: dict,
    a_data: Optional[dict],
) -> dict:
    """Build a comparison dict for one trading day."""
    c_positions   = c_data.get("positions") or []
    c_candidates  = c_data.get("candidates") or []
    c_scan_set    = {r["ticker"] for r in c_candidates}
    c_tickers     = {p["ticker"] for p in c_positions}

    c_closed  = [p for p in c_positions if (p.get("status") or "").lower() == "closed"]
    c_pnl     = round(sum(p.get("realized_pnl") or 0 for p in c_closed), 2)
    c_wins    = sum(1 for p in c_closed if (p.get("realized_pnl") or 0) > 0)
    c_planned_rr = _avg_planned_rr(c_positions)

    score_range = (
        (min(r["score"] for r in c_candidates), max(r["score"] for r in c_candidates))
        if c_candidates else (0, 0)
    )

    result: dict = {
        "date":          target_date,
        "c_candidates":  len(c_candidates),
        "c_score_range": score_range,
        "c_executed":    len(c_positions),
        "c_closed":      len(c_closed),
        "c_pnl":         c_pnl,
        "c_wins":        c_wins,
        "c_losses":      len(c_closed) - c_wins,
        "c_planned_rr":  c_planned_rr,
        "c_tickers":     sorted(c_tickers),
        "a_data":        None,
    }

    if a_data:
        a_positions = a_data.get("positions") or []
        a_tickers   = {p["ticker"] for p in a_positions}
        a_closed    = [p for p in a_positions if (p.get("status") or "").lower() == "closed"]
        a_pnl       = round(sum(p.get("realized_pnl") or 0 for p in a_closed), 2)
        a_wins      = sum(1 for p in a_closed if (p.get("realized_pnl") or 0) > 0)
        a_planned_rr = _avg_planned_rr(a_positions)

        overlap  = c_tickers & a_tickers
        c_only   = c_tickers - a_tickers
        a_only   = a_tickers - c_tickers

        result["a_data"] = {
            "executed":         len(a_positions),
            "closed":           len(a_closed),
            "pnl":              a_pnl,
            "wins":             a_wins,
            "losses":           len(a_closed) - a_wins,
            "planned_rr":       a_planned_rr,
            "tickers":          sorted(a_tickers),
            "overlap":          sorted(overlap),
            "c_only":           sorted(c_only),
            "a_only":           sorted(a_only),
            "a_only_in_c_pool": sorted(t for t in a_only if t in c_scan_set),
        }

    return result


# ── Report printing ────────────────────────────────────────────────────────────

def _win_rate_str(wins: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{round(wins / total * 100)}%"


def print_day_report(comp: dict) -> None:
    target_date = comp["date"]
    lo, hi      = comp["c_score_range"]
    print(f"\nShadow Comparison — {target_date}")
    print("=" * 64)

    score_range = f"scores {lo}-{hi}" if lo or hi else "no scores"
    print(f"Candidate pool (C):  {comp['c_candidates']} tickers  {score_range}")

    tickers_str = f"({', '.join(comp['c_tickers'])})" if comp['c_tickers'] else "(none)"
    print(f"C executed:          {comp['c_executed']} trade(s)  {tickers_str}")

    if comp["c_closed"]:
        wl = f"{comp['c_wins']}W {comp['c_losses']}L"
        wr = _win_rate_str(comp["c_wins"], comp["c_closed"])
        rr = f"  planned R:R {comp['c_planned_rr']}" if comp["c_planned_rr"] else ""
        print(f"C P&L:               ${comp['c_pnl']:+.2f}  {wl}  win rate {wr}{rr}")
    else:
        print(f"C P&L:               no closed trades")

    a = comp.get("a_data")
    if a:
        tickers_str_a = f"({', '.join(a['tickers'])})" if a["tickers"] else "(none)"
        print()
        print(f"Strategy A ({target_date}):")
        print(f"A executed:          {a['executed']} trade(s)  {tickers_str_a}")
        if a["closed"]:
            wl_a = f"{a['wins']}W {a['losses']}L"
            wr_a = _win_rate_str(a["wins"], a["closed"])
            rr_a = f"  planned R:R {a['planned_rr']}" if a["planned_rr"] else ""
            print(f"A P&L:               ${a['pnl']:+.2f}  {wl_a}  win rate {wr_a}{rr_a}")
        else:
            print(f"A P&L:               no closed trades")

        print()
        overlap_str = f"({', '.join(a['overlap'])})" if a["overlap"] else "(none)"
        print(f"Overlap:             {len(a['overlap'])} ticker(s)  {overlap_str}")

        if a["c_only"]:
            print(f"C-only picks:        {', '.join(a['c_only'])}")

        if a["a_only"]:
            in_pool  = a["a_only_in_c_pool"]
            not_pool = [t for t in a["a_only"] if t not in in_pool]
            parts = []
            if in_pool:
                parts.append(f"{', '.join(in_pool)} (in C pool, not selected)")
            if not_pool:
                parts.append(f"{', '.join(not_pool)} (not in C pool)")
            print(f"A-only picks:        {' | '.join(parts)}")
    else:
        print()
        print("(Set SUPABASE_URL_A + SUPABASE_KEY_A for Strategy A comparison)")

    print("=" * 64)


def print_multi_day_table(comparisons: list[dict]) -> None:
    has_a = any(c.get("a_data") for c in comparisons)

    print("\nShadow Comparison — Multi-Day Summary")
    if has_a:
        hdr = f"{'Date':<12} {'C P&L':>9} {'C W/L':>7} {'C R:R':>6}  {'A P&L':>9} {'A W/L':>7} {'Overlap':>7}"
    else:
        hdr = f"{'Date':<12} {'C P&L':>9} {'C W/L':>7} {'C R:R':>6}  {'Pool':>5}"
    print(hdr)
    print("-" * len(hdr))

    c_total_pnl = a_total_pnl = 0.0
    c_total_w = c_total_l = a_total_w = a_total_l = 0

    for comp in sorted(comparisons, key=lambda x: x["date"]):
        c_wl        = f"{comp['c_wins']}/{comp['c_losses']}"
        c_total_pnl += comp["c_pnl"]
        c_total_w   += comp["c_wins"]
        c_total_l   += comp["c_losses"]

        if has_a:
            a       = comp.get("a_data") or {}
            a_pnl   = a.get("pnl") or 0.0
            a_wl    = f"{a.get('wins', 0)}/{a.get('losses', 0)}" if a else "—"
            overlap = len(a.get("overlap") or [])
            a_total_pnl += a_pnl
            a_total_w   += a.get("wins", 0)
            a_total_l   += a.get("losses", 0)
            print(f"{comp['date']:<12} ${comp['c_pnl']:>+8.2f} {c_wl:>7} {comp['c_planned_rr']:>6.1f}  "
                  f"${a_pnl:>+8.2f} {a_wl:>7} {overlap:>7}")
        else:
            print(f"{comp['date']:<12} ${comp['c_pnl']:>+8.2f} {c_wl:>7} {comp['c_planned_rr']:>6.1f}  "
                  f"{comp['c_candidates']:>5}")

    print("-" * len(hdr))
    c_total_wl = f"{c_total_w}/{c_total_l}"
    if has_a:
        a_total_wl = f"{a_total_w}/{a_total_l}"
        print(f"{'Total':<12} ${c_total_pnl:>+8.2f} {c_total_wl:>7} {'':>6}  "
              f"${a_total_pnl:>+8.2f} {a_total_wl:>7}")
    else:
        print(f"{'Total':<12} ${c_total_pnl:>+8.2f} {c_total_wl:>7}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _trading_dates_before(end_date: date, n: int) -> list[str]:
    """Return the last N Mon-Fri dates up to and including end_date."""
    result = []
    d = end_date
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.isoformat())
        d -= timedelta(days=1)
    return result


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Compare Strategy C vs Strategy A")
    parser.add_argument("--date",  default=date.today().isoformat(),
                        help="Target date (YYYY-MM-DD, default today)")
    parser.add_argument("--days",  type=int, default=1,
                        help="Compare last N trading days (default 1)")
    args = parser.parse_args(argv)

    c_db = _c_client()
    if c_db is None:
        print("ERROR: SUPABASE_URL + SUPABASE_KEY not set.")
        sys.exit(1)

    a_db = _a_client()

    target = date.fromisoformat(args.date)
    dates  = _trading_dates_before(target, args.days)

    comparisons = []
    for d in dates:
        c_data = load_c_day(c_db, d)
        if c_data["session"] is None:
            continue
        a_data = load_a_day(a_db, d) if a_db else None
        comparisons.append(compare_day(d, c_data, a_data))

    if not comparisons:
        print(f"No Strategy C sessions found for the requested date(s).")
        return

    if len(comparisons) == 1:
        print_day_report(comparisons[0])
    else:
        print_multi_day_table(comparisons)
        for comp in sorted(comparisons, key=lambda x: x["date"], reverse=True)[:3]:
            print_day_report(comp)


if __name__ == "__main__":
    main()
