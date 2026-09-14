"""EOD outcome reporting to Provy, over its API only: per-trade P&L to the ledger, session risk signals.

⛔ NOTHING HERE WRITES TO A PROVY TABLE. `write_eod_outcome_metrics` used to insert into `ag_outcomes`
through the shared Supabase client. That client points at Provy's PRE-PRODUCTION project, where this
fleet's sessions have not existed since Provy split its databases on 2026-07-25, so every insert failed
on `ag_outcomes_session_id_fkey` from then on (EOD logs 3 Aug to 11 Sep), caught and printed, run green.
The per-ticker `position_realized_pnl` added on 16 Aug (argus#578) never landed anywhere.

Nothing was lost by removing it. The fleet's own record already holds every number: each trade's
realized P&L on `c_positions`, the day on `c_daily_performance`, and `compute_risk_metrics` derives the
risk shape from those trades on demand. Provy receives the per-ticker P&L on its ledger
(`push_trade_outcomes`) and the session risk signals (`push_outcome_signals`), both over the API.
"""
from __future__ import annotations



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
