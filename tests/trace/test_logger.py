from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trace.logger import TraceLogger, _agent_model, _estimate_cost
from tests.conftest import make_query


def _trace_calls(mock_ingest_post):
    """Return payloads for all /api/ingest/trace calls."""
    return [c[0][1] for c in mock_ingest_post.call_args_list if c[0][0] == "/api/ingest/trace"]


def _last_trace(mock_ingest_post):
    calls = _trace_calls(mock_ingest_post)
    assert calls, "No /api/ingest/trace calls found"
    return calls[-1]


def _close_payload(mock_ingest_post):
    for c in mock_ingest_post.call_args_list:
        if c[0][0] == "/api/ingest/session/close":
            return c[0][1]
    raise AssertionError("No /api/ingest/session/close call found")


def _open_payload(mock_ingest_post):
    for c in mock_ingest_post.call_args_list:
        if c[0][0] == "/api/ingest/session/open":
            return c[0][1]
    raise AssertionError("No /api/ingest/session/open call found")


# ── start_agent_span ───────────────────────────────────────────────────────────

class TestStartAgentSpan:
    def test_returns_uuid_string(self, tracer):
        span_id = tracer.start_agent_span("market")
        assert isinstance(span_id, str) and len(span_id) == 36

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
    def test_posts_to_ingest_trace(self, tracer, mock_ingest_post):
        tracer.log_tool_call("market", "get_vix", {}, {"vix": 18.4}, latency_ms=220)
        p = _last_trace(mock_ingest_post)
        assert p["step_type"] == "tool_call"
        assert p["agent"] == "market"
        assert p["tool_name"] == "get_vix"
        assert p["payload"]["tool_output"] == {"vix": 18.4}
        assert p["latency_ms"] == 220
        assert p["session_id"] == "test-session-id-1234"

    def test_returns_span_id_string(self, tracer, mock_ingest_post):
        span_id = tracer.log_tool_call("market", "get_vix", {}, {})
        assert isinstance(span_id, str) and len(span_id) == 36

    def test_parent_span_id_set_after_start_agent_span(self, tracer, mock_ingest_post):
        agent_span = tracer.start_agent_span("research")
        tracer.log_tool_call("research", "get_candidates", {}, {})
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["parent_span_id"] == agent_span

    def test_entity_id_included_when_provided(self, tracer, mock_ingest_post):
        tracer.log_tool_call("research", "get_news", {"ticker": "AAPL"}, {}, entity_id="AAPL")
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["entity_id"] == "AAPL"

    def test_entity_id_auto_derived_for_research_ticker_agent(self, tracer, mock_ingest_post):
        tracer.log_tool_call("research_NVDA", "get_news", {"ticker": "NVDA"}, {})
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["entity_id"] == "NVDA"

    def test_entity_id_explicit_overrides_auto_derive(self, tracer, mock_ingest_post):
        tracer.log_tool_call("research_NVDA", "get_news", {}, {}, entity_id="OVERRIDE")
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["entity_id"] == "OVERRIDE"

    def test_non_research_agent_no_auto_derive(self, tracer, mock_ingest_post):
        tracer.log_tool_call("market", "get_spy_price", {}, {})
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["entity_id"] is None

    def test_sequence_increments_per_call(self, tracer, mock_ingest_post):
        assert tracer.get_sequence() == 0
        tracer.log_tool_call("market", "get_vix", {}, {})
        assert tracer.get_sequence() == 1
        tracer.log_tool_call("market", "get_futures", {}, {})
        assert tracer.get_sequence() == 2

    def test_non_dict_tool_output_wrapped(self, tracer, mock_ingest_post):
        tracer.log_tool_call("market", "get_vix", {}, 18.4)
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["tool_output"] == {"value": 18.4}


# ── log_agent_message ──────────────────────────────────────────────────────────

