"""
Scanner Agent quality evaluations.

Reads from c_traces + c_positions to measure selection quality over time.
Run daily or on-demand after sessions accumulate.

Metrics:
  selection_rate   — % scanner candidates that become research proposals
  acceptance_rate  — % scanner candidates that survive Risk Agent
  trade_rate       — % scanner candidates that become executed trades
  regime_accuracy  — on high-VIX days, scanner uses defensive criteria
  funnel           — candidate → proposal → approved → executed counts per session
  cost_per_trade   — scanner agent cost divided by trades executed
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any


def _client():
    from core.db import get_client
    return get_client()


def _fetch_scanner_traces(days: int = 14) -> list[dict]:
    """Fetch scanner agent trace rows from the last N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = (
        _client()
        .table("c_traces")
        .select("session_id,agent,step_type,tool_name,tool_input,tool_output,outcome,created_at")
        .eq("agent", "scanner")
        .gte("created_at", since)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    return rows


def _fetch_sessions(days: int = 14) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = (
        _client()
        .table("c_sessions")
        .select("id,date,terminal_reason,trades_proposed,trades_approved,trades_executed,cost_breakdown")
        .gte("date", since)
        .order("date", desc=False)
        .execute()
        .data
        or []
    )
    return rows


def _fetch_research_traces(session_ids: list[str]) -> list[dict]:
    """Research agent decision rows for the given sessions."""
    if not session_ids:
        return []
    rows = (
        _client()
        .table("c_traces")
        .select("session_id,agent,outcome,tool_output")
        .in_("session_id", session_ids)
        .eq("step_type", "decision")
        .in_("agent", ["research", "scanner"])
        .execute()
        .data
        or []
    )
    return rows


def _parse_tool_output(row: dict) -> Any:
    out = row.get("tool_output")
    if isinstance(out, dict):
        return out
    if isinstance(out, str):
        try:
            return json.loads(out)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


# ── Per-session funnel ────────────────────────────────────────────────────────

def compute_session_funnel(sessions: list[dict], traces: list[dict]) -> list[dict]:
    """
    For each session with scanner data, compute the candidate → trade funnel.
    Returns a list of per-session dicts.
    """
    scanner_decisions = {
        r["session_id"]: _parse_tool_output(r)
        for r in traces
        if r.get("step_type") == "decision" and r.get("agent") == "scanner"
    }

    funnel = []
    for sess in sessions:
        sid    = sess["id"]
        detail = scanner_decisions.get(sid)
        if not detail:
            continue

        n_candidates = detail.get("n_returned", 0)
        proposed     = sess.get("trades_proposed", 0) or 0
        approved     = sess.get("trades_approved", 0) or 0
        executed     = sess.get("trades_executed", 0) or 0

        scanner_cost = 0.0
        cb = sess.get("cost_breakdown") or {}
        if isinstance(cb, str):
            try:
                cb = json.loads(cb)
            except Exception:
                cb = {}
        scanner_cost = (cb.get("scanner") or {}).get("cost_usd", 0.0)

        funnel.append({
            "session_id":      sid,
            "date":            sess.get("date"),
            "n_candidates":    n_candidates,
            "n_proposed":      proposed,
            "n_approved":      approved,
            "n_executed":      executed,
            "regime":          detail.get("regime", "unknown"),
            "dropped_count":   detail.get("dropped_count", 0),
            "selection_rate":  round(proposed / n_candidates, 3) if n_candidates else 0.0,
            "acceptance_rate": round(approved / n_candidates, 3) if n_candidates else 0.0,
            "trade_rate":      round(executed / n_candidates, 3) if n_candidates else 0.0,
            "scanner_cost_usd": round(scanner_cost, 4),
            "cost_per_trade":  round(scanner_cost / executed, 4) if executed else None,
        })
    return funnel


# ── Regime accuracy ───────────────────────────────────────────────────────────

def compute_regime_accuracy(traces: list[dict]) -> dict:
    """
    On sessions where Scanner Agent logged a regime, check if:
    - high_vix / caution → dropped_count was high (strict filtering)
    - low_vix → dropped_count was lower (more candidates passed)

    Returns: {
      high_vix_sessions, low_vix_sessions,
      avg_dropped_high_vix, avg_dropped_low_vix,
      regime_differentiation: true if high_vix dropped more
    }
    """
    decisions = [
        _parse_tool_output(r)
        for r in traces
        if r.get("step_type") == "decision" and r.get("agent") == "scanner"
    ]
    high_vix = [d for d in decisions if d.get("regime") in ("high_vix", "caution")]
    low_vix  = [d for d in decisions if d.get("regime") == "low_vix"]

    def _avg_dropped(rows):
        vals = [r.get("dropped_count", 0) for r in rows if r.get("dropped_count") is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    avg_high = _avg_dropped(high_vix)
    avg_low  = _avg_dropped(low_vix)

    return {
        "high_vix_sessions":        len(high_vix),
        "low_vix_sessions":         len(low_vix),
        "avg_dropped_high_vix":     avg_high,
        "avg_dropped_low_vix":      avg_low,
        "regime_differentiation":   avg_high > avg_low if (high_vix and low_vix) else None,
    }


# ── Tool usage pattern ────────────────────────────────────────────────────────

def compute_tool_usage(traces: list[dict]) -> dict:
    """
    Count how often each Scanner Agent tool was called.
    Helps verify the agent follows the 5-tool sequence consistently.
    """
    tool_calls = [r for r in traces if r.get("step_type") == "tool_call"]
    counts: dict[str, int] = {}
    for row in tool_calls:
        tool = row.get("tool_name") or "unknown"
        counts[tool] = counts.get(tool, 0) + 1
    return counts


# ── Main report ───────────────────────────────────────────────────────────────

def run_eval(days: int = 14) -> dict:
    """Run all scanner evals and return a structured report."""
    print(f"[eval_scanner] Fetching last {days} days of scanner traces...")
    traces   = _fetch_scanner_traces(days)
    sessions = _fetch_sessions(days)

    session_ids = list({r["session_id"] for r in traces})
    print(f"  sessions with scanner data: {len(session_ids)}")

    funnel   = compute_session_funnel(sessions, traces)
    regime   = compute_regime_accuracy(traces)
    tools    = compute_tool_usage(traces)

    total_candidates = sum(f["n_candidates"] for f in funnel)
    total_executed   = sum(f["n_executed"]   for f in funnel)
    total_cost       = sum(f["scanner_cost_usd"] for f in funnel)

    summary = {
        "eval_period_days":      days,
        "sessions_evaluated":    len(funnel),
        "total_candidates":      total_candidates,
        "total_trades_executed": total_executed,
        "overall_trade_rate":    round(total_executed / total_candidates, 3) if total_candidates else 0.0,
        "total_scanner_cost_usd": round(total_cost, 4),
        "avg_cost_per_trade":    round(total_cost / total_executed, 4) if total_executed else None,
    }

    report = {
        "summary":        summary,
        "regime_accuracy": regime,
        "tool_usage":      tools,
        "session_funnel":  funnel,
    }

    print("\n=== Scanner Agent Eval Report ===")
    print(f"Period:        last {days} days")
    print(f"Sessions:      {summary['sessions_evaluated']}")
    print(f"Total cands:   {summary['total_candidates']}")
    print(f"Trade rate:    {summary['overall_trade_rate']:.1%}")
    print(f"Total cost:    ${summary['total_scanner_cost_usd']:.4f}")
    if summary["avg_cost_per_trade"] is not None:
        print(f"Cost/trade:    ${summary['avg_cost_per_trade']:.4f}")
    print(f"\nRegime accuracy:")
    print(f"  High-VIX sessions: {regime['high_vix_sessions']} "
          f"(avg dropped: {regime['avg_dropped_high_vix']})")
    print(f"  Low-VIX sessions:  {regime['low_vix_sessions']} "
          f"(avg dropped: {regime['avg_dropped_low_vix']})")
    if regime["regime_differentiation"] is not None:
        diff_str = "YES — high-VIX filters more strictly" if regime["regime_differentiation"] else "NO — check prompt"
        print(f"  Differentiation:   {diff_str}")
    print(f"\nTool call counts: {tools}")

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scanner Agent quality eval")
    parser.add_argument("--days", type=int, default=14, help="Look-back window in days")
    args = parser.parse_args()
    run_eval(days=args.days)
