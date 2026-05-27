from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from trace.logger import TraceLogger, _agent_model, _estimate_cost
from tests.conftest import make_query


@pytest.fixture
def tracer(mock_supabase) -> TraceLogger:
    mock_supabase.table.return_value = make_query([])
    return TraceLogger("test-session-id-1234")


# ── start_agent_span ───────────────────────────────────────────────────────────

class TestStartAgentSpan:
    def test_returns_uuid_string(self, tracer):
        span_id = tracer.start_agent_span("market")
        assert isinstance(span_id, str)
        assert len(span_id) == 36  # UUID format

    def test_different_agents_get_different_span_ids(self, tracer):
        m = tracer.start_agent_span("market")
        r = tracer.start_agent_span("research")
        assert m != r

    def test_span_id_retrievable(self, tracer):
        span_id = tracer.start_agent_span("market")
        assert tracer.get_agent_span("market") == span_id

    def test_second_call_overwrites_span(self, tracer):
        tracer.start_agent_span("market")
        second = tracer.start_agent_span("market")
        assert tracer.get_agent_span("market") == second


# ── log_tool_call ──────────────────────────────────────────────────────────────

class TestLogToolCall:
    def test_writes_row_to_c_traces(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_tool_call("market", "get_vix", {}, {"vix": 18.4}, latency_ms=220)

        assert query.insert.called
        row = query.insert.call_args[0][0]
        assert row["step_type"] == "tool_call"
        assert row["agent"] == "market"
        assert row["tool_name"] == "get_vix"
        assert row["tool_input"] == {}
        assert row["tool_output"] == {"vix": 18.4}
        assert row["latency_ms"] == 220
        assert row["session_id"] == "test-session-id-1234"

    def test_returns_span_id_string(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        span_id = tracer.log_tool_call("market", "get_vix", {}, {})
        assert isinstance(span_id, str) and len(span_id) == 36

    def test_parent_span_id_set_after_start_agent_span(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query
        agent_span = tracer.start_agent_span("research")

        tracer.log_tool_call("research", "get_candidates", {}, {})

        row = query.insert.call_args[0][0]
        assert row["parent_span_id"] == agent_span

    def test_entity_id_included_when_provided(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_tool_call("research", "get_news", {"ticker": "AAPL"}, {}, entity_id="AAPL")

        row = query.insert.call_args[0][0]
        assert row["entity_id"] == "AAPL"

    def test_sequence_increments_per_call(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert tracer.get_sequence() == 0
        tracer.log_tool_call("market", "get_vix", {}, {})
        assert tracer.get_sequence() == 1
        tracer.log_tool_call("market", "get_futures", {}, {})
        assert tracer.get_sequence() == 2

    def test_non_dict_tool_output_wrapped(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_tool_call("market", "get_vix", {}, 18.4)

        row = query.insert.call_args[0][0]
        assert row["tool_output"] == {"value": 18.4}


# ── log_agent_message ──────────────────────────────────────────────────────────

class TestLogAgentMessage:
    def test_writes_row_with_correct_step_type(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_agent_message("market", "VIX at 18 — GO", "go",
                                 tokens_input=400, tokens_output=80, model="claude-haiku-4-5-20251001")

        row = query.insert.call_args[0][0]
        assert row["step_type"] == "agent_message"
        assert row["agent"] == "market"
        assert row["agent_reasoning"] == "VIX at 18 — GO"
        assert row["outcome"] == "go"
        assert row["tokens_input"] == 400
        assert row["tokens_output"] == 80
        assert row["model"] == "claude-haiku-4-5-20251001"

    def test_returns_span_id(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        span_id = tracer.log_agent_message("market", "reasoning", "go")
        assert len(span_id) == 36


# ── log_decision ───────────────────────────────────────────────────────────────

class TestLogDecision:
    def test_writes_decision_row(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_decision("orchestrator", "converged", detail={"trades": 3})

        row = query.insert.call_args[0][0]
        assert row["step_type"] == "decision"
        assert row["outcome"] == "converged"
        assert row["tool_output"] == {"trades": 3}

    def test_no_entity_id_on_decision(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query
        tracer.log_decision("orchestrator", "skip_propagated")
        row = query.insert.call_args[0][0]
        assert row["entity_id"] is None


# ── log_error ─────────────────────────────────────────────────────────────────

class TestLogError:
    def test_writes_error_row(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_error("research", "JSON parse failed")

        row = query.insert.call_args[0][0]
        assert row["step_type"] == "error"
        assert row["error"] == "JSON parse failed"
        assert row["outcome"] == "error"


# ── log_tokens ────────────────────────────────────────────────────────────────

class TestLogTokens:
    def test_accumulates_tokens_per_agent(self, tracer):
        usage1 = MagicMock()
        usage1.input_tokens = 1000
        usage1.output_tokens = 200
        usage2 = MagicMock()
        usage2.input_tokens = 500
        usage2.output_tokens = 100

        tracer.log_tokens("market", usage1)
        tracer.log_tokens("market", usage2)

        assert tracer._tokens["market"]["input"] == 1500
        assert tracer._tokens["market"]["output"] == 300

    def test_tracks_multiple_agents_separately(self, tracer):
        market_usage = MagicMock(input_tokens=400, output_tokens=80)
        research_usage = MagicMock(input_tokens=3000, output_tokens=600)

        tracer.log_tokens("market", market_usage)
        tracer.log_tokens("research", research_usage)

        assert tracer._tokens["market"]["input"] == 400
        assert tracer._tokens["research"]["input"] == 3000

    def test_accepts_dict_usage(self, tracer):
        tracer.log_tokens("risk", {"input_tokens": 200, "output_tokens": 50})
        assert tracer._tokens["risk"]["input"] == 200
        assert tracer._tokens["risk"]["output"] == 50


# ── close_session ─────────────────────────────────────────────────────────────

class TestCloseSession:
    def test_writes_c_sessions_row(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        tracer.log_tokens("market", MagicMock(input_tokens=400, output_tokens=80))
        tracer.close_session(
            terminal_reason="converged",
            trades_proposed=3,
            trades_approved=2,
            trades_executed=2,
            retry_triggered=False,
        )

        assert query.insert.called
        row = query.insert.call_args[0][0]
        assert row["id"] == "test-session-id-1234"
        assert row["terminal_reason"] == "converged"
        assert row["trades_proposed"] == 3
        assert row["trades_approved"] == 2
        assert row["trades_executed"] == 2
        assert row["retry_triggered"] is False
        assert row["total_tokens_input"] == 400
        assert row["total_tokens_output"] == 80

    def test_cost_is_positive_float(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer.log_tokens("market", MagicMock(input_tokens=1000, output_tokens=200))
        tracer.close_session("converged")
        row = mock_supabase.table.return_value.insert.call_args[0][0]
        assert row["total_cost_usd"] > 0

    def test_session_id_in_row(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer.close_session("structural_block")
        row = mock_supabase.table.return_value.insert.call_args[0][0]
        assert row["id"] == "test-session-id-1234"

    def test_total_steps_reflects_logged_calls(self, tracer, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        tracer.log_tool_call("market", "get_vix", {}, {})
        tracer.log_tool_call("market", "get_futures", {}, {})
        tracer.log_decision("orchestrator", "converged")
        tracer.close_session("converged")
        row = mock_supabase.table.return_value.insert.call_args[0][0]
        assert row["total_steps"] == 3


# ── ingest_otel_span ──────────────────────────────────────────────────────────

class TestIngestOtelSpan:
    def test_normalizes_and_inserts_otel_span(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query

        span = {
            "traceId": "abc123",
            "spanId":  "def456",
            "parentSpanId": None,
            "name": "news_analyst.get_ticker_news",
            "startTimeUnixNano": 1000000000,
            "endTimeUnixNano":   1840000000,
            "attributes": {
                "agent.name":      "news_analyst",
                "agent.language":  "typescript",
                "session.id":      "test-session-id-1234",
                "tool.name":       "get_ticker_news",
                "tool.input.ticker": "AAPL",
                "tool.output.signal": "neutral",
                "model":           "claude-haiku-4-5-20251001",
            },
        }
        tracer.ingest_otel_span(span)

        assert query.insert.called
        row = query.insert.call_args[0][0]
        assert row["agent"] == "news_analyst"
        assert row["tool_name"] == "get_ticker_news"
        assert row["session_id"] == "test-session-id-1234"

    def test_skips_span_without_agent_name(self, tracer, mock_supabase):
        query = make_query([])
        mock_supabase.table.return_value = query
        tracer.ingest_otel_span({"attributes": {}})
        query.insert.assert_not_called()


# ── cost helpers ───────────────────────────────────────────────────────────────

class TestCostHelpers:
    def test_estimate_cost_haiku(self):
        cost = _estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0)
        assert cost == pytest.approx(0.80, rel=1e-3)

    def test_estimate_cost_sonnet(self):
        cost = _estimate_cost("claude-sonnet-4-6", 1_000_000, 0)
        assert cost == pytest.approx(3.00, rel=1e-3)

    def test_estimate_cost_unknown_model_uses_sonnet_rate(self):
        cost = _estimate_cost("unknown-model", 1_000_000, 0)
        assert cost == pytest.approx(3.00, rel=1e-3)

    def test_agent_model_haiku_agents(self):
        assert _agent_model("market") == "claude-haiku-4-5-20251001"
        assert _agent_model("risk")   == "claude-haiku-4-5-20251001"
        assert _agent_model("news_analyst") == "claude-haiku-4-5-20251001"

    def test_agent_model_sonnet_agents(self):
        assert _agent_model("research")     == "claude-sonnet-4-6"
        assert _agent_model("orchestrator") == "claude-sonnet-4-6"
        assert _agent_model("learning")     == "claude-sonnet-4-6"
