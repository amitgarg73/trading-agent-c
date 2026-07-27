"""
The agent's own memory of its runs.

The bug these guard against: between 2026-07-25 and 2026-07-27 the agent answered every question
about itself by reading Provy's database. Provy split its databases, telemetry started landing in
one project while the agent kept reading another, and premarket began creating runs that the very
next lookup could not find. Three days of sessions died on that line, silently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import run_state
from tests.conftest import make_query


def _rows(mock_supabase, data):
    q = make_query(data)
    mock_supabase.table.return_value = q
    return q


# ── open / close ──────────────────────────────────────────────────────────────


class TestOpenRun:
    def test_writes_run_with_facts_known_at_open(self, mock_supabase, monkeypatch):
        monkeypatch.setenv("WORKFLOW_ID", "wf-c")
        q = _rows(mock_supabase, [])
        run_state.open_run("run-1", "premarket")

        row = q.upsert.call_args[0][0]
        assert row["id"] == "run-1"
        assert row["session_type"] == "premarket"
        assert row["workflow_id"] == "wf-c"
        assert row["status"] == "in_progress"
        assert row["is_simulated"] is False

    def test_is_idempotent_so_a_retry_cannot_reset_started_at(self, mock_supabase):
        """The concurrency guard reads started_at to tell a live run from a stale one. If a retry
        overwrote it, a run stuck for hours would keep looking newly started and keep blocking."""
        q = _rows(mock_supabase, [])
        run_state.open_run("run-1", "premarket")
        assert q.upsert.call_args[1]["ignore_duplicates"] is True
        assert q.upsert.call_args[1]["on_conflict"] == "id"

    def test_records_parent_for_intraday(self, mock_supabase):
        q = _rows(mock_supabase, [])
        run_state.open_run("run-2", "intraday", parent_run_id="run-1")
        assert q.upsert.call_args[0][0]["parent_session_id"] == "run-1"

    def test_simulated_flag_is_set_at_open_not_after(self, mock_supabase):
        q = _rows(mock_supabase, [])
        run_state.open_run("run-3", "premarket", is_simulated=True)
        assert q.upsert.call_args[0][0]["is_simulated"] is True


class TestCloseRun:
    def test_marks_completed_with_reason(self, mock_supabase):
        q = _rows(mock_supabase, [])
        run_state.close_run("run-1", "converged", trades_executed=2)
        fields = q.update.call_args[0][0]
        assert fields["status"] == "completed"
        assert fields["terminal_reason"] == "converged"
        assert fields["trades_executed"] == 2
        assert fields["completed_at"]


# ── lookups ───────────────────────────────────────────────────────────────────


class TestTodaysRun:
    def test_returns_id_when_present(self, mock_supabase):
        _rows(mock_supabase, [{"id": "run-1"}])
        assert run_state.today_premarket_run_id() == "run-1"

    def test_returns_none_when_absent(self, mock_supabase):
        _rows(mock_supabase, [])
        assert run_state.today_premarket_run_id() is None

    def test_guard_lookup_excludes_simulated_runs(self, mock_supabase):
        """A mid-morning rehearsal must not suppress the next real premarket. The old lookup read
        a table where nothing filtered this flag, so it did exactly that."""
        q = _rows(mock_supabase, [])
        run_state.today_premarket_run()
        assert ("is_simulated", False) in [c[0] for c in q.eq.call_args_list]

    def test_day_key_lookup_does_not_filter_simulated(self, mock_supabase):
        q = _rows(mock_supabase, [{"id": "run-1"}])
        run_state.today_premarket_run_id()
        assert ("is_simulated", False) not in [c[0] for c in q.eq.call_args_list]


# ── pending trades ────────────────────────────────────────────────────────────


class TestPendingTrades:
    def test_returns_trades_deferred_past_the_open(self, mock_supabase):
        _rows(mock_supabase, [{"id": "run-1", "pending_trades": [{"ticker": "DE"}]}])
        assert run_state.get_pending_trades("run-1") == [{"ticker": "DE"}]

    def test_missing_run_yields_no_trades_rather_than_raising(self, mock_supabase):
        _rows(mock_supabase, [])
        assert run_state.get_pending_trades("run-1") == []

    def test_null_column_yields_no_trades(self, mock_supabase):
        _rows(mock_supabase, [{"id": "run-1", "pending_trades": None}])
        assert run_state.get_pending_trades("run-1") == []

    def test_clearing_writes_an_empty_list(self, mock_supabase):
        q = _rows(mock_supabase, [])
        run_state.clear_pending_trades("run-1")
        assert q.update.call_args[0][0] == {"pending_trades": []}


# ── entry scan pacing ─────────────────────────────────────────────────────────


class TestEntryScanPacing:
    def test_stamp_writes_a_timestamp(self, mock_supabase):
        q = _rows(mock_supabase, [])
        run_state.stamp_entry_scan("run-1")
        assert "last_entry_scan_at" in q.update.call_args[0][0]

    def test_reads_back_as_aware_utc(self, mock_supabase):
        _rows(mock_supabase, [{"id": "run-1", "last_entry_scan_at": "2026-07-27T15:36:25.669+00:00"}])
        got = run_state.last_entry_scan_at("run-1")
        assert got is not None and got.tzinfo is not None

    def test_never_scanned_yields_none(self, mock_supabase):
        _rows(mock_supabase, [{"id": "run-1", "last_entry_scan_at": None}])
        assert run_state.last_entry_scan_at("run-1") is None


class TestParseTs:
    """Every caller subtracts these from 'now'. A naive result raises TypeError against an aware
    now, and the old call sites swallowed that into a wrong answer rather than a loud failure."""

    def test_offset_form_is_preserved(self):
        assert run_state.parse_ts("2026-07-27T15:36:25+00:00").tzinfo is not None

    def test_zulu_form_is_accepted(self):
        assert run_state.parse_ts("2026-07-27T15:36:25Z").tzinfo is not None

    def test_naive_string_is_treated_as_utc(self):
        assert run_state.parse_ts("2026-07-27T15:36:25").tzinfo == timezone.utc

    def test_result_can_be_subtracted_from_aware_now(self):
        got = run_state.parse_ts("2026-07-27T15:36:25+00:00")
        assert isinstance(datetime.now(timezone.utc) - got, timedelta)

    @pytest.mark.parametrize("bad", [None, "", "not-a-date"])
    def test_unusable_values_yield_none(self, bad):
        assert run_state.parse_ts(bad) is None
