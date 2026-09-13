"""
argus#865, session side: stand down on a closed market, trace every approved pick, count honestly,
claim an expected value rather than the best case, and label dollars as dollars.
"""
from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest
import pytz

from core.market_calendar import MarketDay
from tests.conftest import make_query

_ET = pytz.timezone("America/New_York")
LABOR_DAY = date(2026, 9, 7)
_CLOSED = MarketDay(LABOR_DAY, False, "alpaca", "Labor Day")
_CLOSED_UNKNOWN = MarketDay(date(2029, 3, 6), False, "unknown", "calendar API failed; failing closed")


def _closed(day=_CLOSED):
    return patch("core.market_calendar.check_market_day", return_value=day)


# ── 1. Every session stands down on a closed market ────────────────────────────

class TestPremarketStandsDown:
    def _run(self, day=_CLOSED):
        protection = MagicMock(suspended=False)
        tracer = MagicMock()
        with patch("sessions.premarket.is_trading_day", return_value=True), \
             patch("sessions.premarket._PREMARKET_START", time(0, 0)), \
             patch("sessions.premarket._PREMARKET_END", time(23, 59)), \
             patch("sessions.premarket.check_protection_status", return_value=protection), \
             patch("sessions.premarket.load_agent_config", return_value={}), \
             patch("sessions.premarket._existing_session_guard", return_value=(False, "")), \
             patch("sessions.premarket.TraceLogger", return_value=tracer), \
             patch("sessions.premarket.run_premarket_pipeline") as pipeline, \
             patch("sessions.premarket.send_alert") as alert, \
             patch("scanner.scanner.run_scanner") as scanner, \
             _closed(day):
            from sessions.premarket import main
            main()
        return tracer, pipeline, scanner, alert

    def test_labor_day_runs_nothing(self):
        _, pipeline, scanner, _ = self._run()
        scanner.assert_not_called()
        pipeline.assert_not_called()

    def test_the_day_is_traced_as_market_closed(self):
        tracer, _, _, alert = self._run()
        assert tracer.close_session.call_args.kwargs["terminal_reason"] == "market_closed"
        assert "Labor Day" in tracer.close_session.call_args.kwargs["result_summary"]
        skips = tracer.log_skip.call_args_list
        assert skips and all(c.kwargs["reason"] == "market_closed" for c in skips)
        assert all(c.kwargs["skip_type"] == "design" for c in skips)
        tracer.log_decision.assert_called_with("orchestrator", "market_closed",
                                               detail=_CLOSED.as_detail())
        alert.assert_not_called()

    def test_a_fail_closed_day_is_typed_as_an_error_skip_and_alerts(self):
        tracer, _, _, alert = self._run(_CLOSED_UNKNOWN)
        assert all(c.kwargs["skip_type"] == "error" for c in tracer.log_skip.call_args_list)
        alert.assert_called_once()

    def test_a_second_premarket_run_on_the_closed_day_is_deduplicated(self):
        from sessions.premarket import _existing_session_guard
        with patch("core.run_state.today_premarket_run",
                   return_value={"id": "abcdef12-0000", "terminal_reason": "market_closed",
                                 "status": "in_progress"}):
            skip, _ = _existing_session_guard("2026-09-07")
        assert skip is True


class TestIntradayStandsDown:
    def test_no_research_no_orders_and_no_premarket_fallback(self, capsys):
        fake_now = _ET.localize(datetime(2026, 9, 7, 10, 30))
        with patch("sessions.intraday.is_trading_day", return_value=True), \
             patch("sessions.intraday.datetime") as mock_dt, \
             patch("sessions.intraday.get_premarket_session_id") as get_pm, \
             patch("sessions.premarket.main") as premarket_main, \
             patch("agents.research_agent.run_research_agent") as research, \
             patch("sessions.intraday._place_intraday_trades") as place, \
             _closed():
            mock_dt.now.return_value = fake_now
            from sessions.intraday import main
            main()
        assert "Market closed 2026-09-07" in capsys.readouterr().out
        get_pm.assert_not_called()
        premarket_main.assert_not_called()
        research.assert_not_called()
        place.assert_not_called()


