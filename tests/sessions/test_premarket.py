from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from sessions.premarket import (
    _build_premarket_alert,
    _execute_trades,
    _existing_session_guard,
    _run_news_analyst,
    _write_session_evals,
)
from tests.conftest import make_query


class TestWriteSessionEvals:
    """Every session must get both L5 business evals and an L4 quality score (the L4
    judge was previously only run by a manual backfill, leaving most sessions blank)."""

    def test_writes_l5_and_runs_l4_judge(self):
        with patch("sessions.premarket.write_premarket_outcome_evals") as l5, \
             patch("evals.judge.evaluate_session_from_traces") as l4:
            _write_session_evals("sess-1", trades_proposed=3, trades_approved=2,
                                 terminal_reason="executed")
            l5.assert_called_once_with(session_id="sess-1", trades_proposed=3,
                                       trades_approved=2, terminal_reason="executed")
            l4.assert_called_once_with("sess-1")

    def test_l4_judge_failure_is_non_fatal(self):
        with patch("sessions.premarket.write_premarket_outcome_evals") as l5, \
             patch("evals.judge.evaluate_session_from_traces",
                   side_effect=RuntimeError("judge down")):
            # Must not raise — a judge failure cannot break the trading session.
            _write_session_evals("sess-2", trades_proposed=0, trades_approved=0,
                                 terminal_reason="no_candidates")
            l5.assert_called_once()

_SESSION_ID = "sess-pre-0001"

_V1_REPORT = {"decision": "GO", "max_positions": 10, "bias": "BULLISH", "summary": "Clean open."}
_V2_REPORT = {"decision": "GO", "max_positions": 8, "bias": "BULLISH",
              "confidence": "MEDIUM", "key_factors": [], "summary": "Looks OK.", "circuit_breaker": None}

_TRADE = {
    "ticker": "AAPL",
    "entry_price": 185.0,
    "target_price": 192.4,
    "stop_loss": 183.78,
    "position_size": 3500,
    "shares": 18,
    "confidence": "HIGH",
    "score_at_entry": 7,
}

_RESULT_WITH_TRADES = {
    "trades": [_TRADE],
    "total_estimated_profit": 133.2,
    "total_max_loss": 21.96,
    "session_meta": {
        "terminal_reason": "converged",
        "retry_triggered": False,
    },
}

_RESULT_NO_TRADES = {
    "trades": [],
    "total_estimated_profit": 0.0,
    "total_max_loss": 0.0,
    "session_meta": {
        "terminal_reason": "skip_propagated",
        "retry_triggered": False,
    },
}


class TestRunNewsAnalyst:
    def test_returns_empty_list(self):
        assert _run_news_analyst([{"ticker": "AAPL"}]) == []


_ALPACA_PATCH = patch("core.alpaca.submit_bracket_order", return_value=("ord-001", 185.0))


