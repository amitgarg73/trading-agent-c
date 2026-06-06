"""
L5 business outcome evals for the premarket session funnel.

Deterministic, rule-based metrics — no LLM required.
Writes to ag_evals at layer=5 after session completion.
"""
from __future__ import annotations

import os
from uuid import uuid4


_TENANT_ID = os.environ.get("TENANT_ID", "")


def write_premarket_outcome_evals(
    session_id: str,
    trades_proposed: int,
    trades_approved: int,
    terminal_reason: str,
) -> None:
    """
    Write L5 business outcome evals for a completed premarket session.

    Call after close_session(), before the process exits.
    Skip sessions and no-candidate sessions are excluded — funnel metrics
    are not meaningful when the pipeline didn't run to the research/risk stages.
    """
    tenant_id = _TENANT_ID or os.environ.get("TENANT_ID", "")
    if not tenant_id:
        return

    if terminal_reason in ("skip_propagated", "no_candidates", "no_viable_candidates"):
        return

    metrics: list[dict] = []

    # Did research produce any proposals?
    research_score = 1.0 if trades_proposed > 0 else 0.0
    metrics.append({
        "eval_name": "research_yield",
        "agent":     "research",
        "score":     research_score,
        "passed":    research_score >= 0.9,
        "threshold": 0.9,
        "reasoning": (
            f"Research produced {trades_proposed} proposal(s)"
            if trades_proposed > 0
            else "Research produced no proposals"
        ),
    })

    # What fraction of research proposals survived risk review?
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
        from core.db import get_client
        rows = [
            {
                "id":         str(uuid4()),
                "tenant_id":  tenant_id,
                "session_id": session_id,
                "eval_name":  m["eval_name"],
                "agent":      m["agent"],
                "layer":      5,
                "score":      m["score"],
                "passed":     m["passed"],
                "threshold":  m["threshold"],
                "detail":     {"reasoning": m["reasoning"]},
            }
            for m in metrics
        ]
        get_client().table("ag_evals").insert(rows).execute()
        print(f"[business-eval] Wrote {len(rows)} L5 outcome eval(s) for session {session_id[:8]}")
    except Exception as exc:
        print(f"[business-eval] L5 write failed: {exc}")
