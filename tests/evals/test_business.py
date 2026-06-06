"""Tests for evals/business.py — L5 business outcome evals."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.fixture(autouse=True)
def set_tenant(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "test-tenant-id")
    # Reload module so it picks up the env var
    import importlib
    import evals.business as biz
    importlib.reload(biz)
    yield biz


def _call(set_tenant, session_id="sess-1", trades_proposed=3,
          trades_approved=2, terminal_reason="trades_approved"):
    """Helper: call write_premarket_outcome_evals with a mocked DB client."""
    mock_client = MagicMock()
    mock_insert = MagicMock()
    mock_client.table.return_value.insert.return_value.execute = mock_insert

    with patch("core.db.get_client", return_value=mock_client):
        set_tenant.write_premarket_outcome_evals(
            session_id=session_id,
            trades_proposed=trades_proposed,
            trades_approved=trades_approved,
            terminal_reason=terminal_reason,
        )

    calls = mock_client.table.return_value.insert.call_args_list
    if not calls:
        return []
    rows = calls[0][0][0]
    return rows


def test_writes_two_rows_when_pipeline_ran(set_tenant):
    rows = _call(set_tenant, trades_proposed=3, trades_approved=2)
    assert len(rows) == 2
    names = {r["eval_name"] for r in rows}
    assert names == {"research_yield", "risk_approval_rate"}


def test_all_rows_have_layer_5(set_tenant):
    rows = _call(set_tenant, trades_proposed=3, trades_approved=2)
    assert all(r["layer"] == 5 for r in rows)


def test_research_yield_passes_when_proposals_exist(set_tenant):
    rows = _call(set_tenant, trades_proposed=2, trades_approved=1)
    row = next(r for r in rows if r["eval_name"] == "research_yield")
    assert row["score"] == 1.0
    assert row["passed"] is True


def test_research_yield_fails_when_no_proposals(set_tenant):
    rows = _call(set_tenant, trades_proposed=0, trades_approved=0,
                 terminal_reason="no_viable_proposals")
    row = next(r for r in rows if r["eval_name"] == "research_yield")
    assert row["score"] == 0.0
    assert row["passed"] is False


def test_risk_approval_rate_computed_correctly(set_tenant):
    rows = _call(set_tenant, trades_proposed=4, trades_approved=1)
    row = next(r for r in rows if r["eval_name"] == "risk_approval_rate")
    assert row["score"] == pytest.approx(0.25)


def test_risk_approval_rate_passes_at_threshold(set_tenant):
    # 1 of 5 = 0.20 = exactly at threshold (>= 0.20 required)
    rows = _call(set_tenant, trades_proposed=5, trades_approved=1)
    row = next(r for r in rows if r["eval_name"] == "risk_approval_rate")
    assert row["passed"] is True


def test_risk_approval_rate_fails_below_threshold(set_tenant):
    # 0 approved → rate = 0.0
    rows = _call(set_tenant, trades_proposed=5, trades_approved=0)
    row = next(r for r in rows if r["eval_name"] == "risk_approval_rate")
    assert row["passed"] is False


def test_no_risk_row_when_no_proposals(set_tenant):
    rows = _call(set_tenant, trades_proposed=0, trades_approved=0,
                 terminal_reason="no_viable_proposals")
    names = {r["eval_name"] for r in rows}
    assert "risk_approval_rate" not in names


def test_skip_session_writes_nothing(set_tenant):
    mock_client = MagicMock()
    with patch("core.db.get_client", return_value=mock_client):
        set_tenant.write_premarket_outcome_evals(
            session_id="sess-skip",
            trades_proposed=0,
            trades_approved=0,
            terminal_reason="skip_propagated",
        )
    mock_client.table.assert_not_called()


def test_no_candidates_writes_nothing(set_tenant):
    mock_client = MagicMock()
    with patch("core.db.get_client", return_value=mock_client):
        set_tenant.write_premarket_outcome_evals(
            session_id="sess-nc",
            trades_proposed=0,
            trades_approved=0,
            terminal_reason="no_candidates",
        )
    mock_client.table.assert_not_called()


def test_no_viable_candidates_writes_nothing(set_tenant):
    mock_client = MagicMock()
    with patch("core.db.get_client", return_value=mock_client):
        set_tenant.write_premarket_outcome_evals(
            session_id="sess-nvc",
            trades_proposed=0,
            trades_approved=0,
            terminal_reason="no_viable_candidates",
        )
    mock_client.table.assert_not_called()


def test_missing_tenant_id_writes_nothing(monkeypatch):
    monkeypatch.delenv("TENANT_ID", raising=False)
    import importlib
    import evals.business as biz
    importlib.reload(biz)

    mock_client = MagicMock()
    with patch("core.db.get_client", return_value=mock_client):
        biz.write_premarket_outcome_evals(
            session_id="sess-notenant",
            trades_proposed=3,
            trades_approved=2,
            terminal_reason="trades_approved",
        )
    mock_client.table.assert_not_called()


def test_db_error_is_swallowed(set_tenant, capsys):
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute.side_effect = RuntimeError("db down")

    with patch("core.db.get_client", return_value=mock_client):
        # Should not raise
        set_tenant.write_premarket_outcome_evals(
            session_id="sess-err",
            trades_proposed=2,
            trades_approved=1,
            terminal_reason="trades_approved",
        )

    captured = capsys.readouterr()
    assert "L5 write failed" in captured.out


def test_rows_have_required_fields(set_tenant):
    rows = _call(set_tenant, trades_proposed=3, trades_approved=2)
    required = {"id", "tenant_id", "session_id", "eval_name", "agent", "layer",
                "score", "passed", "threshold", "detail"}
    for row in rows:
        assert required.issubset(row.keys()), f"Missing fields in {row['eval_name']}"


def test_session_id_propagated(set_tenant):
    rows = _call(set_tenant, session_id="my-session-id",
                 trades_proposed=2, trades_approved=1)
    assert all(r["session_id"] == "my-session-id" for r in rows)
