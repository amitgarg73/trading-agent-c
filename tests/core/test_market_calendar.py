"""
argus#865: the market calendar, and the fail-closed policy when it cannot answer.

⛔ THE SHIPPED DEFECT. Mon 7 Sep 2026 was Labor Day. `is_trading_day("MON")` said yes, nothing else was
asked, and the agent ran premarket plus two intraday scans into a closed market, placing four orders.

Every test here is marked real_calendar so it runs the real check_market_day. `_no_real_broker` still
refuses any real Alpaca client, so an unpatched lookup raises and exercises the fallback path.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from core import market_calendar
from core.market_calendar import NYSE_HOLIDAYS, STATIC_COVERED_YEARS

pytestmark = pytest.mark.real_calendar

LABOR_DAY_2026 = date(2026, 9, 7)
NORMAL_TUESDAY = date(2026, 9, 8)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(market_calendar, "_api_cache", {})


class TestTheLaborDayDefect:
    def test_labor_day_2026_is_not_a_trading_day(self):
        day = market_calendar.check_market_day(LABOR_DAY_2026)
        assert day.open is False
        assert day.reason == "Labor Day"

    def test_a_normal_tuesday_is_a_trading_day(self):
        assert market_calendar.check_market_day(NORMAL_TUESDAY).open is True

    def test_the_weekday_config_alone_still_says_yes_which_is_why_both_are_needed(self, mock_supabase):
        from core.agent_config import is_trading_day
        assert is_trading_day("MON") is True
        assert market_calendar.check_market_day(LABOR_DAY_2026).open is False


class TestAlpacaIsAskedFirst:
    def test_a_date_in_the_api_session_list_is_open(self):
        with patch("core.alpaca.get_market_session_dates", return_value={NORMAL_TUESDAY}) as api:
            day = market_calendar.check_market_day(NORMAL_TUESDAY)
        api.assert_called_once_with(NORMAL_TUESDAY, NORMAL_TUESDAY)
        assert (day.open, day.source) == (True, "alpaca")

    def test_a_date_missing_from_the_api_list_is_closed(self):
        with patch("core.alpaca.get_market_session_dates", return_value=set()):
            day = market_calendar.check_market_day(LABOR_DAY_2026)
        assert (day.open, day.source, day.reason) == (False, "alpaca", "Labor Day")

    def test_the_api_outranks_the_static_list(self):
        # An unscheduled closure (the kind the static list cannot know about) must win.
        with patch("core.alpaca.get_market_session_dates", return_value=set()):
            assert market_calendar.check_market_day(NORMAL_TUESDAY).open is False

    def test_the_answer_is_memoised_but_a_failure_is_not(self):
        with patch("core.alpaca.get_market_session_dates", side_effect=RuntimeError("503")) as api:
            market_calendar.check_market_day(NORMAL_TUESDAY)
            market_calendar.check_market_day(NORMAL_TUESDAY)
        assert api.call_count == 2, "a failed lookup must be retried by the next caller"
        with patch("core.alpaca.get_market_session_dates", return_value={NORMAL_TUESDAY}) as api:
            market_calendar.check_market_day(NORMAL_TUESDAY)
            market_calendar.check_market_day(NORMAL_TUESDAY)
        assert api.call_count == 1


class TestTheFailurePolicy:
    """API down: static list for 2026-2027, CLOSED for anything it does not cover."""

    def test_api_failure_on_a_covered_holiday_uses_the_static_list(self):
        with patch("core.alpaca.get_market_session_dates", side_effect=RuntimeError("timeout")):
            day = market_calendar.check_market_day(LABOR_DAY_2026)
        assert (day.open, day.source) == (False, "static")

    def test_api_failure_on_a_covered_trading_day_stays_open(self):
        # A transient outage must not cost an ordinary trading day when the list can answer.
        with patch("core.alpaca.get_market_session_dates", side_effect=RuntimeError("timeout")):
            day = market_calendar.check_market_day(NORMAL_TUESDAY)
        assert (day.open, day.source) == (True, "static")
        assert "timeout" in day.reason

    def test_api_failure_outside_the_static_list_FAILS_CLOSED(self):
        # ⛔ The documented policy. A spurious closed skips a paper day; a spurious open sends orders
        # into a closed market, which is the 7 Sep defect. With nothing able to answer, closed.
        uncovered_weekday = date(2029, 3, 6)   # a Tuesday
        with patch("core.alpaca.get_market_session_dates", side_effect=RuntimeError("down")):
            day = market_calendar.check_market_day(uncovered_weekday)
        assert day.open is False
        assert day.source == "unknown"
        assert "failing closed" in day.reason

    def test_the_real_broker_guard_path_also_falls_back(self):
        # No patch at all: _no_real_broker refuses the client, which is just another API failure.
        assert market_calendar.check_market_day(LABOR_DAY_2026).source == "static"

    def test_weekends_are_closed_even_without_the_api(self):
        with patch("core.alpaca.get_market_session_dates", side_effect=RuntimeError("down")):
            day = market_calendar.check_market_day(date(2029, 3, 3))   # Saturday, uncovered year
        assert (day.open, day.reason) == (False, "weekend")


class TestTheStaticList:
    def test_every_listed_holiday_is_a_weekday(self):
        # An observed closure never falls on a weekend; one that does is a transcription error.
        assert all(d.weekday() < 5 for d in NYSE_HOLIDAYS)

    def test_it_has_ten_closures_per_covered_year(self):
        for year in STATIC_COVERED_YEARS:
            assert sum(1 for d in NYSE_HOLIDAYS if d.year == year) == 10

    def test_observed_dates_not_nominal_ones(self):
        assert date(2026, 7, 3) in NYSE_HOLIDAYS      # 4 Jul 2026 is a Saturday
        assert date(2027, 7, 5) in NYSE_HOLIDAYS      # 4 Jul 2027 is a Sunday
        assert date(2027, 12, 31) not in NYSE_HOLIDAYS  # NYSE does not observe NYD 2028 in 2027

    def test_TRIPWIRE_the_list_covers_the_current_year(self):
        # When this fails, extend NYSE_HOLIDAYS and STATIC_COVERED_YEARS. Until then every API outage
        # in an uncovered year stands the agent down for the day.
        assert date.today().year in STATIC_COVERED_YEARS, (
            f"core/market_calendar.py has no holidays for {date.today().year}"
        )


def test_get_market_session_dates_reads_the_calendar_through_the_trading_client(monkeypatch):
    from types import SimpleNamespace
    from core import alpaca

    class _Client:
        def get_calendar(self, req):
            assert req.start == NORMAL_TUESDAY and req.end == NORMAL_TUESDAY
            return [SimpleNamespace(date=NORMAL_TUESDAY)]

    monkeypatch.setattr(alpaca, "_client", lambda: _Client())
    assert alpaca.get_market_session_dates(NORMAL_TUESDAY, NORMAL_TUESDAY) == {NORMAL_TUESDAY}
