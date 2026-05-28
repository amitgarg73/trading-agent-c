from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from sessions.premarket import (
    _build_premarket_alert,
    _execute_trades,
    _run_news_analyst,
)
from tests.conftest import make_query

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

    def test_exits_on_non_trading_day(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=False):
            from sessions.premarket import main
            main()
        out = capsys.readouterr().out
        assert "Not a trading day" in out

    def test_exits_when_protection_suspended(self, mock_supabase, capsys):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
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

    def test_no_execute_when_no_trades(self, mock_supabase):
        with patch("sessions.premarket.is_trading_day", return_value=True), \
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
