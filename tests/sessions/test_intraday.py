from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sessions.intraday import (
    classify_exit,
    count_open_positions,
    get_daily_pnl,
    get_last_entry_scan_time,
    get_open_positions,
    get_premarket_session_id,
    get_today_session_id,  # backwards-compatible alias
    get_today_tickers,
)
from tests.conftest import make_query

_SESSION_ID    = "sess-pre-0001"
_INTRA_ID_1    = "sess-intra-0001"
_INTRA_ID_2    = "sess-intra-0002"


class TestGetPremarketSessionId:
    def test_returns_id_when_row_exists(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"id": _SESSION_ID}])
        result = get_premarket_session_id()
        assert result == _SESSION_ID

    def test_returns_none_when_no_rows(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = get_premarket_session_id()
        assert result is None

    def test_alias_matches(self, mock_supabase):
        """get_today_session_id is a backwards-compatible alias for get_premarket_session_id."""
        mock_supabase.table.return_value = make_query([{"id": _SESSION_ID}])
        assert get_today_session_id() == get_premarket_session_id()


class TestGetDailyPnl:
    def test_sums_realized_pnl(self, mock_supabase):
        rows = [{"realized_pnl": 100.0}, {"realized_pnl": -30.0}, {"realized_pnl": None}]
        mock_supabase.table.return_value = make_query(rows)
        assert get_daily_pnl(_SESSION_ID) == 70.0

    def test_returns_zero_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_daily_pnl(_SESSION_ID) == 0.0


class TestCountOpenPositions:
    def test_returns_count(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"id": "a"}, {"id": "b"}])
        assert count_open_positions(_SESSION_ID) == 2

    def test_returns_zero_when_none(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert count_open_positions(_SESSION_ID) == 0


class TestGetTodayTickers:
    def test_returns_set_of_tickers(self, mock_supabase):
        rows = [{"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "JPM"}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_today_tickers(_SESSION_ID)
        assert result == {"AAPL", "MSFT", "JPM"}

    def test_deduplicates_same_ticker(self, mock_supabase):
        rows = [{"ticker": "PG"}, {"ticker": "PG"}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_today_tickers(_SESSION_ID)
        assert result == {"PG"}

    def test_ignores_none_ticker(self, mock_supabase):
        rows = [{"ticker": "AAPL"}, {"ticker": None}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_today_tickers(_SESSION_ID)
        assert None not in result

    def test_returns_empty_set_when_no_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = get_today_tickers(_SESSION_ID)
        assert result == set()


class TestGetOpenPositions:
    def test_returns_open_position_rows(self, mock_supabase):
        rows = [
            {"ticker": "NVDA", "entry_price": 890.0, "unrealized_pnl": 142.0},
            {"ticker": "MSFT", "entry_price": 420.0, "unrealized_pnl": -20.0},
        ]
        mock_supabase.table.return_value = make_query(rows)
        result = get_open_positions(_SESSION_ID)
        assert len(result) == 2
        assert result[0]["ticker"] == "NVDA"

    def test_filters_out_none_ticker(self, mock_supabase):
        rows = [{"ticker": "AAPL", "entry_price": 200.0, "unrealized_pnl": 0.0},
                {"ticker": None, "entry_price": 150.0, "unrealized_pnl": 0.0}]
        mock_supabase.table.return_value = make_query(rows)
        result = get_open_positions(_SESSION_ID)
        assert all(r["ticker"] is not None for r in result)

    def test_returns_empty_when_no_open_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        result = get_open_positions(_SESSION_ID)
        assert result == []


class TestGetLastEntryScanTime:
    """get_last_entry_scan_time takes the premarket_session_id, queries ag_sessions
    for child intraday sessions, then queries ag_traces for scan-outcome decisions."""

    def _setup_two_queries(self, mock_supabase, intraday_ids: list[str], trace_rows: list[dict]):
        """Return different query results for the two DB calls:
        first call → ag_sessions (intraday children), second call → ag_traces."""
        calls = iter([
            make_query([{"id": sid} for sid in intraday_ids]),
            make_query(trace_rows),
        ])
        mock_supabase.table.side_effect = lambda _: next(calls)

    def test_returns_none_when_no_intraday_sessions(self, mock_supabase):
        self._setup_two_queries(mock_supabase, [], [])
        assert get_last_entry_scan_time(_SESSION_ID) is None

    def test_returns_none_when_no_scan_decisions(self, mock_supabase):
        trace_rows = [{"created_at": "2026-05-27T14:00:00", "outcome": "lock_in_mode"}]
        self._setup_two_queries(mock_supabase, [_INTRA_ID_1], trace_rows)
        assert get_last_entry_scan_time(_SESSION_ID) is None

    def test_returns_datetime_for_scan_outcome(self, mock_supabase):
        trace_rows = [
            {"created_at": "2026-05-27T14:30:00", "outcome": "intraday_entries_placed"},
            {"created_at": "2026-05-27T14:00:00", "outcome": "lock_in_mode"},
        ]
        self._setup_two_queries(mock_supabase, [_INTRA_ID_1], trace_rows)
        result = get_last_entry_scan_time(_SESSION_ID)
        assert result == datetime(2026, 5, 27, 14, 30, 0)

    def test_returns_most_recent_scan_outcome_across_sessions(self, mock_supabase):
        trace_rows = [
            {"created_at": "2026-05-27T15:00:00", "outcome": "no_intraday_candidates"},
            {"created_at": "2026-05-27T14:00:00", "outcome": "intraday_all_rejected"},
        ]
        self._setup_two_queries(mock_supabase, [_INTRA_ID_1, _INTRA_ID_2], trace_rows)
        result = get_last_entry_scan_time(_SESSION_ID)
        assert result == datetime(2026, 5, 27, 15, 0, 0)

    def test_returns_none_when_no_trace_rows(self, mock_supabase):
        self._setup_two_queries(mock_supabase, [_INTRA_ID_1], [])
        assert get_last_entry_scan_time(_SESSION_ID) is None


class TestClassifyExit:
    def test_limit_sell_is_target(self):
        assert classify_exit({"order_type": "limit", "side": "sell"}) == "target"

    def test_stop_sell_is_stop(self):
        assert classify_exit({"order_type": "stop", "side": "sell"}) == "stop"

    def test_market_sell_is_eod_forced(self):
        assert classify_exit({"order_type": "market", "side": "sell"}) == "eod_forced"

    def test_unknown_is_manual(self):
        assert classify_exit({"order_type": "other", "side": "buy"}) == "manual"

    def test_trailing_stop_sell_is_native_trail(self):
        assert classify_exit({"order_type": "trailing_stop", "side": "sell"}) == "NATIVE_TRAIL"

    def test_trailing_stop_buy_is_manual(self):
        assert classify_exit({"order_type": "trailing_stop", "side": "buy"}) == "manual"


_BRACKET_PATCH = patch("core.alpaca.submit_bracket_order", return_value=("ord-intra-001", 185.0))
_TRAIL_PATCH   = patch("core.alpaca.submit_trailing_stop", return_value="trail-intra-001")

_INTRA_PROPOSAL = {
    "proposals": [
        {
            "ticker": "AAPL",
            "entry_price": 185.0,
            "target_price": 192.4,
            "stop_loss": 183.78,
            "position_size": 3500,
            "shares": 18,
            "confidence": "HIGH",
        },
    ]
}


class TestPlaceIntradayTrades:
    def test_inserts_approved_trades(self, mock_supabase):
        inserted = {}

        def capture(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        from sessions.intraday import _place_intraday_trades
        with _BRACKET_PATCH, _TRAIL_PATCH:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 1
        assert inserted.get("entry_context") == "intraday"
        assert inserted.get("alpaca_order_id") == "ord-intra-001"
        assert inserted.get("trail_order_id") == "trail-intra-001"

    def test_trail_order_id_none_when_fill_pending(self, mock_supabase):
        inserted = {}

        def capture(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=("ord-intra-001", None)), \
             _TRAIL_PATCH as mock_trail:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 1
        mock_trail.assert_not_called()
        assert inserted.get("trail_order_id") is None

    def test_skips_non_approved(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        proposals = {
            "proposals": [
                {
                    "ticker": "MSFT",
                    "entry_price": 300.0,
                    "target_price": 310.0,
                    "stop_loss": 295.0,
                    "position_size": 3000,
                    "shares": 10,
                    "confidence": "MEDIUM",
                },
            ]
        }
        from sessions.intraday import _place_intraday_trades
        with _BRACKET_PATCH, _TRAIL_PATCH:
            count = _place_intraday_trades(proposals, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 0

    def test_skips_rejected_order(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=(None, None)), _TRAIL_PATCH:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008)
        assert count == 0

    def test_rejected_order_logs_tool_call_without_error(self, mock_supabase):
        """log_tool_call must not crash when order is rejected (regression for TypeError: unexpected keyword 'outcome')."""
        mock_supabase.table.return_value = make_query([])
        mock_tracer = MagicMock()
        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=(None, None)), _TRAIL_PATCH:
            count = _place_intraday_trades(_INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008, tracer=mock_tracer)
        assert count == 0
        mock_tracer.log_tool_call.assert_called_once()
        call_args = mock_tracer.log_tool_call.call_args
        # positional: agent, tool_name, tool_input (dict), tool_output (dict)
        assert call_args.args[0] == "orchestrator"
        assert call_args.args[1] == "submit_bracket_order"
        assert isinstance(call_args.args[2], dict)  # tool_input
        assert isinstance(call_args.args[3], dict)  # tool_output
        assert call_args.args[3].get("outcome") == "rejected"

    def test_hard_gate_blocks_ticker_already_entered_today(self, mock_supabase):
        """If AAPL was already entered today (open or closed), hard gate must skip it."""
        mock_supabase.table.return_value = make_query([])
        from sessions.intraday import _place_intraday_trades
        with _BRACKET_PATCH as mock_bracket, _TRAIL_PATCH:
            count = _place_intraday_trades(
                _INTRA_PROPOSAL, {"AAPL"}, _SESSION_ID, 0.008,
                today_tickers={"AAPL"},
            )
        assert count == 0
        mock_bracket.assert_not_called()

    def test_hard_gate_allows_different_ticker(self, mock_supabase):
        """Hard gate only blocks the already-entered ticker; other approved tickers proceed."""
        inserted = {}

        def capture(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        proposals = {
            "proposals": [
                {
                    "ticker": "MSFT",
                    "entry_price": 300.0,
                    "target_price": 315.0,
                    "stop_loss": 294.0,
                    "position_size": 3000,
                    "shares": 10,
                    "confidence": "HIGH",
                },
            ]
        }
        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=("ord-msft-001", 300.0)), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-msft-001"):
            count = _place_intraday_trades(
                proposals, {"MSFT"}, _SESSION_ID, 0.008,
                today_tickers={"AAPL"},  # AAPL blocked, MSFT not
            )
        assert count == 1
        assert inserted.get("ticker") == "MSFT"

    def test_hard_gate_blocks_duplicate_within_same_batch(self, mock_supabase):
        """If two proposals for the same ticker arrive in one batch, only the first goes through."""
        inserted_tickers = []

        def capture(data):
            inserted_tickers.append(data.get("ticker"))
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture
        mock_supabase.table.return_value = q

        proposals = {
            "proposals": [
                {
                    "ticker": "PG",
                    "entry_price": 145.0,
                    "target_price": 152.0,
                    "stop_loss": 143.0,
                    "position_size": 2900,
                    "shares": 20,
                    "confidence": "MEDIUM",
                },
                {
                    "ticker": "PG",
                    "entry_price": 146.0,
                    "target_price": 153.0,
                    "stop_loss": 144.0,
                    "position_size": 2920,
                    "shares": 20,
                    "confidence": "LOW",
                },
            ]
        }
        from sessions.intraday import _place_intraday_trades
        with patch("core.alpaca.submit_bracket_order", return_value=("ord-pg-001", 145.0)), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-pg-001"):
            count = _place_intraday_trades(
                proposals, {"PG"}, _SESSION_ID, 0.008,
                today_tickers=set(),
            )
        assert count == 1
        assert inserted_tickers == ["PG"]


def _old_entry_time() -> str:
    """Return an entry_time 35 minutes ago — old enough to trigger the cancel gate."""
    return (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()


def _fresh_entry_time() -> str:
    """Return an entry_time 10 minutes ago — within the 30-minute hold window."""
    return (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()


class TestSyncPositions:
    """Tests for _sync_positions: trail submission and exit detection."""

    _OPEN_POS = {
        "id": "pos-001",
        "ticker": "AAPL",
        "alpaca_order_id": "ord-001",
        "trail_order_id": None,
        "entry_price": 185.0,
        "shares": 10,
        "entry_time": None,  # None → gate falls through, cancel proceeds immediately
    }

    def test_submits_trailing_stop_when_missing(self, mock_supabase):
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": True, "entry_price": 185.0}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg"), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_called_once_with("AAPL", 10, 0.008)

    def test_backfills_entry_price_on_fill_confirmation(self, mock_supabase):
        pos = {**self._OPEN_POS, "entry_price": 184.0}  # original requested price differs from fill
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        bracket_status = {"entry_filled": True, "entry_price": 185.5}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg"), \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("entry_price") == 185.5

    def test_skips_trail_when_entry_not_yet_filled(self, mock_supabase):
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": False, "entry_price": None}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca.submit_trailing_stop") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_not_called()

    def test_skips_trail_submission_when_already_set(self, mock_supabase):
        pos = {**self._OPEN_POS, "trail_order_id": "trail-existing"}
        mock_supabase.table.return_value = make_query([pos])
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.submit_trailing_stop") as mock_trail:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_trail.assert_not_called()

    def test_closes_position_on_trail_exit(self, mock_supabase):
        pos = {**self._OPEN_POS, "trail_order_id": "trail-001"}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(187.5, "NATIVE_TRAIL")):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "NATIVE_TRAIL"

    def test_cancels_stop_leg_before_trail_retry(self, mock_supabase):
        """_cancel_bracket_stop_leg is called before submitting the trailing stop."""
        mock_supabase.table.return_value = make_query([self._OPEN_POS])
        bracket_status = {"entry_filled": True, "entry_price": 185.0}
        with patch("core.alpaca.get_open_alpaca_tickers", return_value={"AAPL"}), \
             patch("core.alpaca.get_position_data", return_value={"current_price": 186.0}), \
             patch("core.alpaca.get_bracket_status", return_value=bracket_status), \
             patch("core.alpaca._cancel_bracket_stop_leg") as mock_cancel, \
             patch("core.alpaca.submit_trailing_stop", return_value="trail-001"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_cancel.assert_called_once_with("ord-001")

    def test_unfilled_limit_order_marked_unfilled_not_external_close(self, mock_supabase):
        """Limit order that never filled should be cancelled and marked unfilled with 0 P&L."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order") as mock_cancel:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "unfilled"
        assert updated.get("realized_pnl") == pytest.approx(0.0)
        mock_cancel.assert_called_once_with("ord-001")

    def test_unfilled_does_not_use_external_close(self, mock_supabase):
        """Unfilled limit order must not produce an external_close exit reason."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order"):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("exit_reason") != "external_close"

    def test_external_close_uses_last_trade_price(self, mock_supabase):
        """When entry filled but position gone with no fill record, close at last trade price."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        trade_mock = MagicMock()
        trade_mock.price = 188.0

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": True}), \
             patch("core.alpaca._dclient") as mock_dc:
            mock_dc.return_value.get_stock_latest_trade.return_value = {"AAPL": trade_mock}
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "external_close"
        assert updated.get("exit_price") == pytest.approx(188.0)
        assert updated.get("realized_pnl") == pytest.approx((188.0 - 185.0) * 10)

    def test_external_close_falls_back_to_entry_price_on_data_failure(self, mock_supabase):
        """When last trade fetch fails, falls back to entry_price so the row is still closed."""
        pos = {**self._OPEN_POS, "trail_order_id": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": True}), \
             patch("core.alpaca._dclient", side_effect=Exception("rate limit")):
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        assert updated.get("status") == "closed"
        assert updated.get("exit_reason") == "external_close"
        assert updated.get("realized_pnl") == pytest.approx(0.0)  # entry_price == exit_price

    def test_no_rows_returns_early(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.alpaca.get_open_alpaca_tickers") as mock_tickers:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_tickers.assert_not_called()

    def test_fresh_unfilled_order_not_cancelled(self, mock_supabase):
        """Order submitted <30m ago must not be cancelled — it hasn't had a chance to fill."""
        pos = {**self._OPEN_POS, "entry_time": _fresh_entry_time()}
        mock_supabase.table.return_value = make_query([pos])

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order") as mock_cancel:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_cancel.assert_not_called()

    def test_old_unfilled_order_cancelled_after_30m(self, mock_supabase):
        """Order pending >30m with no fill should be cancelled and marked unfilled."""
        pos = {**self._OPEN_POS, "entry_time": _old_entry_time()}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order") as mock_cancel:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_cancel.assert_called_once_with("ord-001")
        assert updated.get("exit_reason") == "unfilled"

    def test_missing_entry_time_cancels_immediately(self, mock_supabase):
        """Positions without entry_time (old rows) cancel on the next cycle as before."""
        pos = {**self._OPEN_POS, "entry_time": None}
        updated = {}

        def capture_update(data):
            updated.update(data)
            q = make_query([])
            q.eq = lambda *a, **k: q
            return q

        q = make_query([pos])
        q.update.side_effect = capture_update
        mock_supabase.table.return_value = q

        with patch("core.alpaca.get_open_alpaca_tickers", return_value=set()), \
             patch("core.alpaca.get_order_fill", return_value=(None, None)), \
             patch("core.alpaca.get_bracket_status", return_value={"entry_filled": False}), \
             patch("core.alpaca.cancel_order") as mock_cancel:
            from sessions.intraday import _sync_positions
            _sync_positions(_SESSION_ID, 0.008)
        mock_cancel.assert_called_once()
        assert updated.get("exit_reason") == "unfilled"


class TestIntradayMain:
    def _mock_protection(self, suspended=False):
        p = MagicMock()
        p.suspended = suspended
        p.reason = ""
        return p

    def _mock_goal(self, lock_in=False, pnl_floor=False, daily_target=500.0):
        g = MagicMock()
        g.lock_in_mode = lock_in
        g.pnl_floor_hit = pnl_floor
        g.daily_target = daily_target
        return g

    def _mock_params(self):
        from core.params import StrategyParams
        p = StrategyParams()
        p.max_positions = 3
        return p

    def test_exits_on_non_trading_day(self, mock_supabase, capsys):
        with patch("sessions.intraday.is_trading_day", return_value=False):
            from sessions.intraday import main
            main()
        assert "Not a trading day" in capsys.readouterr().out

    def test_exits_outside_poll_window(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 8, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "Outside poll window" in capsys.readouterr().out

    def test_exits_when_no_session_and_pipeline_produces_nothing(self, mock_supabase, capsys):
        """If premarket pipeline runs but creates no session (e.g. no candidates), exit cleanly."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        # get_premarket_session_id returns None both before and after the pipeline runs
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=None), \
             patch("sessions.premarket.main"):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "Premarket pipeline produced no session" in capsys.readouterr().out

    def test_dispatches_full_pipeline_when_no_session(self, mock_supabase, capsys):
        """Any time no premarket session exists, intraday runs the full pipeline with bypass_checks=True."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 30, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=None), \
             patch("sessions.premarket.main") as mock_pm:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        mock_pm.assert_called_once_with(bypass_checks=True)
        assert "full pipeline" in capsys.readouterr().out

    def test_dispatches_full_pipeline_past_old_10am_cutoff(self, mock_supabase, capsys):
        """At 1 PM ET (past the old 10:30 AM gate), still dispatches the full premarket pipeline."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 13, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=None), \
             patch("sessions.premarket.main") as mock_pm:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        mock_pm.assert_called_once_with(bypass_checks=True)

    def test_exits_when_protection_suspended(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection(suspended=True)):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            from sessions.intraday import main
            main()
        assert "Protection suspended" in capsys.readouterr().out

    def test_exits_in_lock_in_mode(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config", return_value={}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=500.0), \
             patch("sessions.intraday.evaluate_goals",
                   return_value=self._mock_goal(lock_in=True)), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "Lock-in mode" in capsys.readouterr().out

    def test_skips_when_entry_scan_too_recent(self, mock_supabase, capsys):
        import pytz
        from datetime import timedelta
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 30, tzinfo=_ET)
        recent_scan = datetime.utcnow() - timedelta(minutes=20)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": True,
                                 "intraday_entry_min_interval_mins": 55}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday.get_last_entry_scan_time", return_value=recent_scan), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.utcnow.return_value = datetime.utcnow()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "too recent" in capsys.readouterr().out

    def test_entries_disabled_exits_cleanly(self, mock_supabase, capsys):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals",
                   return_value=self._mock_goal()), \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        assert "Entries disabled" in capsys.readouterr().out

    def test_places_trades_when_candidates_approved(self, mock_supabase):
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        proposals = {
            "proposals": [
                {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.0,
                 "stop_loss": 183.0, "position_size": 3500, "shares": 18, "confidence": "HIGH"},
            ]
        }
        verdicts = {"verdicts": [{"ticker": "AAPL", "verdict": "APPROVED"}]}
        mock_supabase.table.return_value = make_query([])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": True, "intraday_min_score_bonus": 1,
                                 "intraday_max_new_positions": 2}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday.count_open_positions", return_value=0), \
             patch("sessions.intraday.get_today_tickers", return_value=set()), \
             patch("sessions.intraday.get_last_entry_scan_time", return_value=None), \
             patch("agents.research_agent.run_research_agent", return_value=proposals), \
             patch("agents.risk_agent.run_risk_agent", return_value=verdicts), \
             patch("sessions.intraday._place_intraday_trades", return_value=1) as mock_place, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_place.assert_called_once()

    def test_executes_pending_trades_at_market_open(self, mock_supabase, capsys):
        """Deferred premarket trade execution moved to position_watchdog.py.
        sessions/intraday.py (entry scan) no longer calls _execute_trades — watchdog handles it."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 45, tzinfo=_ET)
        _pending = [
            {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.0,
             "stop_loss": 183.0, "position_size": 3500, "shares": 18, "confidence": "HIGH"}
        ]
        mock_supabase.table.return_value = make_query([{"metadata": {"pending_trades": _pending}}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_not_called()

    def test_no_pending_execution_before_945(self, mock_supabase):
        """Before 9:45 AM, pending_trades are not checked."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 15, tzinfo=_ET)
        mock_supabase.table.return_value = make_query([{"pending_trades": [{"ticker": "X"}]}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_not_called()

    def test_no_pending_execution_at_930(self, mock_supabase):
        """At 9:30 AM (before 9:45 gate), pending_trades are not executed."""
        import pytz
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 9, 30, tzinfo=_ET)
        mock_supabase.table.return_value = make_query([{"pending_trades": [{"ticker": "X"}]}])
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id", return_value=_SESSION_ID), \
             patch("sessions.intraday.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.intraday.load_agent_config",
                   return_value={"enable_intraday_entries": False}), \
             patch("sessions.intraday.load_params", return_value=self._mock_params()), \
             patch("sessions.intraday.get_daily_pnl", return_value=0.0), \
             patch("sessions.intraday.evaluate_goals", return_value=self._mock_goal()), \
             patch("sessions.intraday._sync_positions"), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.intraday.TraceLogger") as mock_tracer_cls:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_tracer_cls.return_value = MagicMock()
            from sessions.intraday import main
            main()
        mock_exec.assert_not_called()



class TestIntradayJudge:
    """_run_semantic_evals fires after research + risk complete in intraday entry scan."""

    _PROPOSALS = {"proposals": [{"ticker": "AAPL", "entry_price": 185.0,
                                  "target_price": 192.0, "stop_loss": 183.0,
                                  "position_size": 3500, "shares": 18,
                                  "confidence": "HIGH", "evidence": ["above VWAP"]}]}
    _VERDICTS_APPROVED = {"verdicts": [{"ticker": "AAPL", "verdict": "APPROVED",
                                        "reason": "within limits"}]}
    _VERDICTS_REJECTED = {"verdicts": [{"ticker": "AAPL", "verdict": "REJECTED",
                                        "reason": "position limit"}]}

    _INTRADAY_SESSION_ID = "intra-judge-uuid-0001"

    def _run_main(self, mock_supabase, proposals, verdicts=None):
        import contextlib
        import pytz
        from tests.conftest import make_query
        mock_supabase.table.return_value = make_query([])
        _ET = pytz.timezone("America/New_York")
        fake_now = datetime(2026, 5, 27, 10, 0, tzinfo=_ET)
        mock_judge = MagicMock()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sessions.intraday.is_trading_day", return_value=True))
            mock_dt = stack.enter_context(patch("sessions.intraday.datetime"))
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            stack.enter_context(patch("sessions.intraday.get_premarket_session_id", return_value="sess-judge-01"))
            # Pin the intraday uuid4 so assertions can reference it by name.
            stack.enter_context(patch("sessions.intraday.uuid4", return_value=self._INTRADAY_SESSION_ID))
            stack.enter_context(patch("sessions.intraday.check_protection_status",
                                      return_value=MagicMock(suspended=False)))
            stack.enter_context(patch("sessions.intraday.load_agent_config",
                                      return_value={"enable_intraday_entries": True,
                                                    "intraday_min_score_bonus": 1,
                                                    "intraday_max_new_positions": 2}))
            stack.enter_context(patch("sessions.intraday.load_params",
                                      return_value=MagicMock(max_positions=3, trail_pct=0.05,
                                                              strategy_min_score=5)))
            stack.enter_context(patch("sessions.intraday.get_daily_pnl", return_value=0.0))
            stack.enter_context(patch("sessions.intraday.evaluate_goals",
                                      return_value=MagicMock(lock_in_mode=False, pnl_floor_hit=False)))
            stack.enter_context(patch("sessions.intraday.count_open_positions", return_value=0))
            stack.enter_context(patch("sessions.intraday.get_today_tickers", return_value=set()))
            stack.enter_context(patch("sessions.intraday.get_last_entry_scan_time", return_value=None))
            stack.enter_context(patch("agents.research_agent.run_research_agent", return_value=proposals))
            stack.enter_context(patch("sessions.intraday.TraceLogger", return_value=MagicMock()))
            stack.enter_context(patch("agents.orchestrator._run_semantic_evals", mock_judge))
            if verdicts is not None:
                stack.enter_context(patch("agents.risk_agent.run_risk_agent", return_value=verdicts))
                stack.enter_context(patch("sessions.intraday._place_intraday_trades", return_value=1))
            from sessions.intraday import main
            main()

        return mock_judge

    def test_judge_called_when_proposals_and_verdicts_available(self, mock_supabase):
        mock_judge = self._run_main(mock_supabase, self._PROPOSALS, self._VERDICTS_APPROVED)
        mock_judge.assert_called_once()
        args = mock_judge.call_args[0]
        # Evals are filed against the intraday session_id, not the premarket session_id.
        assert args[0] == self._INTRADAY_SESSION_ID
        assert args[1] == {}                   # market_report — empty for intraday
        assert args[2] == {}                   # scanner_result — empty for intraday
        assert args[3] == self._PROPOSALS
        assert args[4] == self._VERDICTS_APPROVED
        assert args[5] == {}                   # orchestrator_result — empty for intraday

    def test_judge_not_called_when_no_proposals(self, mock_supabase):
        empty = {"proposals": [], "skipped": [], "summary": "Nothing qualified."}
        mock_judge = self._run_main(mock_supabase, empty)  # no verdicts — risk never called
        mock_judge.assert_not_called()

    def test_judge_called_even_when_all_rejected(self, mock_supabase):
        mock_judge = self._run_main(mock_supabase, self._PROPOSALS, self._VERDICTS_REJECTED)
        mock_judge.assert_called_once()
