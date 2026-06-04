from __future__ import annotations

import json

import pytest

from evals.eval_scanner import (
    compute_regime_accuracy,
    compute_session_funnel,
    compute_tool_usage,
    _parse_tool_output,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _decision_row(session_id: str, regime: str, n_returned: int, dropped: int,
                  cost_usd: float = 0.0) -> dict:
    return {
        "session_id": session_id,
        "agent":      "scanner",
        "step_type":  "decision",
        "tool_output": json.dumps({
            "regime":        regime,
            "n_returned":    n_returned,
            "dropped_count": dropped,
            "scan_rationale": f"{regime} rationale",
        }),
    }


def _session(sid: str, proposed: int, approved: int, executed: int,
             scanner_cost: float = 0.0) -> dict:
    cb = {"scanner": {"cost_usd": scanner_cost}} if scanner_cost else {}
    return {
        "id":               sid,
        "date":             "2026-06-04",
        "trades_proposed":  proposed,
        "trades_approved":  approved,
        "trades_executed":  executed,
        "cost_breakdown":   json.dumps(cb) if cb else {},
    }


def _tool_row(session_id: str, tool_name: str) -> dict:
    return {
        "session_id": session_id,
        "agent":      "scanner",
        "step_type":  "tool_call",
        "tool_name":  tool_name,
    }


# ── _parse_tool_output ────────────────────────────────────────────────────────

class TestParseToolOutput:
    def test_dict_passthrough(self):
        row = {"tool_output": {"n_returned": 3}}
        assert _parse_tool_output(row) == {"n_returned": 3}

    def test_json_string_parsed(self):
        row = {"tool_output": json.dumps({"regime": "low_vix"})}
        assert _parse_tool_output(row)["regime"] == "low_vix"

    def test_invalid_string_returns_empty(self):
        row = {"tool_output": "not json"}
        assert _parse_tool_output(row) == {}

    def test_none_returns_empty(self):
        assert _parse_tool_output({"tool_output": None}) == {}

    def test_missing_key_returns_empty(self):
        assert _parse_tool_output({}) == {}


# ── compute_session_funnel ────────────────────────────────────────────────────

class TestComputeSessionFunnel:
    def test_basic_funnel_row(self):
        traces   = [_decision_row("s1", "low_vix", 10, 5)]
        sessions = [_session("s1", proposed=4, approved=3, executed=2)]
        result = compute_session_funnel(sessions, traces)
        assert len(result) == 1
        row = result[0]
        assert row["session_id"] == "s1"
        assert row["n_candidates"] == 10
        assert row["n_proposed"]   == 4
        assert row["n_approved"]   == 3
        assert row["n_executed"]   == 2
        assert row["dropped_count"] == 5

    def test_selection_rate_computed(self):
        traces   = [_decision_row("s1", "low_vix", 10, 0)]
        sessions = [_session("s1", proposed=5, approved=3, executed=1)]
        row = compute_session_funnel(sessions, traces)[0]
        assert row["selection_rate"]  == 0.5
        assert row["acceptance_rate"] == 0.3
        assert row["trade_rate"]      == 0.1

    def test_zero_candidates_rates_are_zero(self):
        traces   = [_decision_row("s1", "high_vix", 0, 0)]
        sessions = [_session("s1", proposed=0, approved=0, executed=0)]
        row = compute_session_funnel(sessions, traces)[0]
        assert row["selection_rate"]  == 0.0
        assert row["acceptance_rate"] == 0.0
        assert row["trade_rate"]      == 0.0

    def test_session_without_scanner_decision_excluded(self):
        sessions = [_session("s1", proposed=2, approved=1, executed=1)]
        result = compute_session_funnel(sessions, [])
        assert result == []

    def test_cost_per_trade_computed(self):
        traces   = [_decision_row("s1", "low_vix", 5, 1, cost_usd=0.02)]
        sessions = [_session("s1", proposed=2, approved=2, executed=2, scanner_cost=0.02)]
        row = compute_session_funnel(sessions, traces)[0]
        assert row["scanner_cost_usd"] == 0.02
        assert row["cost_per_trade"]   == 0.01

    def test_cost_per_trade_none_when_zero_executed(self):
        traces   = [_decision_row("s1", "high_vix", 3, 3)]
        sessions = [_session("s1", proposed=0, approved=0, executed=0, scanner_cost=0.005)]
        row = compute_session_funnel(sessions, traces)[0]
        assert row["cost_per_trade"] is None

    def test_regime_carried_through(self):
        traces   = [_decision_row("s1", "caution", 8, 6)]
        sessions = [_session("s1", proposed=2, approved=2, executed=2)]
        row = compute_session_funnel(sessions, traces)[0]
        assert row["regime"] == "caution"

    def test_multiple_sessions(self):
        traces = [
            _decision_row("s1", "low_vix",  10, 2),
            _decision_row("s2", "high_vix",  5, 4),
        ]
        sessions = [
            _session("s1", proposed=3, approved=2, executed=2),
            _session("s2", proposed=1, approved=1, executed=1),
        ]
        result = compute_session_funnel(sessions, traces)
        assert len(result) == 2
        sids = {r["session_id"] for r in result}
        assert sids == {"s1", "s2"}

    def test_cost_breakdown_as_dict(self):
        traces   = [_decision_row("s1", "low_vix", 5, 1)]
        session  = _session("s1", proposed=1, approved=1, executed=1)
        session["cost_breakdown"] = {"scanner": {"cost_usd": 0.03}}
        row = compute_session_funnel([session], traces)[0]
        assert row["scanner_cost_usd"] == 0.03


# ── compute_regime_accuracy ───────────────────────────────────────────────────

class TestComputeRegimeAccuracy:
    def _traces(self, entries: list[tuple[str, int]]) -> list[dict]:
        return [
            {
                "step_type":  "decision",
                "agent":      "scanner",
                "tool_output": json.dumps({"regime": regime, "dropped_count": dropped}),
            }
            for regime, dropped in entries
        ]

    def test_high_drops_more_than_low(self):
        traces = self._traces([
            ("high_vix", 20), ("high_vix", 18),
            ("low_vix",   5), ("low_vix",   3),
        ])
        result = compute_regime_accuracy(traces)
        assert result["high_vix_sessions"] == 2
        assert result["low_vix_sessions"]  == 2
        assert result["avg_dropped_high_vix"] > result["avg_dropped_low_vix"]
        assert result["regime_differentiation"] is True

    def test_caution_counted_as_high_vix(self):
        traces = self._traces([
            ("caution", 15),
            ("low_vix",  3),
        ])
        result = compute_regime_accuracy(traces)
        assert result["high_vix_sessions"] == 1

    def test_no_differentiation_when_low_drops_more(self):
        traces = self._traces([
            ("high_vix",  2),
            ("low_vix",  10),
        ])
        result = compute_regime_accuracy(traces)
        assert result["regime_differentiation"] is False

    def test_none_when_only_one_regime_present(self):
        traces = self._traces([("high_vix", 10), ("high_vix", 12)])
        result = compute_regime_accuracy(traces)
        assert result["regime_differentiation"] is None

    def test_empty_traces_returns_zeros(self):
        result = compute_regime_accuracy([])
        assert result["high_vix_sessions"] == 0
        assert result["low_vix_sessions"]  == 0
        assert result["regime_differentiation"] is None

    def test_non_decision_rows_ignored(self):
        traces = [
            {"step_type": "tool_call", "agent": "scanner",
             "tool_output": json.dumps({"regime": "high_vix", "dropped_count": 99})},
            {"step_type": "decision",  "agent": "scanner",
             "tool_output": json.dumps({"regime": "low_vix",  "dropped_count": 2})},
        ]
        result = compute_regime_accuracy(traces)
        assert result["high_vix_sessions"] == 0
        assert result["low_vix_sessions"]  == 1

    def test_average_dropped_computed_correctly(self):
        traces = self._traces([("high_vix", 10), ("high_vix", 20)])
        result = compute_regime_accuracy(traces)
        assert result["avg_dropped_high_vix"] == 15.0


# ── compute_tool_usage ────────────────────────────────────────────────────────

class TestComputeToolUsage:
    def test_counts_each_tool(self):
        traces = [
            _tool_row("s1", "get_scan_results"),
            _tool_row("s1", "get_scan_results"),
            _tool_row("s1", "get_gap_ups"),
            _tool_row("s1", "filter_and_rank"),
        ]
        result = compute_tool_usage(traces)
        assert result["get_scan_results"] == 2
        assert result["get_gap_ups"]      == 1
        assert result["filter_and_rank"]  == 1

    def test_non_tool_call_rows_ignored(self):
        traces = [
            {"step_type": "decision", "agent": "scanner", "tool_name": "get_scan_results"},
            _tool_row("s1", "get_gap_ups"),
        ]
        result = compute_tool_usage(traces)
        assert result.get("get_scan_results", 0) == 0
        assert result["get_gap_ups"] == 1

    def test_empty_traces_returns_empty_dict(self):
        assert compute_tool_usage([]) == {}

    def test_missing_tool_name_counted_as_unknown(self):
        traces = [{"step_type": "tool_call", "agent": "scanner", "tool_name": None}]
        result = compute_tool_usage(traces)
        assert result.get("unknown", 0) == 1
