"""
argus#675: the funnel checks must be written from EVERY intraday exit, not the happy path.

⛔ THE SHIPPED DEFECT, VERBATIM. premarket called write_funnel_evals on its main path only. On
27 Jul 2026 a redesign added an early return above that call and research_yield /
risk_approval_rate stopped grading for four weeks. No error, no gap, no empty state.

⛔ AND THE SKIPPABLE-LOOKING PATHS ARE THE ONES THAT MATTER. `no_intraday_candidates` IS a
research_yield of zero and `intraday_all_rejected` IS a risk_approval_rate of zero, so skipping
those two early returns lost the FAILING half of the data specifically.
"""
from unittest.mock import MagicMock, patch

import pytest

from sessions.intraday import _close_intraday


def written(terminal, proposals=None, verdicts=None, executed=0):
    tracer = MagicMock()
    with patch("sessions.intraday.write_funnel_evals") as w:
        _close_intraday(tracer, "sess-1", terminal, proposals=proposals,
                        verdicts=verdicts, trades_executed=executed)
    return tracer, w


P2 = {"proposals": [{"ticker": "AAA"}, {"ticker": "BBB"}]}
V1 = {"verdicts": [{"ticker": "AAA", "verdict": "APPROVED"}, {"ticker": "BBB", "verdict": "REJECTED"}]}
V0 = {"verdicts": [{"ticker": "AAA", "verdict": "REJECTED"}, {"ticker": "BBB", "verdict": "REJECTED"}]}


class TestEveryExitRecordsTheFunnel:
    def test_the_main_path_records_it(self):
        _, w = written("intraday_entries_placed", P2, V1, executed=1)
        assert w.called
        assert w.call_args.kwargs["trades_proposed"] == 2
        assert w.call_args.kwargs["trades_approved"] == 1

    # ⛔ THIS IS research_yield = 0. The early return that skipped it was skipping the failure.
    def test_no_candidates_records_a_research_yield_of_zero(self):
        _, w = written("no_intraday_candidates", {"proposals": []})
        assert w.called
        assert w.call_args.kwargs["trades_proposed"] == 0

    # ⛔ AND THIS IS risk_approval_rate = 0.
    def test_all_rejected_records_an_approval_rate_of_zero(self):
        _, w = written("intraday_all_rejected", P2, V0)
        assert w.called
        assert w.call_args.kwargs["trades_proposed"] == 2
        assert w.call_args.kwargs["trades_approved"] == 0

    def test_the_session_is_still_closed_on_every_path(self):
        for terminal, p, v in [("intraday_entries_placed", P2, V1),
                               ("no_intraday_candidates", {"proposals": []}, None),
                               ("intraday_all_rejected", P2, V0)]:
            tracer, _ = written(terminal, p, v)
            assert tracer.close_session.called
            assert tracer.close_session.call_args.args[0] == terminal


class TestACheckThatCannotRunWritesNoRow:
    # ⛔ research never returned, so there is no funnel. A fabricated research_yield=0 here would
    # blame research for an orchestrator crash.
    def test_an_error_before_research_writes_nothing(self):
        _, w = written("error", proposals=None)
        assert not w.called

    def test_an_error_after_research_still_records_what_ran(self):
        _, w = written("error", proposals=P2, verdicts=V1)
        assert w.called
        assert w.call_args.kwargs["trades_proposed"] == 2

    # ⛔ THE SESSION MUST STILL CLOSE even when no funnel is recorded, or an error leaves the
    # session open forever and the "stopped grading" problem becomes a "never closed" problem.
    def test_the_session_closes_even_when_nothing_is_written(self):
        tracer, w = written("error", proposals=None)
        assert not w.called
        assert tracer.close_session.called


class TestTelemetryNeverBreaksTheSession:
    # ⛔ A FAILED EVAL WRITE MUST NOT TAKE THE TRADING SESSION WITH IT. This agent places real
    # orders; a Provy outage is not a reason to raise through the close path.
    def test_a_raising_writer_is_swallowed_after_the_close(self):
        tracer = MagicMock()
        with patch("sessions.intraday.write_funnel_evals", side_effect=RuntimeError("provy down")):
            _close_intraday(tracer, "sess-1", "intraday_entries_placed", proposals=P2, verdicts=V1)
        assert tracer.close_session.called          # the close happened first, and survived


class TestCountsAreReadFromWhatActuallyRan:
    def test_counts_come_from_the_payloads_not_the_caller(self):
        # ⛔ NOT PASSED IN BY THE CALLER. Every count is derived from the proposals and verdicts the
        # session actually produced, so a caller cannot report a funnel that did not happen.
        _, w = written("intraday_entries_placed", P2, V1, executed=99)
        assert w.call_args.kwargs["trades_proposed"] == 2
        assert w.call_args.kwargs["trades_approved"] == 1

    def test_missing_or_empty_payloads_degrade_to_zero_not_a_crash(self):
        for p, v in [({}, {}), ({"proposals": None}, {"verdicts": None}), (P2, {})]:
            _, w = written("intraday_all_rejected", p, v)
            assert w.called
