"""Write EOD business outcome metrics to ag_outcomes for quality-vs-P&L correlation."""
from __future__ import annotations

import os
from datetime import date
from uuid import uuid4


def write_eod_outcome_metrics(
    session_id: str,
    realized_pnl: float,
    win_rate: float,
    trades_total: int,
) -> None:
    """Write EOD P&L metrics to ag_outcomes, snapshotting avg L4 quality for correlation.

    Skipped silently when TENANT_ID is unset or any DB error occurs.
    """
    try:
        from core.db import get_client

        tenant_id = os.environ.get("TENANT_ID", "")
        if not tenant_id:
            try:
                from dotenv import load_dotenv
                load_dotenv()
                tenant_id = os.environ.get("TENANT_ID", "")
            except ImportError:
                pass

        if not tenant_id:
            print("[outcomes] TENANT_ID not set, skipping")
            return

        client = get_client()

        # Snapshot avg L4 quality for this session at time of writing
        evals_rows = (
            client.table("ag_evals")
            .select("score")
            .eq("tenant_id", tenant_id)
            .eq("session_id", session_id)
            .eq("layer", 4)
            .execute()
            .data
        )
        scores = [r["score"] for r in evals_rows if r.get("score") is not None]
        quality_score = round(sum(scores) / len(scores), 4) if scores else None

        today = date.today().isoformat()
        metrics = [
            ("realized_pnl", float(realized_pnl), "usd"),
            ("win_rate",     float(win_rate),     "ratio"),
            ("trades_total", float(trades_total),  "count"),
        ]

        rows = [
            {
                "id":            str(uuid4()),
                "tenant_id":     tenant_id,
                "session_id":    session_id,
                "metric_name":   name,
                "metric_value":  value,
                "metric_unit":   unit,
                "quality_score": quality_score,
                "period_date":   today,
            }
            for name, value, unit in metrics
        ]

        client.table("ag_outcomes").insert(rows).execute()
        print(f"[outcomes] Wrote {len(rows)} outcome metrics (quality_score={quality_score})")
    except Exception as e:
        print(f"[outcomes] Failed to write outcome metrics: {e}")
