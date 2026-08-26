"""Tests for evals/business.py — L3 funnel throughput evals (migrated from L5)."""
from __future__ import annotations

from unittest.mock import patch
import pytest


def _call_business(session_id="sess-1", trades_proposed=3,
                   trades_approved=2, terminal_reason="trades_approved"):
    """Call write_funnel_evals with mocked _ingest_post. Returns list of posted payloads."""
    from evals.business import write_funnel_evals
    posted = []

    def capture(path, payload):
        if path == "/api/ingest/eval":
            posted.append(payload)

    with patch("trace.logger._ingest_post", side_effect=capture):
        write_funnel_evals(
            session_id=session_id,
            trades_proposed=trades_proposed,
            trades_approved=trades_approved,
            terminal_reason=terminal_reason,
        )
    return posted


@pytest.fixture(autouse=True)
def set_tenant(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "test-tenant-id")


def test_writes_two_evals_when_pipeline_ran():
    payloads = _call_business(trades_proposed=3, trades_approved=2)
    assert len(payloads) == 2
    names = {p["eval_name"] for p in payloads}
    assert names == {"research_yield", "risk_approval_rate"}


def test_all_evals_have_layer_3():
    payloads = _call_business(trades_proposed=3, trades_approved=2)
    assert all(p["layer"] == 3 for p in payloads)


def test_research_yield_passes_when_proposals_exist():
    payloads = _call_business(trades_proposed=2, trades_approved=1)
    row = next(p for p in payloads if p["eval_name"] == "research_yield")
    assert row["score"] == 1.0
    assert row["passed"] is True


def test_research_yield_fails_when_no_proposals():
    payloads = _call_business(trades_proposed=0, trades_approved=0,
                              terminal_reason="no_viable_proposals")
    row = next(p for p in payloads if p["eval_name"] == "research_yield")
    assert row["score"] == 0.0
    assert row["passed"] is False


def test_risk_approval_rate_computed_correctly():
    payloads = _call_business(trades_proposed=4, trades_approved=1)
    row = next(p for p in payloads if p["eval_name"] == "risk_approval_rate")
    assert row["score"] == pytest.approx(0.25)


def test_risk_approval_rate_passes_at_threshold():
    payloads = _call_business(trades_proposed=5, trades_approved=1)
    row = next(p for p in payloads if p["eval_name"] == "risk_approval_rate")
    assert row["passed"] is True


def test_risk_approval_rate_fails_below_threshold():
    payloads = _call_business(trades_proposed=5, trades_approved=0)
    row = next(p for p in payloads if p["eval_name"] == "risk_approval_rate")
    assert row["passed"] is False


def test_no_risk_row_when_no_proposals():
    payloads = _call_business(trades_proposed=0, trades_approved=0,
                              terminal_reason="no_viable_proposals")
    names = {p["eval_name"] for p in payloads}
    assert "risk_approval_rate" not in names


def test_skip_session_writes_nothing():
    payloads = _call_business(trades_proposed=0, trades_approved=0,
                               terminal_reason="skip_propagated")
    assert payloads == []


def test_no_candidates_writes_nothing():
    payloads = _call_business(trades_proposed=0, trades_approved=0,
                               terminal_reason="no_candidates")
    assert payloads == []


def test_no_viable_candidates_writes_nothing():
    payloads = _call_business(trades_proposed=0, trades_approved=0,
                               terminal_reason="no_viable_candidates")
    assert payloads == []


def test_missing_tenant_id_writes_nothing(monkeypatch):
    monkeypatch.delenv("TENANT_ID", raising=False)
    from evals.business import write_funnel_evals
    posted = []
    with patch("trace.logger._ingest_post", side_effect=lambda p, pl: posted.append(pl)):
        write_funnel_evals("sess-notenant", 3, 2, "trades_approved")
    assert posted == []


def test_ingest_error_is_swallowed(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "test-tenant-id")
    from evals.business import write_funnel_evals
    with patch("trace.logger._ingest_post", side_effect=RuntimeError("network error")):
        # Should not raise
        write_funnel_evals("sess-err", 2, 1, "trades_approved")


def test_evals_have_required_fields():
    payloads = _call_business(trades_proposed=3, trades_approved=2)
    required = {"session_id", "eval_name", "agent", "layer", "score", "passed", "threshold", "detail"}
    for p in payloads:
        assert required.issubset(p.keys()), f"Missing fields in {p['eval_name']}"


def test_session_id_propagated():
    payloads = _call_business(session_id="my-session-id",
                              trades_proposed=2, trades_approved=1)
    assert all(p["session_id"] == "my-session-id" for p in payloads)