class TestPositionWatchdogStandsDown:
    def test_no_sync_no_deferred_entries_but_the_heartbeat_still_records(self):
        def mock_now(_tz):
            dt = MagicMock()
            dt.strftime.return_value = "MON"
            dt.time.return_value = time(10, 0)
            return dt

        with patch("sessions.position_watchdog.datetime") as mock_dt, \
             patch("sessions.position_watchdog.is_trading_day", return_value=True), \
             patch("sessions.position_watchdog.check_protection_status") as protection, \
             patch("sessions.position_watchdog._sync_positions") as sync, \
             patch("sessions.position_watchdog._execute_pending_trades") as pending, \
             patch("core.run_state.record_heartbeat") as heartbeat, \
             _closed():
            mock_dt.now.side_effect = mock_now
            from sessions.position_watchdog import main
            main()
        protection.assert_not_called()
        sync.assert_not_called()
        pending.assert_not_called()
        heartbeat.assert_called_once_with("position_watchdog", "ok", None)


class TestEodStandsDown:
    def test_nothing_is_closed_or_scored(self, capsys):
        with patch("sessions.eod.is_trading_day", return_value=True), \
             patch("sessions.eod.get_today_session_id") as get_sid, \
             patch("sessions.eod.force_close_positions") as force_close, \
             patch("sessions.eod.TraceLogger") as tracer_cls, \
             _closed():
            from sessions.eod import main
            main()
        assert "Market closed" in capsys.readouterr().out
        get_sid.assert_not_called()
        force_close.assert_not_called()
        tracer_cls.assert_not_called()


class TestSessionWatchdogIsSilentOnAHoliday:
    def test_no_missing_work_alarm(self):
        from sessions.watchdog import check_expected_work
        at = _ET.localize(datetime(2026, 9, 7, 17, 0))
        with patch("core.agent_config.is_trading_day", return_value=True), \
             patch("core.run_state.today_run", return_value=None), \
             patch("core.run_state.performance_recorded", return_value=False), \
             patch("core.run_state.heartbeat_age_minutes", return_value=None), \
             _closed():
            assert check_expected_work(at) == []


# ── 2. Every approved pick ends in a traced outcome ────────────────────────────

def _proposal(ticker, **kw):
    return {"ticker": ticker, "entry_price": 100.0, "target_price": 106.0, "stop_loss": 98.0,
            "position_size": 3000, "shares": 30, "confidence": "MEDIUM", **kw}


def _place(proposals, approved, *, held=frozenset(), today=None, submit=None):
    from sessions.intraday import _place_intraday_trades
    tracer, outcomes = MagicMock(), {}
    submit = submit or (lambda **kw: (f"ord-{kw['ticker']}", 100.0))
    with patch("core.db.get_client") as db, \
         patch("core.alpaca.get_open_alpaca_tickers", return_value=set(held)), \
         patch("core.alpaca.submit_bracket_order", side_effect=submit) as bracket, \
         patch("core.alpaca.submit_trailing_stop", return_value="trail-1"):
        db.return_value.table.return_value = make_query([])
        count = _place_intraday_trades({"proposals": proposals}, set(approved), "sess-pre", 0.01,
                                       today_tickers=set(today or ()), tracer=tracer, outcomes=outcomes)
    return count, outcomes, tracer, bracket


def _skips(tracer):
    return {c.kwargs["entity_id"]: c.kwargs["reason"] for c in tracer.log_skip.call_args_list}


