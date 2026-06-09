from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from tests.conftest import make_query


# ── helpers ───────────────────────────────────────────────────────────────────

def _trace_row(agent: str, reasoning: str, step_type: str = "agent_message") -> dict:
    return {
        "agent":     agent,
        "step_type": step_type,
        "payload":   {"agent_reasoning": reasoning},
    }


def _mock_evaluate(monkeypatch) -> MagicMock:
    """Patch evaluate_session_outputs so we can inspect calls without hitting Anthropic."""
    mock = MagicMock(return_value={})
    monkeypatch.setattr("evals.judge.evaluate_session_outputs", mock)
    return mock


# ── evaluate_session_from_traces ──────────────────────────────────────────────

class TestEvaluateSessionFromTraces:
    def _db(self, mock_supabase, rows: list) -> None:
        mock_supabase.table.return_value = make_query(rows)

    def test_reads_agent_reasoning_not_outcome(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [
            _trace_row("research_AAPL", "AAPL shows strong momentum with ATR 2.1%"),
        ])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        mock_ev.assert_called_once()
        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "momentum" in agent_outputs["research"]

    def test_strips_ticker_suffix_from_agent_name(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [
            _trace_row("research_MSFT", "MSFT is consolidating near VWAP"),
            _trace_row("research_NVDA", "NVDA has high relative strength"),
        ])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "research_MSFT" not in agent_outputs
        assert "research_NVDA" not in agent_outputs

    def test_combines_up_to_5_reasoning_blocks(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        tickers = ["AAPL", "MSFT", "GOOG", "META", "NVDA", "TSLA", "AMZN"]
        rows = [_trace_row(f"research_{t}", f"reasoning block {i}") for i, t in enumerate(tickers)]
        self._db(mock_supabase, rows)
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        combined = mock_ev.call_args[0][1]["research"]
        # Only first 5 blocks should be joined
        assert combined.count(" | ") == 4
        assert "reasoning block 5" not in combined
        assert "reasoning block 6" not in combined

    def test_skips_rows_with_empty_reasoning(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [
            _trace_row("research_AAPL", ""),
            _trace_row("research_MSFT", "MSFT has good setup"),
        ])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        combined = mock_ev.call_args[0][1]["research"]
        assert "MSFT" in combined
        assert combined.count(" | ") == 0  # only one valid row

    def test_skips_rows_with_null_payload(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [
            {"agent": "orchestrator", "step_type": "agent_message", "payload": None},
            _trace_row("research_AAPL", "good setup"),
        ])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "orchestrator" not in agent_outputs

    def test_does_not_call_evaluate_when_no_rows(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")
        mock_ev.assert_not_called()

    def test_does_not_call_evaluate_when_tenant_id_missing(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        with patch("evals.judge._TENANT_ID", ""):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")
        mock_ev.assert_not_called()

    def test_groups_multiple_agents_separately(self, mock_supabase, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [
            _trace_row("research_AAPL", "AAPL setup"),
            _trace_row("orchestrator",  "Overall conviction is HIGH"),
        ])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "orchestrator" in agent_outputs
        assert agent_outputs["orchestrator"] == "Overall conviction is HIGH"

    def test_does_not_raise_on_db_exception(self, mock_supabase, monkeypatch):
        _mock_evaluate(monkeypatch)
        mock_supabase.table.side_effect = Exception("db down")
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")   # must not raise


# ── _patch_session_quality_score ──────────────────────────────────────────────

class TestPatchSessionQualityScore:
    def test_writes_avg_score_to_ag_sessions(self, mock_supabase):
        from evals.judge import _patch_session_quality_score
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            _patch_session_quality_score("sess-1", {
                "research": [{"score": 0.8}, {"score": 0.6}],
                "risk":     [{"score": 1.0}],
            })
        expected = round((0.8 + 0.6 + 1.0) / 3, 4)
        mock_supabase.table.assert_called_with("ag_sessions")
        mock_supabase.table.return_value.update.assert_called_once_with(
            {"quality_score": expected}
        )

    def test_skips_when_no_scores(self, mock_supabase):
        from evals.judge import _patch_session_quality_score
        _patch_session_quality_score("sess-1", {})
        mock_supabase.table.assert_not_called()

    def test_does_not_raise_on_db_error(self, mock_supabase, capsys):
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.side_effect = RuntimeError("db down")
        from evals.judge import _patch_session_quality_score
        _patch_session_quality_score("sess-1", {"research": [{"score": 0.7}]})  # must not raise
        out = capsys.readouterr().out
        assert "quality_score patch failed" in out
