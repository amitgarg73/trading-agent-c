"""
The agent's own memory of its runs.

Every question the agent asks *about itself* is answered here, from its own database:
"did I run premarket today?", "what is today's run id?", "are trades still pending?",
"when did I last scan for entries?".

Why this module exists
----------------------
These questions used to be answered by reading Provy's ag_sessions table. That made trading
depend on the observability platform: on 2026-07-25 Provy split its databases (#416), telemetry
began landing in Provy production while the agent kept reading the pre-production project, and
premarket started creating runs that the very next lookup could not find. Every session between
2026-07-25 and 2026-07-27 died on that line.

Trading Agent C models a real customer -- its own environment, its own data, telemetry sent
outward. A customer's trading decisions must not require their monitoring vendor to be reachable
and correct. So control flow reads this module, and only this module.

Telemetry is unaffected. TraceLogger still sends the full run record and every step to Provy
production. This is a local copy written alongside it, never instead of it.

Failures here are RAISED, not swallowed. A trace that goes missing costs a row on a dashboard;
a run record that goes missing means the agent forgets it is holding positions. The two are not
the same kind of loss, and the 2026-07-25 outage was silent precisely because the write that
mattered was fire-and-forget.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Optional

# Imported as a module, not as bound names: core.db.get_client is swapped at runtime by the test
# fixtures and by reset_client(), and a name bound here at import time would keep pointing at the
# original client.
from core import db

_TABLE = "c_sessions"

# Columns the control flow reads. Kept explicit so an unrelated schema addition cannot quietly
# change what callers receive.
_RUN_FIELDS = (
    "id,session_type,workflow_id,parent_session_id,status,terminal_reason,"
    "started_at,completed_at,metadata,result_summary,pending_trades,last_entry_scan_at,is_simulated"
)


def _workflow_id() -> str:
    return os.environ.get("WORKFLOW_ID", "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Lifecycle ─────────────────────────────────────────────────────────────────


def open_run(
    run_id: str,
    session_type: str,
    *,
    parent_run_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    is_simulated: bool = False,
) -> None:
    """
    Record that a run has started. Idempotent: re-opening the same id leaves the original
    started_at intact, so a retry cannot make a run look younger than it is (the premarket
    concurrency guard reads that timestamp to decide whether another run is already in flight).
    """
    started = started_at or _now()
    row = {
        "id":                run_id,
        "session_type":      session_type,
        "workflow_id":       workflow_id if workflow_id is not None else _workflow_id(),
        "parent_session_id": parent_run_id,
        "status":            "in_progress",
        "terminal_reason":   "",
        "started_at":        started.isoformat(),
        "date":              started.date().isoformat(),
        "is_simulated":      is_simulated,
        "metadata":          {},
    }
    db.execute_with_retry(
        db.get_client().table(_TABLE).upsert(row, on_conflict="id", ignore_duplicates=True),
        description="open_run",
    )


def close_run(
    run_id: str,
    terminal_reason: str,
    *,
    result_summary: Optional[str] = None,
    trades_proposed: int = 0,
    trades_approved: int = 0,
    trades_executed: int = 0,
    risk_rejections: int = 0,
    agents_invoked: Optional[list[str]] = None,
    loop_iterations: int = 1,
    retry_triggered: bool = False,
    total_steps: int = 0,
) -> None:
    """Record that a run has finished, and why."""
    fields: dict[str, Any] = {
        "status":           "completed",
        "terminal_reason":  terminal_reason,
        "completed_at":     _now().isoformat(),
        "trades_proposed":  trades_proposed,
        "trades_approved":  trades_approved,
        "trades_executed":  trades_executed,
        "risk_rejections":  risk_rejections,
        "loop_iterations":  loop_iterations,
        "retry_triggered":  retry_triggered,
        "total_steps":      total_steps,
    }
    if result_summary is not None:
        fields["result_summary"] = result_summary
    if agents_invoked is not None:
        fields["agents_invoked"] = agents_invoked
    db.execute_with_retry(
        db.get_client().table(_TABLE).update(fields).eq("id", run_id),
        description="close_run",
    )


# ── Lookups ───────────────────────────────────────────────────────────────────


def _latest(
    session_type: str,
    on_day: Optional[str] = None,
    *,
    include_simulated: bool = True,
) -> Optional[dict]:
    day = on_day or date.today().isoformat()
    req = (
        db.get_client()
        .table(_TABLE)
        .select(_RUN_FIELDS)
        .eq("session_type", session_type)
        .gte("started_at", day)
        .order("started_at", desc=True)
        .limit(1)
    )
    if not include_simulated:
        req = req.eq("is_simulated", False)
    wf = _workflow_id()
    if wf:
        req = req.eq("workflow_id", wf)
    rows = db.execute_with_retry(req, description=f"latest_{session_type}_run").data or []
    return rows[0] if rows else None


def today_premarket_run_id(on_day: Optional[str] = None) -> Optional[str]:
    """Today's premarket run id, or None. This is the day-level key everything else hangs off."""
    run = _latest("premarket", on_day)
    return run["id"] if run else None


def today_premarket_run(on_day: Optional[str] = None) -> Optional[dict]:
    """
    Today's real premarket run row, or None. Used by the concurrency guard.

    Simulated runs are excluded on purpose: the validation script exists to rehearse the pipeline
    on demand, and it has always documented that its run must not block the real premarket. The
    old guard read a table where nothing filtered on that flag, so a mid-morning rehearsal would
    in fact have suppressed the next real session.
    """
    return _latest("premarket", on_day, include_simulated=False)


