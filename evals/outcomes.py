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

    This writes to OUR OWN database (SUPABASE_URL), which is this fleet's book of record. It is NOT
    how Provy sees these numbers and never was: ARGUS_URL points at Provy production while
    SUPABASE_URL is our own project, so nothing written here is visible to the contract. That
    mismatch is what made the risk conditions look wired for weeks while grading from nothing.

    Provy is fed by `push_outcome_signals`, over the API, from the same `compute_risk_metrics`
    values. Keep both: this is our record, that is the report. Do not "fix" this by pointing it at
    Provy's database, which we do not own and cannot write to.

    Pass `trades` (the session's closed trades, each carrying position_size and realized_pnl);
    defaults to an empty/zero session.

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

        # ⛔ PER-TICKER P&L, TAGGED, AS A DIAGNOSTIC ONLY (argus#578).
        #
        # The session metrics above are PORTFOLIO readings and stay untagged. "End-of-day net profit
        # is positive after all positions are closed" is a portfolio question (Amit's call, 14 Aug);
        # graded per ticker it would silently become "every position was profitable", a harsher
        # contract nobody wrote.
        #
        # So this emits a DIFFERENT metric name, position_realized_pnl, tagged with the ticker. No
        # contract condition reads it, and since argus#582 the evaluator only fans out over entities
        # contributing a reading the contract actually grades. Before #582 this would have split every
        # session into N passes and re-graded the portfolio conditions once per ticker.
        #
        # What it buys: the per-ticker CLAIM that intraday now states (argus#602) finally has a
        # per-ticker settled number to be reconciled against. Claim and outcome meet at one grain.
        per_ticker = [
            {
                "id":            str(uuid4()),
                "tenant_id":     tenant_id,
                "session_id":    session_id,
                "entity_id":     t.get("ticker"),
                "metric_name":   "position_realized_pnl",
                "metric_value":  float(t.get("realized_pnl") or 0.0),
                "metric_unit":   "usd",
                "quality_score": quality_score,
                "period_date":   today,
            }
            for t in (trades or [])
            # Same exclusion the ledger push applies: an order that never filled settled nothing, so
            # reporting a P&L for it would invent an outcome. Skipping it here keeps the diagnostic
            # and the ledger describing the same set of trades.
            if t.get("ticker") and t.get("realized_pnl") is not None
            and (t.get("exit_reason") or "") not in _NO_TRADE_EXITS
        ]
        rows += per_ticker

        client.table("ag_outcomes").insert(rows).execute()
        print(f"[outcomes] Wrote {len(rows)} outcome metrics "
              f"({len(per_ticker)} per-ticker, quality_score={quality_score})")
    except Exception as e:
        print(f"[outcomes] Failed to write outcome metrics: {e}")


def push_outcome_signals(
    session_id: str,
    realized_pnl: float,
    trades_total: int,
    *,
    trades: list[dict] | None = None,
) -> bool:
    """Report the session's settled risk signals to Provy, so the contract grades on reality.

    The per-trade ledger push (push_trade_outcomes) carries a P&L number per ticker and nothing
    else, so the only contract conditions that ever graded were the two reading realized_pnl, and
    they graded from the AGENTS' OWN trace payloads rather than from what settled. The three risk
    conditions (drawdown, position limits, worst single-trade loss) graded from nothing at all.
    Measured against production on 2026-07-29: 4 of the 6 conditions had never been measured once.

    These signals are per SESSION, not per trade, so they go to the session-scoped endpoint. Sending
    them through the ledger route would need a synthetic entity_id, and Provy HOLDS an outcome for a
    work item it never predicted, so every session would leave a permanent unreconcilable row.

    Best-effort by design: Provy is never in the trade critical path, so a delivery failure is a
    logged warning. It returns the delivery result rather than swallowing it, because a dropped
    outcome that logs like a success is what hid this gap in the first place.
    """
    from trace.logger import _ingest_post

    risk = compute_risk_metrics(trades or [], int(trades_total), _max_positions())
    payload = {
        "session_id": session_id,
        "signals": {
            # realized_pnl is sent here too, deliberately. It is already in the ledger as a per-ticker
            # value, but the contract's conditions grade at session grain, and until now they read it
            # off the agents' own traces — an estimate standing in for a settled fact.
            "realized_pnl":               float(realized_pnl),
            "max_drawdown_pct":           risk["max_drawdown_pct"],
            "within_limits":              bool(risk["within_limits"]),
            "max_single_trade_loss_pct":  risk["max_single_trade_loss_pct"],
        },
    }
    try:
        if _ingest_post("/api/ingest/outcome/signals", payload):
            print(f"[outcomes] pushed {len(payload['signals'])} outcome signals for session {session_id}")
            return True
        print(f"[outcomes] WARNING: outcome signals NOT accepted for session {session_id}")
        return False
    except Exception as e:
        print(f"[outcomes] outcome signal push failed for session {session_id}: {e}")
        return False


def backfill_server_judge() -> None:
    """EOD safety net: judge the workflow's most recent closed sessions server-side.

    Provy grades a session when it closes, so this is a safety net rather than the primary path:
    it covers a close whose background grading was dropped by the serverless runtime, which is the
    failure /api/ingest/session/close's own `after()` wrapper exists to reduce but cannot eliminate.
    A no-session-id call judges the last closed sessions in one shot. Best-effort: a failure never
    affects the trading session.

    ⛔ THE PER-SESSION TRIGGER THIS USED TO SIT BESIDE IS GONE (Provy #730). Asking to grade a
    session one line after closing it raced with the grading the close had already started: both
    runs read "already scored" as empty and both wrote. This one is safe because it runs at EOD,
    hours later, when the skip-set is populated — the race needed the two calls to be seconds apart.
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


def push_trade_outcomes(trades: list[dict], session_id: str | None = None) -> int:
    """Push each closed trade's realized P&L to the Argus Outcome Ledger, keyed on ticker.

    Argus reconciles each against the trace-based prediction it made for that ticker
    (matched / diverged). This is the tenant side of the Ledger: we own the outcome (P&L)
    and report it to Argus like any external customer would. Orders that never filled are
    skipped (no real outcome). Best-effort: a failure never affects the trading session.

    Returns the number of outcomes Argus actually ACCEPTED, not the number attempted.
    The two used to be the same number by construction, because the transport swallowed
    every error, so a day where nothing landed logged exactly like a day where everything
    did. That is how the ledger accumulated predictions nobody ever answered.

    session_id pins the outcome to the prediction made in that session. Without it Argus
    falls back to the most recent unanswered row for the ticker, which on a fleet that sees
    the same ticker on many days can settle the wrong day's prediction.
    """
    from trace.logger import _ingest_post

    sent = 0
    attempted = 0
    for t in trades or []:
        if (t.get("exit_reason") or "") in _NO_TRADE_EXITS:
            continue
        ticker = t.get("ticker")
        pnl = t.get("realized_pnl")
        if not ticker or pnl is None:
            continue
        attempted += 1
        payload = {
            "entity_id":   ticker,
            "value":       float(pnl),
            "source":      "confirmed",
            "occurred_at": t.get("close_time"),
        }
        if session_id:
            payload["session_id"] = session_id
        try:
            if _ingest_post("/api/ingest/outcome", payload):
                sent += 1
            else:
                print(f"[outcomes] ledger push NOT accepted for {ticker}")
        except Exception as e:
            print(f"[outcomes] ledger push failed for {ticker}: {e}")
    if attempted != sent:
        print(f"[outcomes] WARNING: {attempted - sent} of {attempted} trade outcomes did not reach Argus")
    return sent
