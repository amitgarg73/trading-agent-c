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


def trigger_server_judge(session_id: str) -> None:
    """Trigger Argus's server-side judge for a session, after it has closed.

    The server judge scores per-entity (per-ticker) L4 quality and writes the Outcome
    Ledger predictions for that session. This is the one canonical, entity-aware judge,
    so the ledger fills automatically and quality is not double-scored. Call after
    close_session so the session's terminal_reason is set (the ledger skip-exclusion
    reads it). Best-effort: a failure never affects the trading session.
    """
    try:
        import json
        import urllib.request
        from trace.logger import _ARGUS_URL, _ARGUS_API_KEY

        if not _ARGUS_URL:
            return
        req = urllib.request.Request(
            f"{_ARGUS_URL}/api/compute/judge",
            data=json.dumps({"session_id": session_id}).encode(),
            headers={"Content-Type": "application/json", "x-argus-key": _ARGUS_API_KEY or ""},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=120)
    except Exception as e:
        print(f"[outcomes] server judge trigger failed for {session_id[:8]}: {e}")


# Exit reasons that mean no real trade happened, so there is no outcome to reconcile.
_NO_TRADE_EXITS = {"unfilled", "test_cleanup"}


def push_trade_outcomes(trades: list[dict]) -> int:
    """Push each closed trade's realized P&L to the Argus Outcome Ledger, keyed on ticker.

    Argus reconciles each against the trace-based prediction it made for that ticker
    (matched / diverged). This is the tenant side of the Ledger: we own the outcome (P&L)
    and report it to Argus like any external customer would. Orders that never filled are
    skipped (no real outcome). Best-effort: a failure never affects the trading session.
    Returns the number of outcomes posted.
    """
    from trace.logger import _ingest_post

    sent = 0
    for t in trades or []:
        if (t.get("exit_reason") or "") in _NO_TRADE_EXITS:
            continue
        ticker = t.get("ticker")
        pnl = t.get("realized_pnl")
        if not ticker or pnl is None:
            continue
        try:
            _ingest_post("/api/ingest/outcome", {
                "entity_id":   ticker,
                "value":       float(pnl),
                "source":      "confirmed",
                "occurred_at": t.get("close_time"),
            })
            sent += 1
        except Exception as e:
            print(f"[outcomes] ledger push failed for {ticker}: {e}")
    return sent
