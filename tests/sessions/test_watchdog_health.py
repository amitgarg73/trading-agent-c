"""
The watchdog has to be able to fail.

Between 25 and 27 July 2026 every scheduled session died and this watchdog reported success
every hour throughout, because "no orphaned sessions found" is what a completely dead agent
looks like. These tests assert the watchdog now notices an ABSENCE of work.

The first test is the July outage itself: a normal trading morning where nothing ran.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
import pytz

from sessions.watchdog import POSITION_WATCHDOG_JOB, check_expected_work

_ET = pytz.timezone("America/New_York")


def _at(hour: int, minute: int = 0, day: int = 27):
    """A Monday in July 2026 unless told otherwise."""
    return _ET.localize(datetime(2026, 7, day, hour, minute))


def _state(premarket=None, perf=True, heartbeat_age=0.0, trading_day=True):
    """Patch everything the checks read. Defaults describe a healthy agent."""
    def _today_run(session_type, *_a, **_kw):
        return {"premarket": premarket}.get(session_type)

    return (
        patch("core.run_state.today_run", side_effect=_today_run),
        patch("core.run_state.performance_recorded", return_value=perf),
        patch("core.run_state.heartbeat_age_minutes", return_value=heartbeat_age),
        patch("core.agent_config.is_trading_day", return_value=trading_day),
    )


def _run(now, **kw):
    a, b, c, d = _state(**kw)
    with a, b, c, d:
        return check_expected_work(now)


_COMPLETED = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "status": "completed"}


class TestTheJulyOutage:
    def test_a_trading_morning_where_nothing_ran_is_reported(self):
        """The exact condition that went unnoticed for three days."""
        problems = _run(_at(11, 30), premarket=None, heartbeat_age=None)
        assert len(problems) == 2
        joined = " ".join(problems)
        assert "No premarket run" in joined
        assert "position watchdog" in joined

    def test_a_healthy_morning_is_silent(self):
        assert _run(_at(11, 30), premarket=_COMPLETED, heartbeat_age=5.0) == []


class TestPremarket:
    def test_missing_premarket_after_the_window_alerts(self):
        problems = _run(_at(11, 30), premarket=None, heartbeat_age=5.0)
        assert any("No premarket run" in p for p in problems)

    def test_not_flagged_before_the_window_closes(self):
        """Premarket runs 06:00-10:30. At 09:00 its absence is not yet news."""
        assert _run(_at(9, 0), premarket=None, heartbeat_age=5.0) == []

    def test_started_but_never_finished_alerts(self):
        stuck = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "status": "in_progress"}
        problems = _run(_at(11, 30), premarket=stuck, heartbeat_age=5.0)
        assert any("never finished" in p for p in problems)


class TestPositionWatchdog:
    def test_stale_heartbeat_during_market_hours_alerts(self):
        problems = _run(_at(12, 0), premarket=_COMPLETED, heartbeat_age=90.0)
        assert any("last ran 90 minutes ago" in p for p in problems)

    def test_never_reported_alerts(self):
        problems = _run(_at(12, 0), premarket=_COMPLETED, heartbeat_age=None)
        assert any("never reported in" in p for p in problems)

    def test_recent_heartbeat_is_silent(self):
        assert _run(_at(12, 0), premarket=_COMPLETED, heartbeat_age=12.0) == []

    def test_not_checked_outside_market_hours(self):
        """It is not supposed to be polling at 08:00, so silence then is correct."""
        assert _run(_at(8, 0), premarket=_COMPLETED, heartbeat_age=None) == []


class TestEndOfDay:
    def test_missing_performance_after_the_close_alerts(self):
        problems = _run(_at(17, 0), premarket=_COMPLETED, perf=False, heartbeat_age=5.0)
        assert any("No end-of-day performance" in p for p in problems)

    def test_not_flagged_before_eod_is_due(self):
        assert _run(_at(14, 0), premarket=_COMPLETED, perf=False, heartbeat_age=5.0) == []

    def test_recorded_performance_is_silent(self):
        assert _run(_at(17, 0), premarket=_COMPLETED, perf=True, heartbeat_age=5.0) == []

    def test_a_running_eod_is_not_reported_as_missing(self):
        """
        The 27 July false alarm. EOD ran, closed out and emailed the P&L, but it runs under the
        premarket session id, so there was no session_type='eod' row for the old check to find and
        it alerted every hour from 16:30 ET onward. The performance row is what EOD really writes.
        """
        assert _run(_at(21, 42), premarket=_COMPLETED, perf=True, heartbeat_age=5.0) == []


class TestNonTradingDays:
    def test_weekend_is_never_flagged(self):
        """Sunday 26 July. Nothing is expected to run, so nothing is missing."""
        assert _run(_at(12, 0, day=26), premarket=None, heartbeat_age=None,
                    trading_day=False) == []


class TestAlerting:
    def test_problems_are_alerted_and_returned(self):
        from sessions.watchdog import run_health_checks
        a, b, c, d = _state(premarket=None, heartbeat_age=None)
        with a, b, c, d, patch("core.alerts.send_alert") as alert:
            problems = run_health_checks(_at(11, 30))
        assert problems
        assert alert.called
        subject, body = alert.call_args[0]
        assert "not doing its work" in subject
        assert "premarket" in body.lower()

    def test_a_healthy_check_sends_nothing(self):
        from sessions.watchdog import run_health_checks
        a, b, c, d = _state(premarket=_COMPLETED, heartbeat_age=5.0)
        with a, b, c, d, patch("core.alerts.send_alert") as alert:
            problems = run_health_checks(_at(11, 30))
        assert problems == []
        assert not alert.called
