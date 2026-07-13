"""Write EOD business outcome metrics to ag_outcomes for quality-vs-P&L correlation."""
from __future__ import annotations

import os
from datetime import date
from uuid import uuid4


def _max_positions() -> int:
    """Configured position-count limit; falls back to the default if params can't load."""
    try:
        from core.params import load_params
        return int(load_params().max_positions)
    except Exception:
        return 10


def compute_risk_metrics(
    trades: list[dict],
    trades_total: int,
    max_positions: int,
) -> dict[str, float]:
    """Derive the success contract's risk-shape signals from a session's closed trades.

    Percentages are measured against the capital actually put to work, not the idle account pool
    (a trade's real capital-at-risk is its position size, ~$3k, not the ~$50k buying-power pool):
      - max_drawdown_pct: peak-to-trough of cumulative realized P&L over the session's closed trades
        (ordered by close time), as a percent of the capital DEPLOYED this session (sum of position
        sizes). Realized-trade drawdown, not tick-level mark-to-market.
      - within_limits: 1.0 when the session's trade count stayed within the position-count limit,
        else 0.0. (Position count is the limit we enforce and can verify at close.)
      - max_single_trade_loss_pct: the worst single closed-trade loss, as a percent of THAT trade's
        own position size — so a $3k trade losing $60 reads as 2%, independent of pool size.

    A no-trade session yields 0 drawdown, within limits, 0 single-trade loss — all correct.
    """
    # Capital deployed this session = sum of the per-trade position sizes (money actually at work).
    deployed = sum(float(t.get("position_size") or 0.0) for t in trades)

    # Realized-trade drawdown: run the cumulative P&L over the closed trades in time order and track
    # the deepest fall from a running peak, as a percent of the capital deployed.
    seq = sorted(
        (t for t in trades if t.get("close_time")),
        key=lambda t: t["close_time"],
    )
    cumulative = peak = max_dd = 0.0
    for t in seq:
        cumulative += float(t.get("realized_pnl") or 0.0)
        if cumulative > peak:
            peak = cumulative
        if peak - cumulative > max_dd:
            max_dd = peak - cumulative
    drawdown_pct = round(max_dd / deployed * 100, 4) if deployed > 0 else 0.0

    # Worst single-trade loss as a percent of that trade's own deployed capital (position_size).
    worst_loss_pct = 0.0
    for t in trades:
        pnl = float(t.get("realized_pnl") or 0.0)
        size = float(t.get("position_size") or 0.0)
        if pnl < 0 and size > 0:
            loss_pct = -pnl / size * 100
            if loss_pct > worst_loss_pct:
                worst_loss_pct = loss_pct

    within_limits = 1.0 if int(trades_total) <= int(max_positions) else 0.0

    return {
        "max_drawdown_pct": drawdown_pct,
        "within_limits": within_limits,
        "max_single_trade_loss_pct": round(worst_loss_pct, 4),
    }


def write_eod_outcome_metrics(
    session_id: str,
    realized_pnl: float,
    win_rate: float,
    trades_total: int,
    *,
    trades: list[dict] | None = None,
) -> None:
    """Write EOD P&L and risk metrics to ag_outcomes, snapshotting avg L4 quality for correlation.

    Beyond P&L, this emits the drawdown / limits / single-trade-loss signals the success contract
    grades against (conditions s2, s3, f2, r1), computed from the session's closed trades. Pass
    `trades` (the session's closed trades, each carrying position_size and realized_pnl); defaults
    to an empty/zero session.

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
        # Risk-shape signals for the success contract (drawdown / limits / single-trade loss). Every
        # session grades the full contract, not just P&L, so s2/s3/f2/r1 stop reading "not measurable".
        risk = compute_risk_metrics(trades or [], int(trades_total), _max_positions())
        metrics += [
            ("max_drawdown_pct",          risk["max_drawdown_pct"],          "pct"),
            ("within_limits",             risk["within_limits"],             "flag"),
            ("max_single_trade_loss_pct", risk["max_single_trade_loss_pct"], "pct"),
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
        from trace.logger import _ARGUS_URL, _ARGUS_API_KEY, _emit_enabled

        if not _emit_enabled():
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


def backfill_server_judge() -> None:
    """EOD safety net: judge the workflow's most recent closed sessions server-side.

    trigger_server_judge fires per-session only on the premarket / intraday-entry close paths, so
    sessions that close another way (EOD, a premarket stand-down, an intraday with no entries) never
    get their L4 quality scored. A no-session-id call judges the last closed sessions in one shot
    (idempotent server-side — already-scored pairs are skipped), so quality coverage no longer
    depends on any single close path. Best-effort: a failure never affects the trading session.
    """
    try:
        import json
        import urllib.request
        from trace.logger import _ARGUS_URL, _ARGUS_API_KEY, _emit_enabled

        if not _emit_enabled():
            return
        req = urllib.request.Request(
            f"{_ARGUS_URL}/api/compute/judge",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json", "x-argus-key": _ARGUS_API_KEY or ""},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=120)
    except Exception as e:
        print(f"[outcomes] server judge backfill failed: {e}")


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
