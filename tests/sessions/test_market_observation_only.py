"""The restored macro read must be observation-only, and the scan must report itself.

Both agents went silent on 27 Jul and nothing noticed for nine days, because Provy showed them as
ordinary rows with a stale grade. These tests pin the two properties that matter:

  1. the market agent runs again and its verdict is RECORDED
  2. it does NOT change what the session does, including when it says SKIP
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import scanner.scanner as scanner_mod
import sessions.premarket as premarket


class TestMarketReadIsObservationOnly:
    def test_the_verdict_is_recorded(self):
        src = inspect.getsource(premarket)
        assert 'tracer.log_decision("market", "observation_only"' in src
        assert '"would_have_blocked_trading": decision == "SKIP"' in src
        assert '"acted_on": False' in src

    def test_NOTHING_BRANCHES_ON_THE_VERDICT(self):
        """⛔ The whole point. If a future edit reads market_report to decide anything, this fails.

        The gate stood the system down on 4 days between 17 Jun and 6 Jul, so restoring it live is a
        real trading change and a separate decision. This restores the SIGNAL only.
        """
        src = inspect.getsource(premarket)
        start = src.index('market_report = run_market_agent(tracer, params)')
        block = src[start:start + 1600]
        for forbidden in ('if decision ==', 'if market_report', 'return' ,'params.max_positions ='):
            assert forbidden not in block.split('print(f"[premarket] Pre-open')[0], \
                f"observation-only block must not act on the verdict, found: {forbidden}"

    def test_a_macro_failure_can_never_stop_the_session(self):
        src = inspect.getsource(premarket)
        start = src.index('market_report = run_market_agent(tracer, params)')
        assert 'except Exception as e:' in src[start:start + 1600]
        assert 'observation-only read failed' in src[start:start + 1600]


class TestScannerReportsItself:
    def test_tracer_is_optional_so_backtests_are_unaffected(self):
        sig = inspect.signature(scanner_mod.run_scanner)
        assert 'tracer' in sig.parameters
        assert sig.parameters['tracer'].default is None

    def test_premarket_passes_one(self):
        assert 'run_scanner(scan_date=date.today(), tracer=tracer)' in inspect.getsource(premarket)

    def test_it_opens_a_scanner_span_and_reports_the_count(self):
        src = inspect.getsource(scanner_mod.run_scanner)
        assert 'tracer.start_agent_span("scanner")' in src
        assert 'candidates_scored' in src

    def test_EVERY_EXIT_REPORTS(self):
        """A scan that dies silently looks exactly like a scan that never ran. That is the bug."""
        src = inspect.getsource(scanner_mod.run_scanner)
        assert 'already_scored' in src and 'log_decision' in src
        assert src.count('tracer.log_error("scanner"') == 2, 'both download failure paths must report'

    def test_no_tracer_means_no_calls(self):
        """Guards the default path: a None tracer must not raise."""
        src = inspect.getsource(scanner_mod.run_scanner)
        for line in src.splitlines():
            if 'tracer.' in line and 'if tracer' not in line:
                assert line.strip().startswith(('tracer.',)), line
        # every tracer.* use sits under an `if tracer:` guard
        assert src.count('if tracer:') >= 4


def test_the_scan_still_returns_a_count_without_a_tracer(monkeypatch):
    """The signature change must be invisible to existing callers."""
    monkeypatch.setattr(scanner_mod, 'get_tickers', lambda: [])
    fake_db = MagicMock()
    fake_db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {'ticker': 'AAPL'}, {'ticker': 'MSFT'},
    ]
    monkeypatch.setattr('core.db.get_client', lambda: fake_db)
    assert scanner_mod.run_scanner() == 2
