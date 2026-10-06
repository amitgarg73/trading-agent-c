"""
The day an end-of-day run settles (argus#1566).

Every end-of-day step used to ask the clock ("today") for the day it was settling. That made a
missed EOD unrecoverable the next morning: a re-run would settle the new day, find no session
under it and exit, and the missed day stayed unscored with its positions open.

settle_day() is now the ONE place that answers that question. With nothing given it returns
date.today(), exactly what every call site returned before, in the same (machine local) timezone.
An explicit day can be set by `--date YYYY-MM-DD` on sessions/eod.py or by the EOD_DATE env var.

An explicit day is refused when it is malformed, in the future, or not a day the market was open.
Refusing is deliberate: a typo must not settle the wrong day against the real account.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Callable, Optional

import pytz

_ET = pytz.timezone("America/New_York")

ENV_VAR = "EOD_DATE"

_override: Optional[date] = None


class SettleDateError(ValueError):
    """The requested settle date is not usable. The message says why, in plain words."""


def settle_day() -> date:
    """The day this run settles: the explicit day if one was set, else today (unchanged default)."""
    return _override if _override is not None else date.today()


def set_settle_day(d: Optional[date]) -> None:
    """Fix the settle day for this process. None restores the default (today)."""
    global _override
    _override = d


def explicit_settle_day() -> Optional[date]:
    """The explicit day, or None when this run is settling today by default."""
    return _override


def requested_date(cli_value: Optional[str] = None, env: Optional[dict] = None) -> Optional[str]:
    """The raw date text asked for: the command-line value wins over EOD_DATE; blank means none."""
    env = os.environ if env is None else env
    for raw in (cli_value, env.get(ENV_VAR)):
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def parse_settle_date(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise SettleDateError(
            f"EOD date {raw!r} is not a date. Use YYYY-MM-DD, for example 2026-10-05."
        ) from None


def validate_settle_date(
    d: date,
    *,
    is_trading_weekday: Callable[[str], bool],
    market_open_on: Callable[[date], bool],
    now_et: Optional[datetime] = None,
) -> None:
    """Raise SettleDateError unless `d` is a past-or-current day the market held a session."""
    now_et = now_et or datetime.now(_ET)
    # The later of local today and ET today, so a run in the evening Pacific time can still name
    # the ET date that has already started. Never earlier than the default would have been.
    latest = max(date.today(), now_et.date())
    if d > latest:
        raise SettleDateError(
            f"EOD date {d.isoformat()} is in the future (today is {latest.isoformat()}). "
            f"Refusing: a day that has not happened cannot be settled."
        )
    weekday = d.strftime("%a").upper()[:3]
    if not is_trading_weekday(weekday):
        raise SettleDateError(
            f"EOD date {d.isoformat()} is a {weekday}, not a configured trading day. Refusing."
        )
    if not market_open_on(d):
        raise SettleDateError(
            f"EOD date {d.isoformat()} was not a market trading day (holiday or closed). Refusing."
        )
