"""
Is the US equity market open on a given date? (argus#865)

⛔ THE WEEKDAY IS NOT THE ANSWER. `is_trading_day` reads the configured weekdays and nothing else, so
on Labor Day, Mon 7 Sep 2026, the agent ran premarket and two intraday scans against a closed market.
Research read the missing bars as "pre-market", still rated PRU HIGH, and Alpaca accepted four bracket
orders (WFC, JNJ, PRU, SNOW) that sat unfilled with `fill_price: null`.

Resolution order, most authoritative first:

  1. Alpaca's calendar API (`TradingClient.get_calendar`). It lists every session the exchange will
     hold, holidays already removed, so a date is open exactly when it comes back in the list.
  2. A static NYSE full-closure list for 2026 and 2027, used when the API call fails.
  3. For a date the static list does not cover, with the API also down: CLOSED.

⛔ WHY THE LAST RESORT IS CLOSED (fail closed). The two mistakes are not symmetric:

  - A spurious CLOSED skips one paper-trading day. Nothing is bought, nothing is at risk, and the
    skip is traced with terminal_reason `market_closed` and source `unknown`, so it is visible.
  - A spurious OPEN sends orders into a closed market. That is the 7 Sep failure: orders accepted,
    queued for the next open at a price nobody chose, positions written that the rest of the day
    cannot manage. It is exactly what this module exists to stop.

Fail closed only applies when BOTH the API and the static list have nothing to say. When the API is
down on a covered date the static list answers, so a transient Alpaca outage does not cost a normal
trading day. The static list is a tripwire as well as a fallback: tests fail once the current year
is past its last covered year, so it gets extended before it silently stops covering anything.

Early closes (13:00 ET on the day after Thanksgiving, Christmas Eve and some 3 July sessions) are
NOT modelled here: those days are open, and this module answers only open or closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

# NYSE full-day closures. Source: NYSE holiday calendar. Observed dates, not nominal ones: Independence
# Day 2026 falls on a Saturday and is observed Friday 3 Jul; Juneteenth 2027 (Sat) is observed Fri 18
# Jun, Independence Day 2027 (Sun) Mon 5 Jul, Christmas 2027 (Sat) Fri 24 Dec. New Year's Day 2028 is a
# Saturday and NYSE does not observe it on Fri 31 Dec 2027, so that date is correctly absent.
NYSE_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1):   "New Year's Day",
    date(2026, 1, 19):  "Martin Luther King Jr. Day",
    date(2026, 2, 16):  "Washington's Birthday",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 5, 25):  "Memorial Day",
    date(2026, 6, 19):  "Juneteenth",
    date(2026, 7, 3):   "Independence Day (observed)",
    date(2026, 9, 7):   "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1):   "New Year's Day",
    date(2027, 1, 18):  "Martin Luther King Jr. Day",
    date(2027, 2, 15):  "Washington's Birthday",
    date(2027, 3, 26):  "Good Friday",
    date(2027, 5, 31):  "Memorial Day",
    date(2027, 6, 18):  "Juneteenth (observed)",
    date(2027, 7, 5):   "Independence Day (observed)",
    date(2027, 9, 6):   "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
}

STATIC_COVERED_YEARS = frozenset({2026, 2027})


@dataclass(frozen=True)
class MarketDay:
    """The answer, plus where it came from, so a skip can say WHY it skipped."""
    day: date
    open: bool
    source: str                  # "alpaca" | "static" | "unknown"
    reason: Optional[str] = None  # holiday name, "weekend", or the failure that forced a fallback

    def as_detail(self) -> dict:
        return {"date": self.day.isoformat(), "open": self.open,
                "source": self.source, "reason": self.reason}


# Per-process memo of API answers only. A failed lookup is never cached, so the next caller retries.
_api_cache: dict[date, bool] = {}


def _static_answer(d: date, api_error: str) -> MarketDay:
    if d.weekday() >= 5:
        return MarketDay(d, False, "static", "weekend")
    if d.year not in STATIC_COVERED_YEARS:
        # Fail closed. See the module docstring for why this direction.
        return MarketDay(d, False, "unknown",
                         f"calendar API failed ({api_error}) and {d.year} is outside the static "
                         f"holiday list; failing closed")
    if d in NYSE_HOLIDAYS:
        return MarketDay(d, False, "static", NYSE_HOLIDAYS[d])
    return MarketDay(d, True, "static", f"calendar API failed ({api_error}); static list says open")


def check_market_day(d: date) -> MarketDay:
    """Whether the market holds a regular session on `d`. Never raises."""
    if d in _api_cache:
        is_open = _api_cache[d]
        return MarketDay(d, is_open, "alpaca", None if is_open else NYSE_HOLIDAYS.get(d, "closed"))
    try:
        from core.alpaca import get_market_session_dates
        sessions = get_market_session_dates(d, d)
    except Exception as e:                       # network, auth, SDK shape: all fall back the same way
        return _static_answer(d, str(e)[:200] or type(e).__name__)
    is_open = d in sessions
    _api_cache[d] = is_open
    reason = None
    if not is_open:
        reason = "weekend" if d.weekday() >= 5 else NYSE_HOLIDAYS.get(d, "closed")
    return MarketDay(d, is_open, "alpaca", reason)
