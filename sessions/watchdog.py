"""
Session watchdog — close orphaned sessions that never called close_session().

Marks sessions with terminal_reason IN ('in_progress', '') that started more
than STALE_HOURS ago as 'watchdog_timeout'. Safe to run repeatedly (idempotent).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta


STALE_HOURS = 1


def _parse_ts(ts: str) -> datetime:
    """Parse Supabase timestamps, normalising fractional seconds to 6 digits."""
    ts = ts.replace("Z", "+00:00")
    ts = re.sub(r"\.(\d+)([+-])", lambda m: f".{m.group(1)[:6].ljust(6, '0')}{m.group(2)}", ts)
    return datetime.fromisoformat(ts)


def find_orphaned_sessions(client, stale_before: datetime) -> list[dict]:
    rows = (
        client.table("c_sessions")
        .select("id,date,started_at,terminal_reason,total_cost_usd")
        .lt("started_at", stale_before.isoformat())
        .execute()
        .data
    )
    return [
        r for r in (rows or [])
        if r.get("terminal_reason") in ("in_progress", "", None)
    ]


def close_orphaned_session(client, session_id: str) -> None:
    client.table("c_sessions").update({
        "terminal_reason": "watchdog_timeout",
        "completed_at":    datetime.now(timezone.utc).isoformat(),
    }).eq("id", session_id).execute()


def run_watchdog(dry_run: bool = False) -> list[str]:
    """
    Close all orphaned sessions. Returns list of closed session IDs.
    With dry_run=True, reports what would be closed without writing.
    """
    from core.db import get_client
    client     = get_client()
    stale_before = datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)
    orphans    = find_orphaned_sessions(client, stale_before)

    if not orphans:
        print("[watchdog] No orphaned sessions found.")
        return []

    closed = []
    for s in orphans:
        cost = s.get("total_cost_usd") or 0.0
        age_h = (
            datetime.now(timezone.utc)
            - _parse_ts(s["started_at"])
        ).total_seconds() / 3600
        print(
            f"[watchdog] {'(dry-run) ' if dry_run else ''}orphan {s['id'][:8]} "
            f"date={s['date']} age={age_h:.1f}h cost=${cost:.4f} "
            f"reason={repr(s['terminal_reason'])}"
        )
        if not dry_run:
            close_orphaned_session(client, s["id"])
            closed.append(s["id"])

    if not dry_run:
        print(f"[watchdog] Closed {len(closed)} orphaned session(s).")
    else:
        print(f"[watchdog] Would close {len(orphans)} session(s) (dry-run).")

    return closed


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    run_watchdog(dry_run=dry)