class TestLogAgentMessage:
    def test_posts_with_correct_step_type(self, tracer, mock_ingest_post):
        tracer.log_agent_message("market", "VIX at 18 — GO", "go",
                                 tokens_input=400, tokens_output=80,
                                 model="claude-haiku-4-5-20251001")
        p = _last_trace(mock_ingest_post)
        assert p["step_type"] == "agent_message"
        assert p["agent"] == "market"
        assert p["payload"]["agent_reasoning"] == "VIX at 18 — GO"
        assert p["outcome"] == "go"
        assert p["tokens_input"] == 400
        assert p["tokens_output"] == 80

    def test_returns_span_id(self, tracer, mock_ingest_post):
        span_id = tracer.log_agent_message("market", "reasoning", "go")
        assert len(span_id) == 36


# ── log_decision ───────────────────────────────────────────────────────────────

class TestLogDecision:
    def test_posts_decision_row(self, tracer, mock_ingest_post):
        tracer.log_decision("orchestrator", "converged", detail={"trades": 3})
        p = _last_trace(mock_ingest_post)
        assert p["step_type"] == "decision"
        assert p["outcome"] == "converged"
        assert p["payload"]["tool_output"] == {"trades": 3}

    def test_no_entity_id_on_decision(self, tracer, mock_ingest_post):
        tracer.log_decision("orchestrator", "skip_propagated")
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["entity_id"] is None


# ── log_error ─────────────────────────────────────────────────────────────────

class TestLogError:
    def test_posts_error_row(self, tracer, mock_ingest_post):
        tracer.log_error("research", "JSON parse failed")
        p = _last_trace(mock_ingest_post)
        assert p["step_type"] == "error"
        assert p["error"] == "JSON parse failed"
        assert p["outcome"] == "error"


# ── log_tokens ────────────────────────────────────────────────────────────────

def _usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
    m = MagicMock()
    m.input_tokens = input_tokens
    m.output_tokens = output_tokens
    m.cache_read_input_tokens = cache_read
    m.cache_creation_input_tokens = cache_write
    return m


class TestLogTokens:
    def test_accumulates_tokens_per_agent(self, tracer):
        tracer.log_tokens("market", _usage(1000, 200))
        tracer.log_tokens("market", _usage(500, 100))
        assert tracer._tokens["market"]["input"] == 1500
        assert tracer._tokens["market"]["output"] == 300

    def test_tracks_multiple_agents_separately(self, tracer):
        tracer.log_tokens("market",   _usage(400, 80))
        tracer.log_tokens("research", _usage(3000, 600))
        assert tracer._tokens["market"]["input"] == 400
        assert tracer._tokens["research"]["input"] == 3000

    def test_accepts_dict_usage(self, tracer):
        tracer.log_tokens("risk", {"input_tokens": 200, "output_tokens": 50})
        assert tracer._tokens["risk"]["input"] == 200
        assert tracer._tokens["risk"]["output"] == 50

    def test_accumulates_cache_tokens(self, tracer):
        tracer.log_tokens("research", _usage(1000, 200, cache_read=500, cache_write=100))
        tracer.log_tokens("research", _usage(800,  150, cache_read=400, cache_write=0))
        assert tracer._tokens["research"]["cache_read"]  == 900
        assert tracer._tokens["research"]["cache_write"] == 100


# ── close_session ─────────────────────────────────────────────────────────────