def today_run(session_type: str, on_day: Optional[str] = None) -> Optional[dict]:
    """Today's latest run of a given type, or None. Used by the health checks."""
    return _latest(session_type, on_day)


def read_run(run_id: str) -> Optional[dict]:
    """One run by id, or None."""
    rows = db.execute_with_retry(
        db.get_client().table(_TABLE).select(_RUN_FIELDS).eq("id", run_id).limit(1),
        description="read_run",
    ).data or []
    return rows[0] if rows else None


# ── Pending trades ────────────────────────────────────────────────────────────
#
# Premarket can approve trades that are deliberately not placed until after the opening
# volatility settles. The watchdog picks them up later, so they have to survive between two
# separate processes.


def get_pending_trades(run_id: str) -> list:
    run = read_run(run_id)
    if not run:
        return []
    pending = run.get("pending_trades")
    return pending if isinstance(pending, list) else []


def set_pending_trades(run_id: str, trades: list) -> None:
    db.execute_with_retry(
        db.get_client().table(_TABLE).update({"pending_trades": trades}).eq("id", run_id),
        description="set_pending_trades",
    )


def clear_pending_trades(run_id: str) -> None:
    set_pending_trades(run_id, [])


# ── Entry scan pacing ─────────────────────────────────────────────────────────
#
# Intraday entries are rate limited so the agent cannot scan its way into an oversized book.
# The timestamp lives on the premarket run because that is the one row per trading day, which
# makes the check a single read regardless of how many intraday polls have happened.


def stamp_entry_scan(premarket_run_id: str, at: Optional[datetime] = None) -> None:
    db.execute_with_retry(
        db.get_client()
        .table(_TABLE)
        .update({"last_entry_scan_at": (at or _now()).isoformat()})
        .eq("id", premarket_run_id),
        description="stamp_entry_scan",
    )


def last_entry_scan_at(premarket_run_id: str) -> Optional[datetime]:
    run = read_run(premarket_run_id)
    if not run:
        return None
    return parse_ts(run.get("last_entry_scan_at"))


# ── Job heartbeats ────────────────────────────────────────────────────────────
#
# Some jobs produce no run record. The position watchdog is the important one: it polls every
# 15 minutes to manage open positions and, by design, writes nothing when there is nothing to do.
# That silence is indistinguishable from the job being dead, which is exactly how three days of
# outage went unnoticed in July 2026. A heartbeat makes "ran and had nothing to do" different
# from "did not run".

_HEARTBEAT_TABLE = "c_job_heartbeat"


def record_heartbeat(job: str, status: str = "ok", detail: Optional[str] = None) -> None:
    """Record that a job ran to completion. Called even on no-op exits -- the job still ran."""
    db.execute_with_retry(
        db.get_client().table(_HEARTBEAT_TABLE).upsert(
            {
                "job":         job,
                "last_run_at": _now().isoformat(),
                "last_status": status,
                "detail":      detail,
            },
            on_conflict="job",
        ),
        description="record_heartbeat",
    )


def read_heartbeat(job: str) -> Optional[dict]:
    rows = db.execute_with_retry(
        db.get_client().table(_HEARTBEAT_TABLE)
        .select("job,last_run_at,last_status,detail").eq("job", job).limit(1),
        description="read_heartbeat",
    ).data or []
    return rows[0] if rows else None


def heartbeat_age_minutes(job: str) -> Optional[float]:
    """Minutes since the job last completed, or None if it has never reported."""
    hb = read_heartbeat(job)
    if not hb:
        return None
    last = parse_ts(hb.get("last_run_at"))
    if not last:
        return None
    return (_now() - last).total_seconds() / 60


# ── Did the day's work actually land ─────────────────────────────────────────
#
# EOD does NOT get a run row of its own. It runs under the premarket session id (eod.py calls
# today_premarket_run_id), and open_run upserts with ignore_duplicates, so the premarket row wins
# and the "eod" session_type is discarded. There has never been a session_type='eod' row.
#
# Pinning EOD to its own session was tried and reverted (824103a) because it missed the rows that
# reconcile, so the fix is NOT to give EOD a run record. Ask instead whether the day's outcome was
# written: EOD computes performance and upserts one c_daily_performance row per date, which is the
# thing "today's performance was not recorded" is actually about.

_PERFORMANCE_TABLE = "c_daily_performance"


def performance_recorded(on_day: Optional[str] = None) -> bool:
    """True when EOD has written today's performance row. The honest end-of-day liveness signal."""
    day = on_day or date.today().isoformat()
    rows = db.execute_with_retry(
        db.get_client().table(_PERFORMANCE_TABLE).select("date").eq("date", day).limit(1),
        description="performance_recorded",
    ).data or []
    return bool(rows)


def parse_ts(raw: Any) -> Optional[datetime]:
    """
    Parse a Postgres timestamp into an aware UTC datetime.

    Postgres hands back '+00:00' offsets and fractional seconds of varying width; a naive
    fromisoformat on the raw string produces a naive datetime that then cannot be subtracted
    from an aware one. Every caller here compares against 'now', so normalise on the way in.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
