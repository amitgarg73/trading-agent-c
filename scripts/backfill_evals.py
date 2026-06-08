"""
One-time backfill: write L4 (judge) and L5 (business) evals for existing sessions.

Reads agent outputs from ag_traces for L4.
Reads trades_proposed/trades_approved from ag_sessions.metadata for L5.

Usage:
    SUPABASE_URL=... SUPABASE_KEY=... TENANT_ID=... WORKFLOW_ID=... ANTHROPIC_API_KEY=... \
    python scripts/backfill_evals.py

Safe to re-run — skips sessions that already have evals written.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TENANT_ID    = os.environ["TENANT_ID"]
WORKFLOW_ID  = os.environ.get("WORKFLOW_ID", "")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def has_layer_evals(session_id: str, layer: int) -> bool:
    r = (
        sb.table("ag_evals")
        .select("id", count="exact")
        .eq("tenant_id", TENANT_ID)
        .eq("session_id", session_id)
        .eq("layer", layer)
        .limit(1)
        .execute()
    )
    return (r.count or 0) > 0


def backfill_l5(session_id: str, metadata: dict) -> None:
    trades_proposed = int(metadata.get("trades_proposed", 0))
    trades_approved = int(metadata.get("trades_approved", 0))
    terminal_reason = metadata.get("terminal_reason", "")
    from evals.business import write_premarket_outcome_evals
    write_premarket_outcome_evals(
        session_id=session_id,
        trades_proposed=trades_proposed,
        trades_approved=trades_approved,
        terminal_reason=terminal_reason,
    )


def backfill_l4(session_id: str) -> None:
    from evals.judge import evaluate_session_from_traces
    evaluate_session_from_traces(session_id)


def main() -> None:
    print(f"Backfill evals — tenant={TENANT_ID[:8]} workflow={WORKFLOW_ID[:8] if WORKFLOW_ID else 'ALL'}")

    q = (
        sb.table("ag_sessions")
        .select("id, status, terminal_reason, metadata, started_at")
        .eq("tenant_id", TENANT_ID)
        .neq("terminal_reason", "simulated")
        .order("started_at", desc=False)
        .limit(500)
    )
    if WORKFLOW_ID:
        q = q.eq("workflow_id", WORKFLOW_ID)

    sessions = q.execute().data or []
    print(f"Found {len(sessions)} sessions\n")

    l4_ok = l4_err = l4_skip = l5_ok = l5_err = l5_skip = 0

    for i, sess in enumerate(sessions):
        sid  = sess["id"]
        meta = sess.get("metadata") or {}
        print(f"[{i+1}/{len(sessions)}] {sid[:8]}  started={sess['started_at'][:10]}  status={sess['status']}")

        # L5 — skip only if L5 already exists
        if has_layer_evals(sid, 5):
            print("  L5 skipped (already exists)")
            l5_skip += 1
        else:
            try:
                backfill_l5(sid, meta)
                print("  L5 written")
                l5_ok += 1
            except Exception as e:
                print(f"  L5 failed: {e}")
                l5_err += 1

        # L4 — skip only if L4 already exists
        if has_layer_evals(sid, 4):
            print("  L4 skipped (already exists)")
            l4_skip += 1
        else:
            try:
                backfill_l4(sid)
                print("  L4 written")
                l4_ok += 1
            except Exception as e:
                print(f"  L4 failed: {e}")
                l4_err += 1

    print(f"\nDone.  L4 ok={l4_ok} skip={l4_skip} err={l4_err}  |  L5 ok={l5_ok} skip={l5_skip} err={l5_err}")


if __name__ == "__main__":
    main()