class TestCloseSession:
    def test_posts_to_ingest_close(self, tracer, mock_ingest_post):
        tracer.log_tokens("market", _usage(400, 80))
        tracer.close_session(
            terminal_reason="converged",
            trades_proposed=3, trades_approved=2, trades_executed=2,
            retry_triggered=False,
        )
        p = _close_payload(mock_ingest_post)
        assert p["terminal_reason"] == "converged"
        assert p["session_id"] == "test-session-id-1234"
        assert p["metadata"]["trades_proposed"] == 3
        assert p["metadata"]["trades_approved"] == 2
        assert p["metadata"]["trades_executed"] == 2
        assert p["total_tokens_in"] == 400
        assert p["total_tokens_out"] == 80

    def test_started_at_not_in_close_payload(self, tracer, mock_ingest_post):
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        assert "started_at" not in p

    def test_no_tokens_omits_cost_fields(self, tracer, mock_ingest_post):
        tracer.close_session("eod_complete")
        p = _close_payload(mock_ingest_post)
        assert "total_cost_usd"   not in p
        assert "total_tokens_in"  not in p
        assert "total_tokens_out" not in p
        assert "cost_breakdown"   not in p.get("metadata", {})

    def test_no_tokens_still_writes_terminal_reason(self, tracer, mock_ingest_post):
        tracer.close_session("eod_complete", trades_executed=2)
        p = _close_payload(mock_ingest_post)
        assert p["terminal_reason"] == "eod_complete"
        assert p["metadata"]["trades_executed"] == 2

    def test_cost_is_positive(self, tracer, mock_ingest_post):
        tracer.log_tokens("market", _usage(1000, 200))
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        assert p["total_cost_usd"] > 0

    def test_cost_breakdown_in_metadata(self, tracer, mock_ingest_post):
        tracer.log_tokens("market",   _usage(400, 80, cache_read=200))
        tracer.log_tokens("research", _usage(3000, 600))
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        breakdown = p["metadata"]["cost_breakdown"]
        assert "market"   in breakdown
        assert "research" in breakdown
        assert breakdown["market"]["cache_read"] == 200

    def test_total_steps_reflects_logged_calls(self, tracer, mock_ingest_post):
        tracer.log_tool_call("market", "get_vix", {}, {})
        tracer.log_tool_call("market", "get_futures", {}, {})
        tracer.log_decision("orchestrator", "converged")
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        assert p["metadata"]["total_steps"] == 3

    def test_result_summary_written_when_provided(self, tracer, mock_ingest_post):
        tracer.close_session("converged", result_summary="2 trade(s) executed: AAPL, MSFT")
        p = _close_payload(mock_ingest_post)
        assert p["result_summary"] == "2 trade(s) executed: AAPL, MSFT"

    def test_result_summary_omitted_when_not_provided(self, tracer, mock_ingest_post):
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        assert "result_summary" not in p


# ── session open (daemon thread) ──────────────────────────────────────────────

class TestSessionOpen:
    def test_session_open_posts_session_id(self, mock_ingest_post, mock_ingest_patch):
        t = TraceLogger("sess-open-001", session_type="premarket")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["session_id"] == "sess-open-001"
        assert p["session_type"] == "premarket"

    def test_session_type_included(self, mock_ingest_post, mock_ingest_patch):
        t = TraceLogger("sess-open-002", session_type="intraday")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["session_type"] == "intraday"

    def test_parent_session_id_included(self, mock_ingest_post, mock_ingest_patch):
        t = TraceLogger("sess-open-003", session_type="intraday", parent_session_id="pre-001")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["parent_session_id"] == "pre-001"

    def test_eod_session_type(self, mock_ingest_post, mock_ingest_patch):
        t = TraceLogger("sess-open-004", session_type="eod")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["session_type"] == "eod"


# ── flush_cost_breakdown (via ingest PATCH) ───────────────────────────────────

class TestFlushCostBreakdown:
    def test_patches_session_with_cost_breakdown(self, tracer, mock_ingest_patch):
        tracer.log_tokens("market", _usage(3000, 900))
        tracer.flush_cost_breakdown()
        assert mock_ingest_patch.called
        path, payload = mock_ingest_patch.call_args[0]
        assert path == "/api/ingest/session"
        assert payload["session_id"] == "test-session-id-1234"
        assert "market" in payload["cost_breakdown"]
        assert payload["total_cost_usd"] > 0

    def test_flush_is_cumulative_across_agents(self, tracer, mock_ingest_patch):
        tracer.log_tokens("market", _usage(3000, 900))
        tracer.log_tokens("research_NVDA", _usage(10000, 2000))
        tracer.flush_cost_breakdown()
        _, payload = mock_ingest_patch.call_args[0]
        assert "market" in payload["cost_breakdown"]
        assert "research_NVDA" in payload["cost_breakdown"]

    def test_flush_does_nothing_when_no_tokens(self, tracer, mock_ingest_patch):
        tracer.flush_cost_breakdown()
        mock_ingest_patch.assert_not_called()

    def test_flush_uses_correct_model_rates(self, tracer, mock_ingest_patch):
        tracer.log_tokens("research_NVDA", _usage(1_000_000, 0))
        tracer.flush_cost_breakdown()
        _, payload = mock_ingest_patch.call_args[0]
        assert payload["cost_breakdown"]["research_NVDA"]["cost_usd"] == pytest.approx(0.80, rel=1e-3)

    def test_close_session_includes_all_accumulated_agents(self, tracer, mock_ingest_post, mock_ingest_patch):
        tracer.log_tokens("market", _usage(3000, 900))
        tracer.flush_cost_breakdown()
        tracer.log_tokens("research_NVDA", _usage(10000, 2000))
        tracer.close_session("converged")
        p = _close_payload(mock_ingest_post)
        assert "market" in p["metadata"]["cost_breakdown"]
        assert "research_NVDA" in p["metadata"]["cost_breakdown"]


