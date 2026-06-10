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

    def test_scanner_summary_fields_reordered_before_candidates(self, mock_supabase, monkeypatch):
        """regime/scan_rationale/dropped_count must appear first in the judge input.

        The scanner's full JSON is ~3,600 chars; the judge window is 3,000 chars.
        Without reordering, the summary fields get truncated and the judge marks
        them as missing — producing a false-positive incident (root cause: June 9 backfill).
        """
        import json
        mock_ev = _mock_evaluate(monkeypatch)

        # Build a scanner output that mimics the real one: 20 candidates + summary fields at end
        # Use indent=2 + code fences to match how the scanner LLM formats its output.
        # Short synthetic tickers produce ~2400 chars compact; indented with fences hits ~3600.
        candidates = [
            {"ticker": f"T{i}", "technical_score": 6, "premarket_change_pct": 1.0 + i,
             "price": 100.0 + i, "sector": "Consumer Discretionary"}
            for i in range(20)
        ]
        scanner_json = "```json\n" + json.dumps({
            "candidates":    candidates,
            "n_returned":    20,
            "scan_rationale": "ELEVATED vix regime applied min_score=5.",
            "signals_used":  ["technical_score>=5", "premarket_change_pct"],
            "regime":        "elevated_vix",
            "dropped_count": 3,
        }, indent=2) + "\n```"
        assert len(scanner_json) > 3000, "fixture must exceed 3000 chars to be a valid regression test"

        self._db(mock_supabase, [_trace_row("scanner", scanner_json)])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert "scanner" in agent_outputs
        condensed = json.loads(agent_outputs["scanner"])

        # Summary fields present and correct
        assert condensed["regime"] == "elevated_vix"
        assert condensed["scan_rationale"] == "ELEVATED vix regime applied min_score=5."
        assert condensed["dropped_count"] == 3
        assert condensed["n_returned"] == 20
        # Full candidates list replaced by top 5
        assert len(condensed["top_candidates"]) == 5
        # Result must fit within the judge window
        assert len(agent_outputs["scanner"]) < 3000

    def test_scanner_reorder_skipped_when_not_valid_json(self, mock_supabase, monkeypatch):
        """If scanner reasoning is not JSON, leave it unchanged rather than raising."""
        mock_ev = _mock_evaluate(monkeypatch)
        self._db(mock_supabase, [_trace_row("scanner", "not json at all")])
        with patch("evals.judge._TENANT_ID", "tenant-1"):
            from evals.judge import evaluate_session_from_traces
            evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert agent_outputs["scanner"] == "not json at all"


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
