from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _trace_row(agent: str, reasoning: str, step_type: str = "agent_message") -> dict:
    return {
        "agent":     agent,
        "step_type": step_type,
        "payload":   {"agent_reasoning": reasoning},
    }


def _mock_evaluate(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value={})
    monkeypatch.setattr("evals.judge.evaluate_session_outputs", mock)
    return mock


def _mock_get(monkeypatch, traces: list) -> MagicMock:
    """Patch _ingest_get to return a traces payload."""
    mock = MagicMock(return_value={"traces": traces})
    monkeypatch.setattr("trace.logger._ingest_get", mock)
    return mock


# ── evaluate_session_from_traces ──────────────────────────────────────────────

class TestEvaluateSessionFromTraces:
    def test_reads_agent_reasoning_not_outcome(self, monkeypatch):
        _mock_get(monkeypatch, [_trace_row("research_AAPL", "AAPL shows strong momentum with ATR 2.1%")])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        mock_ev.assert_called_once()
        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "momentum" in agent_outputs["research"]

    def test_strips_ticker_suffix_from_agent_name(self, monkeypatch):
        _mock_get(monkeypatch, [
            _trace_row("research_MSFT", "MSFT is consolidating near VWAP"),
            _trace_row("research_NVDA", "NVDA has high relative strength"),
        ])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "research_MSFT" not in agent_outputs
        assert "research_NVDA" not in agent_outputs

    def test_combines_up_to_5_reasoning_blocks(self, monkeypatch):
        tickers = ["AAPL", "MSFT", "GOOG", "META", "NVDA", "TSLA", "AMZN"]
        rows = [_trace_row(f"research_{t}", f"reasoning block {i}") for i, t in enumerate(tickers)]
        _mock_get(monkeypatch, rows)
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        combined = mock_ev.call_args[0][1]["research"]
        assert combined.count(" | ") == 4
        assert "reasoning block 5" not in combined
        assert "reasoning block 6" not in combined

    def test_skips_rows_with_empty_reasoning(self, monkeypatch):
        _mock_get(monkeypatch, [
            _trace_row("research_AAPL", ""),
            _trace_row("research_MSFT", "MSFT has good setup"),
        ])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        combined = mock_ev.call_args[0][1]["research"]
        assert "MSFT" in combined
        assert combined.count(" | ") == 0

    def test_skips_rows_with_null_payload(self, monkeypatch):
        _mock_get(monkeypatch, [
            {"agent": "orchestrator", "step_type": "agent_message", "payload": None},
            _trace_row("research_AAPL", "good setup"),
        ])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "orchestrator" not in agent_outputs

    def test_does_not_call_evaluate_when_no_traces(self, monkeypatch):
        _mock_get(monkeypatch, [])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        mock_ev.assert_not_called()

    def test_does_not_call_evaluate_when_api_returns_empty(self, monkeypatch):
        mock = MagicMock(return_value={})
        monkeypatch.setattr("trace.logger._ingest_get", mock)
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        mock_ev.assert_not_called()

    def test_groups_multiple_agents_separately(self, monkeypatch):
        _mock_get(monkeypatch, [
            _trace_row("research_AAPL", "AAPL setup"),
            _trace_row("orchestrator",  "Overall conviction is HIGH"),
        ])
        mock_ev = _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        agent_outputs = mock_ev.call_args[0][1]
        assert "research" in agent_outputs
        assert "orchestrator" in agent_outputs
        assert agent_outputs["orchestrator"] == "Overall conviction is HIGH"

    def test_does_not_raise_on_ingest_exception(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(side_effect=Exception("api down")))
        _mock_evaluate(monkeypatch)
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")   # must not raise

    def test_scanner_summary_fields_reordered_before_candidates(self, monkeypatch):
        """regime/scan_rationale/dropped_count must appear first in the judge input."""
        import json
        mock_ev = _mock_evaluate(monkeypatch)
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
        assert len(scanner_json) > 3000

        _mock_get(monkeypatch, [_trace_row("scanner", scanner_json)])
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")

        agent_outputs = mock_ev.call_args[0][1]
        assert "scanner" in agent_outputs
        condensed = json.loads(agent_outputs["scanner"])
        assert condensed["regime"] == "elevated_vix"
        assert condensed["scan_rationale"] == "ELEVATED vix regime applied min_score=5."
        assert condensed["dropped_count"] == 3
        assert condensed["n_returned"] == 20
        assert len(condensed["top_candidates"]) == 5
        assert len(agent_outputs["scanner"]) < 3000

    def test_scanner_reorder_skipped_when_not_valid_json(self, monkeypatch):
        mock_ev = _mock_evaluate(monkeypatch)
        _mock_get(monkeypatch, [_trace_row("scanner", "not json at all")])
        from evals.judge import evaluate_session_from_traces
        evaluate_session_from_traces("sess-1")
        assert mock_ev.call_args[0][1]["scanner"] == "not json at all"


