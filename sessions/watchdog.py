"""
Session watchdog — two jobs.

1. Close orphaned sessions that never called close_session(). Idempotent.
2. Assert the day's work actually happened, and raise the alarm when it did not.

Job 2 exists because job 1 alone is unfalsifiable. Between 25 and 27 July 2026 every scheduled
session failed for three days straight and this watchdog reported success every hour throughout,
because "no orphaned sessions found" is exactly what a completely dead agent looks like. The
failure was found only because the trading jobs happened to email on failure; nothing was
watching for the absence of work.

A monitor that cannot fail on the thing it watches is not a monitor. These checks are written so
that silence means healthy and anything else alerts and exits non-zero.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, time, timezone, timedelta

import pytz


STALE_HOURS = 1

_ET = pytz.timezone("America/New_York")

# When each job is late enough to be worth waking someone. Deliberately generous: the point is to
# catch a dead agent, not to page on a slow morning.
_PREMARKET_LATE_AFTER = time(11, 0)   # window ends 10:30 ET
_EOD_LATE_AFTER       = time(16, 30)  # EOD runs 15:55 ET
_POSITION_POLL_END    = time(15, 50)
_POSITION_STALE_MINS  = 45            # polls every 15 minutes; three misses is not a blip
# ⛔ THE OLD WINDOW OPENED AT 9:45 ON THE COMMENT "first poll is 9:15". THAT WAS NEVER TRUE. Measured
# across the runs that actually happened, the first poll of the day lands at 10:00 ET, and on 21 Aug
# it was 11:00. So every trading morning the check ran, found a heartbeat from YESTERDAY AFTERNOON,
# and mailed "the position watchdog last ran 1079 minutes ago". It was the only failing run in the
# last sixty, and it was wrong. An alert that is wrong every morning is one you stop reading.
_POSITION_FIRST_POLL_DUE = time(10, 30)   # observed 10:00, sometimes 11:00; this is the "never started" line

POSITION_WATCHDOG_JOB = "position_watchdog"


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


def check_expected_work(now_et: datetime | None = None) -> list[str]:
    """
    Return a list of plain-language problems with today's work. Empty means healthy.

    Only meaningful on trading days. Each check is time-gated so it cannot fire before the job it
    watches was ever due.
    """
    from core import run_state
    from core.agent_config import is_trading_day

    now_et  = now_et or datetime.now(_ET)
    now_t   = now_et.time()
    weekday = now_et.strftime("%a").upper()[:3]
    today   = now_et.date().isoformat()

    if not is_trading_day(weekday):
        return []

    problems: list[str] = []

    premarket = run_state.today_run("premarket", today)
    if now_t >= _PREMARKET_LATE_AFTER:
        if not premarket:
            problems.append(
                f"No premarket run recorded for {today}. The agent has not analysed the market "
                f"today, so no trades can have been placed."
            )
        elif premarket.get("status") != "completed":
            problems.append(
                f"Premarket run {premarket['id'][:8]} started but never finished "
                f"(status {premarket.get('status')!r}). Today's plan is incomplete."
            )

    # ⛔ "HAS NOT STARTED TODAY" AND "STOPPED MID-SESSION" ARE DIFFERENT PROBLEMS AND THE OLD CODE
    # REPORTED THEM AS ONE. Before the first poll of the day the newest heartbeat is from yesterday
    # afternoon, so a plain staleness test reads ~1,080 minutes and fires every morning. Ask whether
    # the poller has run TODAY first, and only call it stale once it has.
    if now_t <= _POSITION_POLL_END:
        age = run_state.heartbeat_age_minutes(POSITION_WATCHDOG_JOB)
        ran_today = age is not None and (
            (datetime.combine(now_et.date(), now_t) - timedelta(minutes=age)).date() == now_et.date()
        )
        if age is None:
            if now_t >= _POSITION_FIRST_POLL_DUE:
                problems.append(
                    "The position watchdog has never reported in. Open positions are not being "
                    "managed and trailing stops are not being maintained."
                )
        elif not ran_today:
            # Yesterday's heartbeat is not staleness, it is a poller that has not started.
            if now_t >= _POSITION_FIRST_POLL_DUE:
                problems.append(
                    f"The position watchdog has not run at all today (last run was "
                    f"{age/60:.0f} hours ago, before today's session). Open positions are not "
                    f"being managed."
                )
        elif age > _POSITION_STALE_MINS:
            problems.append(
                f"The position watchdog last ran {age:.0f} minutes ago (expected every 15). "
                f"Open positions may not be under management."
            )

    # Ask whether the day's outcome landed, NOT whether an "eod" run row exists. EOD deliberately
    # runs under the premarket session id, so there is no session_type='eod' row and never has been;
    # checking for one alerted every single evening while EOD was in fact running fine. What EOD
    # does write, once it has closed out and computed the day, is the performance row.
    if now_t >= _EOD_LATE_AFTER:
        if not run_state.performance_recorded(today):
            problems.append(
                f"No end-of-day performance recorded for {today}. Positions may not have been "
                f"closed and the day was not scored."
            )

    return problems


def run_health_checks(now_et: datetime | None = None, alert: bool = True) -> list[str]:
    """Run the expectation checks and alert on anything found. Returns the problems."""
    problems = check_expected_work(now_et)
    if not problems:
        print("[watchdog] Expected work check: healthy.")
        return []

    for p in problems:
        print(f"[watchdog] PROBLEM: {p}")

    if not alert:
        print("[watchdog] (dry-run) not sending an alert.")
        return problems

    from core.alerts import send_alert
    send_alert(
        "Strategy C — agent is not doing its work",
        "The hourly watchdog found work that should have happened and did not:\n\n"
        + "\n\n".join(f"- {p}" for p in problems)
        + "\n\nThis is the watchdog reporting an ABSENCE of work, not a job that crashed. "
          "Check the GitHub Actions runs for Premarket, Intraday Scan and Position Watchdog.",
    )
    return problems


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    run_watchdog(dry_run=dry)
    problems = run_health_checks(alert=not dry)
    # Exit non-zero so the scheduled job goes red and the failure notification fires. Without
    # this the watchdog would print its findings into a log nobody reads.
    if problems and not dry:
        sys.exit(1)
