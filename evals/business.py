"""
L3 funnel throughput evals for whichever session actually runs the funnel.

⛔ RENAMED FROM write_premarket_outcome_evals. The funnel moved to intraday on 27 Jul 2026 and the
name kept pointing at premarket, which is how it stayed easy to believe these were still being
written. A name that describes where a thing USED to run is a small lie that costs a lot later.

Deterministic, rule-based metrics — no LLM required. Migrated from L5 to L3
because these measure pipeline throughput, not decision validity (which is L5).
Writes via Argus ingest /eval API — auto-creates incidents on failure.
"""
from __future__ import annotations

import os


def write_funnel_evals(
    session_id: str,
    trades_proposed: int,
    trades_approved: int,
    terminal_reason: str,
) -> None:
    """
    Write L3 funnel throughput evals for a completed premarket session.

    ⛔ CALL IT FROM THE CLOSE PATH, NOT FROM THE HAPPY PATH. These stopped grading on 27 Jul 2026
    because premarket called them on its main path only, and a redesign added an early return above
    the call. No error, no gap: the checks simply stopped and nothing said so for four weeks.
    See sessions/intraday.py::_close_intraday for the shape that cannot be skipped.

    Skip and no-candidate sessions are excluded: funnel metrics are not meaningful when the pipeline
    did not run to the research/risk stages at all. ⛔ THAT IS NOT THE SAME AS "research ran and
    found nothing", which IS a real research_yield of zero and must still be recorded.
    """
    if not os.environ.get("TENANT_ID"):
        return
    if terminal_reason in ("skip_propagated", "no_candidates", "no_viable_candidates", "scanner_error"):
        return

    metrics: list[dict] = [
        {
            "eval_name": "research_yield",
            "agent":     "research",
            "score":     1.0 if trades_proposed > 0 else 0.0,
            "passed":    trades_proposed > 0,
            "threshold": 0.9,
            "reasoning": (
                f"Research produced {trades_proposed} proposal(s)"
                if trades_proposed > 0
                else "Research produced no proposals"
            ),
        },
    ]
    if trades_proposed > 0:
        approval_rate = round(trades_approved / trades_proposed, 3)
        metrics.append({
            "eval_name": "risk_approval_rate",
            "agent":     "risk",
            "score":     approval_rate,
            "passed":    approval_rate >= 0.20,
            "threshold": 0.20,
            "reasoning": f"{trades_approved}/{trades_proposed} proposals approved by risk",
        })

    try:
        from trace.logger import _ingest_post
        for m in metrics:
            _ingest_post("/api/ingest/eval", {
                "session_id": session_id,
                "eval_name":  m["eval_name"],
                "agent":      m["agent"],
                "layer":      3,
                "score":      m["score"],
                "passed":     m["passed"],
                "threshold":  m["threshold"],
                "detail":     {"reasoning": m["reasoning"]},
            })
        print(f"[business-eval] Wrote {len(metrics)} L3 funnel eval(s) for session {session_id[:8]}")
    except Exception as exc:
        print(f"[business-eval] eval write failed: {exc}")
