"""Tests for evals/outcomes.py."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture()
def env_tenant(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "test-tenant-123")


def _make_client(evals_scores: list[float] | None = None, insert_ok: bool = True):
    """Build a mock Supabase client chain for ag_evals select + ag_outcomes insert."""
    client = MagicMock()

    # ag_evals query chain
    evals_chain = (
        client.table.return_value
        .select.return_value
        .eq.return_value
        .eq.return_value
        .eq.return_value
        .execute.return_value
    )
    evals_chain.data = [{"score": s} for s in (evals_scores or [])]

    # ag_outcomes insert chain
    insert_result = MagicMock()
    if not insert_ok:
        client.table.return_value.insert.side_effect = RuntimeError("DB insert failed")
    else:
        client.table.return_value.insert.return_value.execute.return_value = insert_result

    return client


# ── happy path ───────────────────────────────────────────────────────────────

def test_writes_pnl_and_risk_rows(env_tenant):
    client = _make_client(evals_scores=[0.80, 0.90])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 125.50, 0.6, 5)

    inserted = client.table.return_value.insert.call_args[0][0]
    assert len(inserted) == 6
    names = {r["metric_name"] for r in inserted}
    assert names == {
        "realized_pnl", "win_rate", "trades_total",
        "max_drawdown_pct", "within_limits", "max_single_trade_loss_pct",
    }


def test_risk_metrics_from_trades(env_tenant):
    client = _make_client(evals_scores=[])
    trades = [
        {"realized_pnl":  300.0, "position_size": 5000.0, "close_time": "2026-06-24T14:00:00"},
        {"realized_pnl": -800.0, "position_size": 5000.0, "close_time": "2026-06-24T15:00:00"},
    ]  # deployed 10000; cum 300 -> -500, dd 800; worst loss 800 on its own 5000 = 16%
    with patch("core.db.get_client", return_value=client), \
         patch("evals.outcomes._max_positions", return_value=10):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("s", 0.0, 0.5, 2, trades=trades)

    rows = {r["metric_name"]: r["metric_value"] for r in client.table.return_value.insert.call_args[0][0]}
    assert rows["max_drawdown_pct"] == 8.0            # dd 800 / deployed 10000
    assert rows["max_single_trade_loss_pct"] == 16.0  # 800 / that trade's own 5000
    assert rows["within_limits"] == 1.0               # 2 trades <= 10


def test_metric_values_correct(env_tenant):
    client = _make_client(evals_scores=[])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", -50.25, 0.333, 3)

    rows = {r["metric_name"]: r for r in client.table.return_value.insert.call_args[0][0]}
    assert rows["realized_pnl"]["metric_value"] == -50.25
    assert rows["realized_pnl"]["metric_unit"] == "usd"
    assert abs(rows["win_rate"]["metric_value"] - 0.333) < 1e-9
    assert rows["win_rate"]["metric_unit"] == "ratio"
    assert rows["trades_total"]["metric_value"] == 3.0
    assert rows["trades_total"]["metric_unit"] == "count"


def test_quality_score_avg(env_tenant):
    client = _make_client(evals_scores=[0.80, 0.90, 1.00])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)

    rows = client.table.return_value.insert.call_args[0][0]
    expected = round((0.80 + 0.90 + 1.00) / 3, 4)
    assert all(abs(r["quality_score"] - expected) < 1e-9 for r in rows)


def test_quality_score_none_when_no_evals(env_tenant):
    client = _make_client(evals_scores=[])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)

    rows = client.table.return_value.insert.call_args[0][0]
    assert all(r["quality_score"] is None for r in rows)


def test_tenant_id_written_to_all_rows(env_tenant):
    client = _make_client()
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-abc", 10.0, 0.5, 2)

    rows = client.table.return_value.insert.call_args[0][0]
    assert all(r["tenant_id"] == "test-tenant-123" for r in rows)
    assert all(r["session_id"] == "sess-abc" for r in rows)


def test_period_date_is_today(env_tenant):
    from datetime import date
    client = _make_client()
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)

    rows = client.table.return_value.insert.call_args[0][0]
    assert all(r["period_date"] == date.today().isoformat() for r in rows)


# ── skip / error paths ────────────────────────────────────────────────────────

def test_skips_when_no_tenant_id(monkeypatch):
    monkeypatch.delenv("TENANT_ID", raising=False)
    client = MagicMock()
    # patch load_dotenv so .env file doesn't repopulate TENANT_ID
    with patch("core.db.get_client", return_value=client), \
         patch("dotenv.load_dotenv"):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)

    client.table.assert_not_called()


def test_handles_insert_error_gracefully(env_tenant, capsys):
    client = MagicMock()
    # evals select returns empty list
    (
        client.table.return_value
        .select.return_value
        .eq.return_value
        .eq.return_value
        .eq.return_value
        .execute.return_value
    ).data = []
    # insert raises
    client.table.return_value.insert.return_value.execute.side_effect = RuntimeError("boom")
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)  # must not raise

    out = capsys.readouterr().out
    assert "Failed" in out


class TestComputeRiskMetrics:
    """Pure derivation of the contract's drawdown / limits / single-trade-loss signals."""

    def _m(self, **kw):
        from evals.outcomes import compute_risk_metrics
        return compute_risk_metrics(**kw)

    def test_no_trades_is_clean(self):
        m = self._m(trades=[], trades_total=0, max_positions=10)
        assert m == {"max_drawdown_pct": 0.0, "within_limits": 1.0, "max_single_trade_loss_pct": 0.0}

    def test_drawdown_vs_deployed_capital(self):
        trades = [
            {"realized_pnl":  300.0, "position_size": 5000.0, "close_time": "t1"},
            {"realized_pnl": -800.0, "position_size": 5000.0, "close_time": "t2"},  # cum 300 -> -500, dd 800
        ]
        m = self._m(trades=trades, trades_total=2, max_positions=10)
        assert m["max_drawdown_pct"] == 8.0   # 800 / deployed 10000 * 100

    def test_single_trade_loss_is_per_trade_not_pool(self):
        # A $60 loss on a $3k trade is 2% of THAT trade, regardless of the other trade or any pool.
        trades = [
            {"realized_pnl": -60.0,  "position_size": 3000.0, "close_time": "t1"},
            {"realized_pnl": 100.0,  "position_size": 3000.0, "close_time": "t2"},
        ]
        m = self._m(trades=trades, trades_total=2, max_positions=10)
        assert m["max_single_trade_loss_pct"] == 2.0

    def test_all_winners_zero_drawdown(self):
        trades = [{"realized_pnl": 100.0, "position_size": 3000.0, "close_time": "t1"},
                  {"realized_pnl": 40.0,  "position_size": 3000.0, "close_time": "t2"}]
        m = self._m(trades=trades, trades_total=2, max_positions=10)
        assert m["max_drawdown_pct"] == 0.0 and m["max_single_trade_loss_pct"] == 0.0

    def test_within_limits_breach(self):
        m = self._m(trades=[], trades_total=12, max_positions=10)
        assert m["within_limits"] == 0.0

    def test_zero_deployed_capital_safe(self):
        # No position sizes -> no divide by zero, metrics read clean.
        m = self._m(trades=[{"realized_pnl": -100.0, "position_size": 0.0, "close_time": "t1"}],
                    trades_total=1, max_positions=10)
        assert m["max_drawdown_pct"] == 0.0 and m["max_single_trade_loss_pct"] == 0.0

    def test_ignores_open_trades_for_drawdown(self):
        # An unclosed trade (no close_time, no realized P&L) contributes no drawdown.
        trades = [{"realized_pnl": None, "position_size": 5000.0, "close_time": None},
                  {"realized_pnl": 100.0, "position_size": 5000.0, "close_time": "t1"}]
        m = self._m(trades=trades, trades_total=2, max_positions=10)
        assert m["max_drawdown_pct"] == 0.0


