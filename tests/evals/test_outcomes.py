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

def test_writes_three_rows(env_tenant):
    client = _make_client(evals_scores=[0.80, 0.90])
    with patch("core.db.get_client", return_value=client):
        from evals.outcomes import write_eod_outcome_metrics
        write_eod_outcome_metrics("sess-1", 125.50, 0.6, 5)

    inserted = client.table.return_value.insert.call_args[0][0]
    assert len(inserted) == 3
    names = {r["metric_name"] for r in inserted}
    assert names == {"realized_pnl", "win_rate", "trades_total"}


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
