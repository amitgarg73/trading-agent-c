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

    def _run(self, trades):
        posted = []
        with patch("trace.logger._ingest_post", side_effect=lambda path, payload: posted.append((path, payload))):
            from evals.outcomes import push_trade_outcomes
            n = push_trade_outcomes(trades)
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
        with patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            trigger_server_judge("sess-abc")
        assert captured["url"].endswith("/api/compute/judge")
        assert b"sess-abc" in captured["body"]
        assert captured["timeout"] == 120

    def test_failure_is_non_fatal(self):
        from evals.outcomes import trigger_server_judge
        with patch("trace.logger._ARGUS_URL", "https://argus.test"), \
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
        with patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            backfill_server_judge()
        assert captured["url"].endswith("/api/compute/judge")
        assert captured["body"] == b"{}"          # no session_id -> judges recent closed sessions
        assert captured["timeout"] == 120

    def test_backfill_failure_is_non_fatal(self):
        from evals.outcomes import backfill_server_judge
        with patch("trace.logger._ARGUS_URL", "https://argus.test"), \
             patch("trace.logger._ARGUS_API_KEY", "k"), \
             patch("urllib.request.urlopen", side_effect=RuntimeError("down")):
            backfill_server_judge()  # must not raise

    def test_backfill_noop_without_argus_url(self):
        from evals.outcomes import backfill_server_judge
        with patch("trace.logger._ARGUS_URL", ""), \
             patch("urllib.request.urlopen") as uo:
            backfill_server_judge()
            uo.assert_not_called()