class TestExecuteTrades:
    def test_inserts_one_row_per_trade(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with _ALPACA_PATCH:
            _execute_trades([_TRADE], _SESSION_ID, 0.008)
        assert mock_supabase.table.call_count >= 1
        table_call_args = [c[0][0] for c in mock_supabase.table.call_args_list]
        assert "c_positions" in table_call_args

    def test_inserts_correct_ticker(self, mock_supabase):
        inserted = {}

        def capture_insert(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture_insert
        mock_supabase.table.return_value = q

        with _ALPACA_PATCH:
            _execute_trades([_TRADE], _SESSION_ID, 0.008)
        assert inserted.get("ticker") == "AAPL"

    def test_shares_fallback_computed(self, mock_supabase):
        """If shares not present, computes int(position_size / entry_price)."""
        trade_no_shares = {**_TRADE}
        del trade_no_shares["shares"]

        inserted = {}

        def capture_insert(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture_insert
        mock_supabase.table.return_value = q

        with _ALPACA_PATCH:
            _execute_trades([trade_no_shares], _SESSION_ID, 0.008)
        assert inserted.get("shares") == int(3500 / 185.0)

    def test_status_is_open(self, mock_supabase):
        inserted = {}

        def capture_insert(data):
            inserted.update(data)
            return make_query([])

        q = make_query([])
        q.insert.side_effect = capture_insert
        mock_supabase.table.return_value = q

        with _ALPACA_PATCH:
            _execute_trades([_TRADE], _SESSION_ID, 0.008)
        assert inserted.get("status") == "open"

    def test_skips_rejected_order(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.alpaca.submit_bracket_order", return_value=(None, None)):
            _execute_trades([_TRADE], _SESSION_ID, 0.008)
        insert_calls = [c for c in mock_supabase.table.call_args_list
                        if c[0][0] == "c_positions"]
        assert len(insert_calls) == 0

    def test_no_inserts_for_empty_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with _ALPACA_PATCH:
            _execute_trades([], _SESSION_ID, 0.008)
        mock_supabase.table.assert_not_called()


class TestBuildPremarketAlert:
    def _now_et(self):
        import pytz
        return datetime(2026, 5, 27, 7, 15, tzinfo=pytz.timezone("America/New_York"))

    def test_subject_contains_date(self):
        subject, _ = _build_premarket_alert(_RESULT_WITH_TRADES, _SESSION_ID, self._now_et())
        assert "2026-05-27" in subject

    def test_subject_contains_strategy_c(self):
        subject, _ = _build_premarket_alert(_RESULT_WITH_TRADES, _SESSION_ID, self._now_et())
        assert "Strategy C" in subject

    def test_body_contains_ticker(self):
        _, body = _build_premarket_alert(_RESULT_WITH_TRADES, _SESSION_ID, self._now_et())
        assert "AAPL" in body

    def test_body_contains_terminal_reason(self):
        _, body = _build_premarket_alert(_RESULT_NO_TRADES, _SESSION_ID, self._now_et())
        assert "skip_propagated" in body

    def test_no_trades_shows_none(self):
        _, body = _build_premarket_alert(_RESULT_NO_TRADES, _SESSION_ID, self._now_et())
        assert "none" in body.lower() or "0" in body


def _make_session_row(term: str, age_seconds: int = 7200) -> dict:
    started = (datetime.utcnow() - timedelta(seconds=age_seconds)).isoformat()
    return {"id": "aaaa-bbbb-cccc-dddd-eeee", "terminal_reason": term, "started_at": started}


class TestExistingSessionGuard:
    def _patch_db(self, rows: list[dict]):
        q = MagicMock()
        q.table.return_value = q
        q.select.return_value = q
        q.eq.return_value = q
        q.gte.return_value = q
        q.order.return_value = q
        q.limit.return_value = q
        q.execute.return_value = MagicMock(data=rows)
        return patch("core.db.get_client", return_value=q)

    def test_no_sessions_today_returns_false(self):
        with self._patch_db([]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is False
        assert msg == ""

    def test_completed_session_returns_true(self):
        row = _make_session_row("converged")
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True
        assert "converged" in msg

    def test_error_session_returns_true(self):
        row = _make_session_row("error")
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True

    def test_watchdog_timeout_returns_true(self):
        row = _make_session_row("watchdog_timeout")
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True

    def test_in_progress_recent_returns_true(self):
        row = _make_session_row("in_progress", age_seconds=300)
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True
        assert "in_progress" in msg

    def test_in_progress_stale_returns_false(self):
        row = _make_session_row("in_progress", age_seconds=4000)
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is False

    def test_no_candidates_returns_true(self):
        row = _make_session_row("no_candidates")
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True

    def test_no_opportunity_returns_true(self):
        row = _make_session_row("no_opportunity")
        with self._patch_db([row]):
            skip, msg = _existing_session_guard("2026-06-01")
        assert skip is True


class TestPremarketMain:
    def _mock_protection(self, suspended=False, tier=0, reason="", resume_at=None):
        p = MagicMock()
        p.suspended = suspended
        p.tier = tier
        p.reason = reason
        p.resume_at = resume_at
        return p

    def _mock_config(self):
        return {"enable_intraday_entries": False, "enable_learning_agent": True}

    def _mock_params(self):
        from core.params import StrategyParams
        return StrategyParams()

    def test_skips_when_completed_session_exists(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket._existing_session_guard",
                   return_value=(True, "Session abc12345 already completed (converged). Skipping.")):
            from sessions.premarket import main
            main()
        out = capsys.readouterr().out
        assert "Skipping" in out

    def test_skips_when_in_progress_session_recent(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket._existing_session_guard",
                   return_value=(True, "Session abc12345 in_progress (4m old). Skipping concurrent run.")):
            from sessions.premarket import main
            main()
        out = capsys.readouterr().out
        assert "Skipping" in out

    def test_exits_on_non_trading_day(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=False):
            from sessions.premarket import main
            main()
        out = capsys.readouterr().out
        assert "Not a trading day" in out

    def test_exits_when_protection_suspended(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection(suspended=True, tier=5,
                                                       reason="drawdown", resume_at="2026-06-01")), \
             patch("sessions.premarket.send_alert") as mock_alert:
            from sessions.premarket import main
            main()
        mock_alert.assert_called_once()
        assert "Suspended" in mock_alert.call_args[0][0]

    _SHADOW_PATCHES = (
        patch("sessions.premarket.run_market_agent_v1", return_value=_V1_REPORT),
        patch("sessions.premarket._log_market_eval"),
        patch("scanner.scanner.run_scanner", return_value=5),
    )

    def test_runs_pipeline_and_executes_trades(self, mock_supabase, capsys):
        mock_supabase.table.return_value = make_query([])
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket._MARKET_OPEN", time(0, 0)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_params", return_value=self._mock_params()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket.run_premarket_pipeline",
                   return_value={**_RESULT_WITH_TRADES, "_v2_market_report": _V2_REPORT}), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.premarket.TraceLogger") as mock_tracer_cls, \
             patch("sessions.premarket.run_market_agent_v1", return_value=_V1_REPORT), \
             patch("sessions.premarket._log_market_eval"), \
             patch("scanner.scanner.run_scanner", return_value=5), \
             patch("sessions.premarket.send_alert"):
            mock_tracer = MagicMock()
            mock_tracer_cls.return_value = mock_tracer
            from sessions.premarket import main
            main()
        mock_exec.assert_called_once()

    def test_defers_to_open_before_market_open(self, mock_supabase, capsys):
        """Before 9:30 the premarket session scans and DEFERS the entry decision to the open:
        the research pipeline is not run (no live data) and no pending trades are stored —
        the intraday session makes the call after open with live data."""
        q = make_query([])
        mock_supabase.table.return_value = q
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket._MARKET_OPEN", time(23, 59)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_params", return_value=self._mock_params()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket.run_premarket_pipeline") as mock_pipeline, \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.premarket.TraceLogger") as mock_tracer_cls, \
             patch("scanner.scanner.run_scanner", return_value=5), \
             patch("sessions.premarket.send_alert") as mock_alert:
            mock_tracer = MagicMock()
            mock_tracer_cls.return_value = mock_tracer
            from sessions.premarket import main
            main()
        mock_pipeline.assert_not_called()                 # decision deferred — no research pre-open
        mock_exec.assert_not_called()
        mock_tracer.set_pending_trades.assert_not_called()
        assert mock_tracer.close_session.call_args.kwargs["terminal_reason"] == "deferred_to_open"
        mock_alert.assert_called_once()
        out = capsys.readouterr().out
        assert "deferring" in out.lower()

    def test_no_execute_when_no_trades(self, mock_supabase):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket._MARKET_OPEN", time(0, 0)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_params", return_value=self._mock_params()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket.run_premarket_pipeline",
                   return_value={**_RESULT_NO_TRADES, "_v2_market_report": _V2_REPORT}), \
             patch("sessions.premarket._execute_trades") as mock_exec, \
             patch("sessions.premarket.TraceLogger") as mock_tracer_cls, \
             patch("sessions.premarket.run_market_agent_v1", return_value=_V1_REPORT), \
             patch("sessions.premarket._log_market_eval"), \
             patch("scanner.scanner.run_scanner", return_value=5), \
             patch("sessions.premarket.send_alert"):
            mock_tracer = MagicMock()
            mock_tracer_cls.return_value = mock_tracer
            from sessions.premarket import main
            main()
        mock_exec.assert_not_called()

    def test_sends_alert_on_success(self, mock_supabase):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket._MARKET_OPEN", time(0, 0)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_params", return_value=self._mock_params()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket.run_premarket_pipeline",
                   return_value={**_RESULT_NO_TRADES, "_v2_market_report": _V2_REPORT}), \
             patch("sessions.premarket._execute_trades"), \
             patch("sessions.premarket.TraceLogger") as mock_tracer_cls, \
             patch("sessions.premarket.run_market_agent_v1", return_value=_V1_REPORT), \
             patch("sessions.premarket._log_market_eval"), \
             patch("scanner.scanner.run_scanner", return_value=5), \
             patch("sessions.premarket.send_alert") as mock_alert:
            mock_tracer = MagicMock()
            mock_tracer_cls.return_value = mock_tracer
            from sessions.premarket import main
            main()
        mock_alert.assert_called_once()

    def test_sends_error_alert_and_reraises_on_exception(self, mock_supabase):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket._MARKET_OPEN", time(0, 0)), \
             patch("sessions.premarket.check_protection_status",
                   return_value=self._mock_protection()), \
             patch("sessions.premarket.load_params", return_value=self._mock_params()), \
             patch("sessions.premarket.load_agent_config", return_value=self._mock_config()), \
             patch("sessions.premarket.run_premarket_pipeline",
                   side_effect=RuntimeError("boom")), \
             patch("sessions.premarket.TraceLogger") as mock_tracer_cls, \
             patch("scanner.scanner.run_scanner", return_value=5), \
             patch("sessions.premarket.send_alert") as mock_alert:
            mock_tracer = MagicMock()
            mock_tracer_cls.return_value = mock_tracer
            from sessions.premarket import main
            with pytest.raises(RuntimeError, match="boom"):
                main()
        assert mock_alert.called
        assert "Error" in mock_alert.call_args[0][0]
