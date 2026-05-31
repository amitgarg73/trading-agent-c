from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from sessions.watchdog import find_orphaned_sessions, run_watchdog, STALE_HOURS, _parse_ts


def _make_client(rows: list[dict]) -> MagicMock:
    q = MagicMock()
    q.select.return_value = q
    q.lt.return_value = q
    q.update.return_value = q
    q.eq.return_value = q
    q.execute.return_value = MagicMock(data=rows)
    q.table.return_value = q
    client = MagicMock()
    client.table.return_value = q
    return client


_OLD = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS + 1)).isoformat()
_NEW = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


class TestParseTs:
    def test_standard_iso(self):
        dt = _parse_ts("2026-05-28T06:56:38.123456+00:00")
        assert dt.year == 2026 and dt.second == 38

    def test_five_digit_fractional(self):
        dt = _parse_ts("2026-05-28T06:56:38.00743+00:00")
        assert dt.year == 2026 and dt.second == 38

    def test_z_suffix(self):
        dt = _parse_ts("2026-05-28T06:56:38.123456Z")
        assert dt.tzinfo is not None

    def test_no_fractional(self):
        dt = _parse_ts("2026-05-28T06:56:38+00:00")
        assert dt.second == 38


class TestFindOrphanedSessions:
    def test_returns_in_progress_sessions(self):
        rows = [{"id": "aaa", "date": "2026-05-30", "started_at": _OLD,
                 "terminal_reason": "in_progress", "total_cost_usd": 0.08}]
        client = _make_client(rows)
        result = find_orphaned_sessions(client, datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS))
        assert len(result) == 1
        assert result[0]["id"] == "aaa"

    def test_returns_empty_terminal_reason_sessions(self):
        rows = [{"id": "bbb", "date": "2026-05-30", "started_at": _OLD,
                 "terminal_reason": "", "total_cost_usd": 1.98}]
        client = _make_client(rows)
        result = find_orphaned_sessions(client, datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS))
        assert len(result) == 1

    def test_excludes_completed_sessions(self):
        rows = [
            {"id": "ccc", "date": "2026-05-30", "started_at": _OLD,
             "terminal_reason": "converged", "total_cost_usd": 0.07},
            {"id": "ddd", "date": "2026-05-30", "started_at": _OLD,
             "terminal_reason": "structural_block", "total_cost_usd": 0.06},
        ]
        client = _make_client(rows)
        result = find_orphaned_sessions(client, datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS))
        assert result == []

    def test_excludes_error_sessions(self):
        rows = [{"id": "eee", "date": "2026-05-30", "started_at": _OLD,
                 "terminal_reason": "error", "total_cost_usd": 0.05}]
        client = _make_client(rows)
        result = find_orphaned_sessions(client, datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS))
        assert result == []


class TestRunWatchdog:
    def test_closes_orphaned_sessions(self, mock_supabase):
        rows = [
            {"id": "aaa", "date": "2026-05-30", "started_at": _OLD,
             "terminal_reason": "in_progress", "total_cost_usd": 0.08},
            {"id": "bbb", "date": "2026-05-30", "started_at": _OLD,
             "terminal_reason": "", "total_cost_usd": 1.98},
        ]
        q = MagicMock()
        q.select.return_value = q
        q.lt.return_value = q
        q.update.return_value = q
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=rows)
        mock_supabase.table.return_value = q

        closed = run_watchdog(dry_run=False)
        assert set(closed) == {"aaa", "bbb"}

    def test_dry_run_makes_no_writes(self, mock_supabase):
        rows = [{"id": "aaa", "date": "2026-05-30", "started_at": _OLD,
                 "terminal_reason": "in_progress", "total_cost_usd": 0.08}]
        q = MagicMock()
        q.select.return_value = q
        q.lt.return_value = q
        q.update.return_value = q
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=rows)
        mock_supabase.table.return_value = q

        closed = run_watchdog(dry_run=True)
        assert closed == []
        q.update.assert_not_called()

    def test_no_orphans_returns_empty(self, mock_supabase):
        q = MagicMock()
        q.select.return_value = q
        q.lt.return_value = q
        q.execute.return_value = MagicMock(data=[])
        mock_supabase.table.return_value = q

        closed = run_watchdog(dry_run=False)
        assert closed == []