class TestNoApprovedPickVanishes:
    def test_the_psx_shape_a_ticker_held_at_the_broker_is_traced_as_a_skip(self):
        # PSX on 8-11 Sep and VLO on 7 Sep: approved, never submitted, nothing recorded.
        count, outcomes, tracer, bracket = _place(
            [_proposal("PSX"), _proposal("WFC")], {"PSX", "WFC"}, held={"PSX"})
        assert count == 1
        assert outcomes == {"PSX": "skipped:already_held_at_broker", "WFC": "entered"}
        assert _skips(tracer) == {"PSX": "already_held_at_broker"}
        assert [c.kwargs["ticker"] for c in bracket.call_args_list] == ["WFC"]

    def test_already_entered_today_is_traced_as_a_skip(self):
        _, outcomes, tracer, _ = _place([_proposal("VLO")], {"VLO"}, today={"VLO"})
        assert outcomes == {"VLO": "skipped:already_entered_today"}
        assert _skips(tracer) == {"VLO": "already_entered_today"}

    def test_an_approved_ticker_with_no_proposal_is_traced_as_a_skip(self):
        _, outcomes, tracer, _ = _place([_proposal("WFC")], {"WFC", "GHOST"})
        assert outcomes["GHOST"] == "skipped:no_matching_proposal"
        assert _skips(tracer) == {"GHOST": "no_matching_proposal"}

    def test_a_broker_rejection_is_recorded_as_rejected(self):
        _, outcomes, tracer, _ = _place(
            [_proposal("MU")], {"MU"}, submit=lambda **kw: (None, None))
        assert outcomes == {"MU": "rejected"}
        assert tracer.log_tool_call.call_args.kwargs["outcome"] == "rejected"

    def test_every_approved_ticker_has_exactly_one_outcome(self):
        # The 7 Sep session shape: six approved, one held, one refused, four entered.
        props = [_proposal(t) for t in ("VLO", "WFC", "MU", "JNJ", "PRU", "SNOW")]

        def submit(**kw):
            return (None, None) if kw["ticker"] == "MU" else (f"ord-{kw['ticker']}", 100.0)

        count, outcomes, tracer, _ = _place(props, {p["ticker"] for p in props},
                                            held={"VLO"}, submit=submit)
        assert count == 4
        assert set(outcomes) == {"VLO", "WFC", "MU", "JNJ", "PRU", "SNOW"}
        order_traced = {c.kwargs["entity_id"] for c in tracer.log_tool_call.call_args_list}
        skip_traced = set(_skips(tracer))
        assert order_traced | skip_traced == set(outcomes)
        assert not order_traced & skip_traced

    def test_a_skip_trace_failure_never_breaks_the_scan(self):
        from sessions.intraday import _place_intraday_trades
        tracer = MagicMock()
        tracer.log_skip.side_effect = RuntimeError("exporter down")
        with patch("core.db.get_client") as db, \
             patch("core.alpaca.get_open_alpaca_tickers", return_value={"PSX"}), \
             patch("core.alpaca.submit_bracket_order", return_value=("ord-1", 100.0)), \
             patch("core.alpaca.submit_trailing_stop", return_value="t"):
            db.return_value.table.return_value = make_query([])
            count = _place_intraday_trades({"proposals": [_proposal("PSX"), _proposal("WFC")]},
                                           {"PSX", "WFC"}, "s", 0.01, tracer=tracer)
        assert count == 1


def test_log_skip_writes_entity_and_keeps_reason_authoritative(tracer):
    with patch.object(tracer, "_write", return_value="id") as write:
        tracer.log_skip("orchestrator", reason="already_held_at_broker", entity_id="PSX",
                        detail={"reason": "overwritten?", "gate": "g"})
    fields = write.call_args.args[0]
    assert fields["entity_id"] == "PSX"
    assert fields["payload"] == {"gate": "g", "reason": "already_held_at_broker", "skip_type": "design"}


# ── 3. Session metadata counts what happened ───────────────────────────────────