class TestPushTradeOutcomes:
    """Each closed trade's realized P&L is pushed to the Argus Outcome Ledger so the
    trace-based prediction for that ticker reconciles against the real result."""

    def _run(self, trades, accepted=True, session_id=None):
        """accepted mirrors what the transport reports back. A push that was attempted but
        not accepted must not be counted, so the stub returns that verdict explicitly."""
        posted = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return accepted

        with patch("trace.logger._ingest_post", side_effect=fake_post):
            from evals.outcomes import push_trade_outcomes
            n = push_trade_outcomes(trades, session_id=session_id)
        return n, posted

    def test_posts_one_outcome_per_filled_trade(self):
        n, posted = self._run([
            {"ticker": "CAT", "realized_pnl": 214.0, "exit_reason": "NATIVE_TRAIL", "close_time": "2026-06-24T20:00:00"},
            {"ticker": "GE",  "realized_pnl": -50.0, "exit_reason": "eod_forced",   "close_time": "2026-06-24T20:00:00"},
        ])
        assert n == 2
        assert [p for p, _ in posted] == ["/api/ingest/outcome", "/api/ingest/outcome"]
        cat = next(pl for _, pl in posted if pl["entity_id"] == "CAT")
        assert cat["value"] == 214.0
        assert cat["source"] == "confirmed"
        assert cat["occurred_at"] == "2026-06-24T20:00:00"

    def test_skips_unfilled_orders(self):
        n, posted = self._run([
            {"ticker": "X", "realized_pnl": 0.0, "exit_reason": "unfilled", "close_time": None},
        ])
        assert n == 0
        assert posted == []

    def test_skips_rows_missing_ticker_or_pnl(self):
        n, posted = self._run([
            {"ticker": None, "realized_pnl": 10.0, "exit_reason": "eod_forced"},
            {"ticker": "Y",  "realized_pnl": None, "exit_reason": "eod_forced"},
        ])
        assert n == 0

    def test_counts_deliveries_not_attempts(self):
        """A push the server never accepted is not an outcome the fleet reported.

        This is the defect that let the ledger fill with unanswered predictions: the count
        came from the loop, not from the transport, so a day where nothing landed logged
        identically to a day where everything did.
        """
        trades = [
            {"ticker": "CAT", "realized_pnl": 214.0, "exit_reason": "NATIVE_TRAIL", "close_time": "t"},
            {"ticker": "GE",  "realized_pnl": -50.0, "exit_reason": "eod_forced",   "close_time": "t"},
        ]
        n, posted = self._run(trades, accepted=False)
        assert len(posted) == 2, "both were attempted"
        assert n == 0, "neither was accepted, so neither counts"

    def test_pins_the_outcome_to_its_own_session(self):
        """Without session_id the server settles the most recent unanswered row for the
        ticker, which on a fleet that sees the same ticker repeatedly can answer the wrong
        day's prediction."""
        n, posted = self._run(
            [{"ticker": "CAT", "realized_pnl": 1.0, "exit_reason": "eod_forced", "close_time": "t"}],
            session_id="sess-today",
        )
        assert n == 1
        assert posted[0][1]["session_id"] == "sess-today"

    def test_omits_session_id_when_not_given(self):
        n, posted = self._run(
            [{"ticker": "CAT", "realized_pnl": 1.0, "exit_reason": "eod_forced", "close_time": "t"}],
        )
        assert n == 1
        assert "session_id" not in posted[0][1]