# ── ingest_otel_span ──────────────────────────────────────────────────────────

class TestIngestOtelSpan:
    def test_normalizes_and_posts_otel_span(self, tracer, mock_ingest_post):
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
        p = _last_trace(mock_ingest_post)
        assert p["agent"] == "news_analyst"
        assert p["session_id"] == "test-session-id-1234"

    def test_skips_span_without_agent_name(self, tracer, mock_ingest_post):
        before = len([c for c in mock_ingest_post.call_args_list if c[0][0] == "/api/ingest/trace"])
        tracer.ingest_otel_span({"attributes": {}})
        after = len([c for c in mock_ingest_post.call_args_list if c[0][0] == "/api/ingest/trace"])
        assert after == before  # no new trace call


# ── cost helpers ───────────────────────────────────────────────────────────────

class TestCostHelpers:
    def test_estimate_cost_haiku(self):
        assert _estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(0.80, rel=1e-3)

    def test_estimate_cost_sonnet(self):
        assert _estimate_cost("claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(3.00, rel=1e-3)

    def test_estimate_cost_unknown_model_uses_sonnet_rate(self):
        assert _estimate_cost("unknown-model", 1_000_000, 0) == pytest.approx(3.00, rel=1e-3)

    def test_cache_tokens_reduce_cost_vs_uncached(self):
        cost_uncached = _estimate_cost("claude-sonnet-4-6", 10_000, 1_000)
        cost_cached   = _estimate_cost("claude-sonnet-4-6", 0, 1_000, cache_read_tokens=10_000)
        assert cost_cached < cost_uncached

    def test_agent_model_haiku_agents(self):
        for agent in ("market", "risk", "news_analyst", "research", "research_NVDA", "research_AAPL"):
            assert _agent_model(agent) == "claude-haiku-4-5-20251001"

    def test_agent_model_sonnet_agents(self):
        for agent in ("orchestrator", "learning"):
            assert _agent_model(agent) == "claude-sonnet-4-6"


# ── log_skip ───────────────────────────────────────────────────────────────────

class TestLogSkip:
    def test_design_skip_posts_correct_row(self, tracer, mock_ingest_post):
        tracer.log_skip("news", reason="no_candidates", skip_type="design")
        p = _last_trace(mock_ingest_post)
        assert p["step_type"] == "skip"
        assert p["agent"] == "news"
        assert p["outcome"] == "skipped"
        assert p["payload"]["reason"] == "no_candidates"
        assert p["payload"]["skip_type"] == "design"

    def test_error_skip_sets_amber_signal(self, tracer, mock_ingest_post):
        tracer.log_skip("risk", reason="market_skip", skip_type="error")
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["skip_type"] == "error"

    def test_default_skip_type_is_design(self, tracer, mock_ingest_post):
        tracer.log_skip("research", reason="no_candidates")
        p = _last_trace(mock_ingest_post)
        assert p["payload"]["skip_type"] == "design"

    def test_returns_span_id(self, tracer, mock_ingest_post):
        result = tracer.log_skip("news", reason="no_candidates")
        assert isinstance(result, str) and len(result) == 36