# ── _fetch_criteria ───────────────────────────────────────────────────────────

class TestFetchCriteria:
    def test_returns_configs_grouped_by_agent(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(return_value={
            "configs": [
                {"id": "c1", "eval_name": "research_yield", "agent": "research", "threshold": 0.7, "config": {}},
                {"id": "c2", "eval_name": "risk_clarity",   "agent": "risk",     "threshold": 0.7, "config": {}},
            ]
        }))
        from evals.judge import _fetch_criteria
        result = _fetch_criteria(["research", "risk"])
        assert "research" in result
        assert "risk" in result
        assert result["research"][0]["eval_name"] == "research_yield"

    def test_wildcard_agent_applies_to_all(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(return_value={
            "configs": [
                {"id": "c1", "eval_name": "exit_quality", "agent": "*", "threshold": 0.7, "config": {}},
            ]
        }))
        from evals.judge import _fetch_criteria
        result = _fetch_criteria(["research", "risk"])
        assert "research" in result
        assert "risk" in result

    def test_ignores_agents_not_in_agent_names(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(return_value={
            "configs": [
                {"id": "c1", "eval_name": "x", "agent": "market", "threshold": 0.7, "config": {}},
            ]
        }))
        from evals.judge import _fetch_criteria
        result = _fetch_criteria(["research"])
        assert result == {}

    def test_returns_empty_on_empty_agent_names(self, monkeypatch):
        from evals.judge import _fetch_criteria
        assert _fetch_criteria([]) == {}

    def test_skips_non_l4_layer_configs(self, monkeypatch):
        # L3 rule checks (e.g. orchestrator decision_made / exit_quality) must not be
        # run through the LLM judge; only L4 semantic criteria are judged. Configs with
        # no layer field stay judgeable (backward compat).
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(return_value={
            "configs": [
                {"id": "c1", "eval_name": "decision_made", "agent": "orchestrator", "layer": 3, "threshold": 0.9, "config": {}},
                {"id": "c2", "eval_name": "exit_quality", "agent": "orchestrator", "layer": 3, "threshold": 0.9, "config": {}},
                {"id": "c3", "eval_name": "orchestrator_synthesis", "agent": "orchestrator", "layer": 4, "threshold": 0.7, "config": {"prompt": "Did it synthesize all inputs?"}},
                {"id": "c4", "eval_name": "legacy_no_layer", "agent": "orchestrator", "threshold": 0.7, "config": {"prompt": "x"}},
            ]
        }))
        from evals.judge import _fetch_criteria
        names = [c["eval_name"] for c in _fetch_criteria(["orchestrator"]).get("orchestrator", [])]
        assert "decision_made" not in names
        assert "exit_quality" not in names
        assert "orchestrator_synthesis" in names   # L4 judged
        assert "legacy_no_layer" in names          # layer absent → still judged

    def test_returns_empty_when_api_returns_empty(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(return_value={}))
        from evals.judge import _fetch_criteria
        assert _fetch_criteria(["research"]) == {}

    def test_returns_empty_on_api_exception(self, monkeypatch):
        monkeypatch.setattr("trace.logger._ingest_get", MagicMock(side_effect=Exception("api down")))
        from evals.judge import _fetch_criteria
        assert _fetch_criteria(["research"]) == {}


# ── _patch_session_quality_score ──────────────────────────────────────────────

class TestPatchSessionQualityScore:
    def test_patches_quality_score_via_ingest_api(self, monkeypatch):
        mock_patch = MagicMock()
        monkeypatch.setattr("trace.logger._ingest_patch", mock_patch)
        from evals.judge import _patch_session_quality_score
        _patch_session_quality_score("sess-1", {
            "research": [{"score": 0.8}, {"score": 0.6}],
            "risk":     [{"score": 1.0}],
        })
        mock_patch.assert_called_once()
        path, payload = mock_patch.call_args[0]
        assert path == "/api/ingest/session"
        assert payload["session_id"] == "sess-1"
        expected = round((0.8 + 0.6 + 1.0) / 3, 4)
        assert payload["quality_score"] == pytest.approx(expected, rel=1e-4)

    def test_skips_when_no_scores(self, monkeypatch):
        mock_patch = MagicMock()
        monkeypatch.setattr("trace.logger._ingest_patch", mock_patch)
        from evals.judge import _patch_session_quality_score
        _patch_session_quality_score("sess-1", {})
        mock_patch.assert_not_called()

    def test_does_not_raise_on_exception(self, monkeypatch, capsys):
        monkeypatch.setattr("trace.logger._ingest_patch", MagicMock(side_effect=RuntimeError("api down")))
        from evals.judge import _patch_session_quality_score
        _patch_session_quality_score("sess-1", {"research": [{"score": 0.7}]})  # must not raise
        out = capsys.readouterr().out
        assert "quality_score patch failed" in out