class TestTriggerServerJudge:
    """The pipeline triggers the canonical server judge after a session closes; it scores
    per-ticker quality and writes the Outcome Ledger predictions."""

    def test_posts_to_compute_judge(self):
        from evals.outcomes import trigger_server_judge
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["timeout"] = timeout
            return MagicMock()
        with patch.dict("os.environ", {"PROVY_EMIT": "1"}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            trigger_server_judge("sess-abc")
        assert captured["url"].endswith("/api/compute/judge")
        assert b"sess-abc" in captured["body"]
        assert captured["timeout"] == 120

    def test_failure_is_non_fatal(self):
        from evals.outcomes import trigger_server_judge
        with patch.dict("os.environ", {"PROVY_EMIT": "1"}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("down")):
            trigger_server_judge("sess-x")  # must not raise

    def test_noop_without_argus_url(self):
        from evals.outcomes import trigger_server_judge
        with patch("trace.logger._ARGUS_URL", ""), \
             patch("urllib.request.urlopen") as uo:
            trigger_server_judge("sess-y")
            uo.assert_not_called()

    def test_backfill_posts_without_session_id(self):
        # The EOD safety net judges recent sessions with a no-session-id call.
        from evals.outcomes import backfill_server_judge
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["timeout"] = timeout
            return MagicMock()
        with patch.dict("os.environ", {"PROVY_EMIT": "1"}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            backfill_server_judge()
        assert captured["url"].endswith("/api/compute/judge")
        assert captured["body"] == b"{}"          # no session_id -> judges recent closed sessions
        assert captured["timeout"] == 120

    def test_backfill_failure_is_non_fatal(self):
        from evals.outcomes import backfill_server_judge
        with patch.dict("os.environ", {"PROVY_EMIT": "1"}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("down")):
            backfill_server_judge()  # must not raise

    def test_backfill_noop_without_argus_url(self):
        from evals.outcomes import backfill_server_judge
        with patch("trace.logger._ARGUS_URL", ""), \
             patch("urllib.request.urlopen") as uo:
            backfill_server_judge()
            uo.assert_not_called()

    def test_trigger_noop_when_emit_disabled(self):
        # Credentials present, but no opt-in (PROVY_EMIT unset, not in GitHub Actions):
        # the server judge must NOT hit the network. Guards the local EOD hang.
        from evals.outcomes import trigger_server_judge
        with patch.dict("os.environ", {"PROVY_EMIT": "", "GITHUB_ACTIONS": ""}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen") as uo:
            trigger_server_judge("sess-z")
            uo.assert_not_called()

    def test_backfill_noop_when_emit_disabled(self):
        from evals.outcomes import backfill_server_judge
        with patch.dict("os.environ", {"PROVY_EMIT": "", "GITHUB_ACTIONS": ""}), \
             patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen") as uo:
            backfill_server_judge()
            uo.assert_not_called()


class TestPushOutcomeSignals:
    """The session's settled risk signals go to Provy over the API.

    Before this existed, the only thing reaching Provy was a per-ticker P&L number, so the two
    conditions reading realized_pnl graded from the agents' OWN trace payloads and the three risk
    conditions graded from nothing. Measured against production on 2026-07-29: four of the six
    contract conditions had never been measured once.
    """

    def _run(self, *, realized_pnl=0.0, trades_total=0, trades=None, accepted=True):
        posted = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return accepted

        with patch("trace.logger._ingest_post", side_effect=fake_post):
            from evals.outcomes import push_outcome_signals
            ok = push_outcome_signals(
                "sess-1", realized_pnl, trades_total, trades=trades or [],
            )
        return ok, posted

    def test_posts_to_the_session_scoped_route_not_the_ledger(self):
        # The ledger's grain is the work item; these signals are per session. Sending them through
        # /api/ingest/outcome would need a synthetic entity_id, and Provy HOLDS an outcome for a work
        # item it never predicted, so every session would leave a permanent unreconcilable row.
        ok, posted = self._run()
        assert ok is True
        assert [p for p, _ in posted] == ["/api/ingest/outcome/signals"]
        assert posted[0][1]["session_id"] == "sess-1"

    def test_carries_every_signal_the_contract_grades_on(self):
        _, posted = self._run(
            realized_pnl=-120.0,
            trades_total=2,
            trades=[
                {"ticker": "CAT", "realized_pnl": 30.0,   "position_size": 3000.0, "close_time": "2026-07-29T13:00:00"},
                {"ticker": "GE",  "realized_pnl": -150.0, "position_size": 3000.0, "close_time": "2026-07-29T14:00:00"},
            ],
        )
        signals = posted[0][1]["signals"]
        assert set(signals) == {
            "realized_pnl", "max_drawdown_pct", "within_limits", "max_single_trade_loss_pct",
        }
        assert signals["realized_pnl"] == -120.0
        # Peak cumulative P&L was +30, trough -120, so the fall is 150 on 6000 deployed = 2.5%.
        assert signals["max_drawdown_pct"] == pytest.approx(2.5)
        # Worst single loss is 150 on its own 3000 position = 5%.
        assert signals["max_single_trade_loss_pct"] == pytest.approx(5.0)

    def test_within_limits_is_a_real_boolean(self):
        # compute_risk_metrics returns 1.0/0.0. Provy records a signal's declared type at intake and
        # a 0/1 NUMBER is not a flag there, so it must be sent as a bool or the condition grades as a
        # number comparison against a threshold nobody set.
        _, posted = self._run(trades_total=1)
        assert posted[0][1]["signals"]["within_limits"] is True

    def test_zero_trade_session_still_reports(self):
        # "No drawdown, within limits" is a real result, not an absence of one. A day that reported
        # nothing would leave the contract ungraded and look identical to a delivery failure.
        ok, posted = self._run(realized_pnl=0.0, trades_total=0, trades=[])
        assert ok is True
        assert posted[0][1]["signals"]["max_drawdown_pct"] == 0.0
        assert posted[0][1]["signals"]["within_limits"] is True

    def test_reports_a_rejected_push_instead_of_claiming_success(self):
        # A dropped outcome that logs like a success is exactly what hid this gap for weeks.
        ok, posted = self._run(accepted=False)
        assert ok is False
        assert len(posted) == 1


# ── argus#578: per-ticker P&L as a tagged DIAGNOSTIC ─────────────────────────

def test_emits_per_ticker_pnl_tagged_with_the_ticker(env_tenant):
    """The per-ticker settled number the per-ticker CLAIM (argus#602) reconciles against."""
    client = _make_client(evals_scores=[])
    trades = [
        {"ticker": "RTX", "realized_pnl": 120.0, "position_size": 3000.0, "close_time": "2026-08-17T14:00:00"},
        {"ticker": "NOC", "realized_pnl": -45.0, "position_size": 3000.0, "close_time": "2026-08-17T15:00:00"},
    ]
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 75.0, 0.5, 2, trades=trades)

    inserted = client.table.return_value.insert.call_args[0][0]
    per_ticker = [r for r in inserted if r["metric_name"] == "position_realized_pnl"]
    assert {r["entity_id"] for r in per_ticker} == {"RTX", "NOC"}
    assert {r["entity_id"]: r["metric_value"] for r in per_ticker} == {"RTX": 120.0, "NOC": -45.0}


def test_portfolio_metrics_stay_UNTAGGED(env_tenant):
    """⛔ THE WHOLE POINT OF THE SPLIT. 'End-of-day net profit is positive' is a PORTFOLIO question
    (Amit's call, 14 Aug). Tagging realized_pnl per ticker would silently turn it into 'every
    position was profitable', a harsher contract nobody wrote."""
    client = _make_client(evals_scores=[])
    trades = [{"ticker": "RTX", "realized_pnl": 120.0, "position_size": 3000.0, "close_time": "2026-08-17T14:00:00"}]
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 120.0, 1.0, 1, trades=trades)

    inserted = client.table.return_value.insert.call_args[0][0]
    portfolio = [r for r in inserted if r["metric_name"] != "position_realized_pnl"]
    assert portfolio, "the session-level metrics must still be written"
    for r in portfolio:
        assert r.get("entity_id") is None, f"{r['metric_name']} must stay a portfolio reading"


def test_a_trade_that_never_filled_reports_no_outcome(env_tenant):
    """Same exclusion the ledger push applies. An unfilled order settled nothing, so reporting a P&L
    for it would invent an outcome and put the diagnostic out of step with the ledger."""
    client = _make_client(evals_scores=[])
    trades = [
        {"ticker": "RTX", "realized_pnl": 0.0, "position_size": 0.0, "exit_reason": "unfilled",
         "close_time": "2026-08-17T14:00:00"},
        {"ticker": "NOC", "realized_pnl": 10.0, "position_size": 3000.0, "close_time": "2026-08-17T15:00:00"},
    ]
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 10.0, 1.0, 1, trades=trades)

    inserted = client.table.return_value.insert.call_args[0][0]
    per_ticker = [r for r in inserted if r["metric_name"] == "position_realized_pnl"]
    assert {r["entity_id"] for r in per_ticker} == {"NOC"}


def test_no_trades_means_no_per_ticker_rows(env_tenant):
    client = _make_client(evals_scores=[])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 0.0, 0.0, 0)

    inserted = client.table.return_value.insert.call_args[0][0]
    assert [r for r in inserted if r["metric_name"] == "position_realized_pnl"] == []
    assert len(inserted) == 6      # the portfolio set, unchanged