class TestSessionCounts:
    V = {"verdicts": [{"ticker": t, "verdict": "APPROVED"} for t in ("VLO", "WFC", "JNJ", "PRU", "SNOW", "MU")]
                     + [{"ticker": "XOM", "verdict": "REJECTED", "reason": "sector cap"}]}

    def test_trades_approved_and_rejections_reach_close_session(self):
        from sessions.intraday import _close_intraday
        tracer = MagicMock()
        with patch("sessions.intraday.write_funnel_evals"):
            _close_intraday(tracer, "s", "intraday_entries_placed",
                            proposals={"proposals": [{}] * 7}, verdicts=self.V, trades_executed=4)
        kw = tracer.close_session.call_args.kwargs
        assert (kw["trades_proposed"], kw["trades_approved"], kw["risk_rejections"],
                kw["trades_executed"]) == (7, 6, 1, 4)

    def test_the_summary_names_only_entered_tickers(self):
        from sessions.intraday import _entry_summary
        approved = [v for v in self.V["verdicts"] if v["verdict"] == "APPROVED"]
        outcomes = {"VLO": "skipped:already_held_at_broker", "WFC": "entered", "MU": "rejected",
                    "JNJ": "entered", "PRU": "entered", "SNOW": "entered"}
        s = _entry_summary(self.V, approved, outcomes, 4)
        assert s.startswith("4 entered: WFC, JNJ, PRU, SNOW.")
        assert "Risk approved 6, rejected 1." in s
        assert "VLO (already_held_at_broker)" in s and "MU (rejected)" in s
        head = s.split(".")[0]
        assert "VLO" not in head and "MU" not in head

    def test_zero_entered_says_zero(self):
        from sessions.intraday import _entry_summary
        s = _entry_summary({"verdicts": [{"ticker": "A", "verdict": "APPROVED"}]},
                           [{"ticker": "A"}], {"A": "rejected"}, 0)
        assert s == "0 entered. Risk approved 1, rejected 0. Approved but not entered: A (rejected)."


# ── 4. The claim is an expected value ─────────────────────────────────────────

class TestExpectedValueClaim:
    from sessions.intraday import _expected_value_claim as _ev
    _ev = staticmethod(_ev)

    def test_high_confidence_weights_target_and_stop(self):
        c = self._ev(_proposal("A", confidence="HIGH"), entry=100.0, shares=30, ticker="A")
        # 0.9 * 6 * 30  -  0.1 * 2 * 30  =  162 - 6
        assert c == {"signal": "realized_pnl", "value": 156.0, "entity_id": "A", "confidence": 0.9}

    def test_it_is_below_the_best_case(self):
        c = self._ev(_proposal("A", confidence="MEDIUM"), entry=100.0, shares=30, ticker="A")
        assert c["value"] < (106.0 - 100.0) * 30

    def test_the_claim_can_predict_a_loss(self):
        # ⛔ The point of the fix. LOW (0.3) at 1.5:1: 0.3 * 3 * 30 - 0.7 * 2 * 30 = 27 - 42.
        low = _proposal("A", confidence="LOW", target_price=103.0)
        c = self._ev(low, entry=100.0, shares=30, ticker="A")
        assert c["value"] == -15.0
        assert c["confidence"] == 0.7, "confidence is in the claimed sign, so 1 - p for a loss"

    def test_no_stated_confidence_means_no_claim(self):
        assert self._ev(_proposal("A", confidence=None), entry=100.0, shares=30, ticker="A") is None
        assert self._ev(_proposal("A", confidence="SURE"), entry=100.0, shares=30, ticker="A") is None

    def test_the_schema_provy_reads_is_unchanged(self):
        c = self._ev(_proposal("A"), entry=100.0, shares=30, ticker="A")
        assert set(c) == {"signal", "value", "entity_id", "confidence"}
        assert isinstance(c["value"], float) and 0 <= c["confidence"] <= 1

    def test_the_entry_message_carries_the_ev_claim(self):
        _, _, tracer, _ = _place([_proposal("WFC", confidence="LOW", target_price=103.0)], {"WFC"})
        msg = [c for c in tracer.log_agent_message.call_args_list if c.args[2] == "entered"][0]
        assert msg.kwargs["claim"]["value"] == -15.0
        assert msg.kwargs["payload"]["estimated_profit"] == 90.0        # the reading stays
        assert msg.kwargs["payload"]["expected_value"] == -15.0


# ── 6. Units ───────────────────────────────────────────────────────────────────

def test_position_size_is_labelled_as_dollars():
    from sessions.intraday import _entry_rationale
    say = _entry_rationale({"proposals": [_proposal("WFC", position_size=3000.0)]},
                           {"verdicts": [{"ticker": "WFC", "verdict": "APPROVED"}]},
                           [{"ticker": "WFC", "verdict": "APPROVED"}], 1, "intraday_entries_placed")
    assert "$3,000 position" in say
    assert "size 3000" not in say
