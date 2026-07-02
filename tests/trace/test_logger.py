from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from trace.logger import TraceLogger, _agent_model, _estimate_cost
from tests.conftest import make_query, RecordingExporter


# ── OTel span helpers ─────────────────────────────────────────────────────────

def _span_attrs(span) -> dict:
    return dict(span.attributes or {})


def _trace_spans(recorder: RecordingExporter) -> list:
    """All spans except session root and agent-level parent spans."""
    return [
        s for s in recorder.spans
        if _span_attrs(s).get("argus.session") != "true"
        and not str(s.name).startswith("agent:")
    ]


def _last_span(recorder: RecordingExporter):
    spans = _trace_spans(recorder)
    assert spans, "No trace spans exported"
    return spans[-1]


def _last_span_attrs(recorder: RecordingExporter) -> dict:
    return _span_attrs(_last_span(recorder))


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
    def test_returns_hex_span_id(self, tracer):
        span_id = tracer.start_agent_span("market")
        assert isinstance(span_id, str) and len(span_id) == 16

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
    def test_exports_span_with_correct_step_type(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("market", "get_vix", {}, {"vix": 18.4}, latency_ms=220)
        attrs = _last_span_attrs(mock_argus_exporter)
        assert attrs["argus.step_type"] == "tool_call"
        assert attrs["argus.agent"] == "market"
        assert attrs["argus.tool_name"] == "get_vix"
        assert attrs["argus.latency_ms"] == 220
        assert attrs["argus.session_id"] == "test-session-id-1234"

    def test_tool_output_serialised_as_json(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("market", "get_vix", {}, {"vix": 18.4})
        attrs = _last_span_attrs(mock_argus_exporter)
        assert json.loads(attrs["argus.tool_output"]) == {"vix": 18.4}

    def test_returns_hex_span_id(self, tracer, mock_argus_exporter):
        span_id = tracer.log_tool_call("market", "get_vix", {}, {})
        assert isinstance(span_id, str) and len(span_id) == 16

    def test_parent_is_agent_span_when_start_agent_span_called(self, tracer, mock_argus_exporter):
        agent_span_id = tracer.start_agent_span("research")
        tracer.log_tool_call("research", "get_candidates", {}, {})
        tool_span = _last_span(mock_argus_exporter)
        parent_hex = format(tool_span.parent.span_id, "016x") if tool_span.parent else None
        assert parent_hex == agent_span_id

    def test_entity_id_included_when_provided(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("research", "get_news", {"ticker": "AAPL"}, {}, entity_id="AAPL")
        assert _last_span_attrs(mock_argus_exporter)["argus.entity_id"] == "AAPL"

    def test_entity_id_auto_derived_for_research_ticker_agent(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("research_NVDA", "get_news", {"ticker": "NVDA"}, {})
        assert _last_span_attrs(mock_argus_exporter)["argus.entity_id"] == "NVDA"

    def test_entity_id_explicit_overrides_auto_derive(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("research_NVDA", "get_news", {}, {}, entity_id="OVERRIDE")
        assert _last_span_attrs(mock_argus_exporter)["argus.entity_id"] == "OVERRIDE"

    def test_non_research_agent_no_auto_derive(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("market", "get_spy_price", {}, {})
        assert "argus.entity_id" not in _last_span_attrs(mock_argus_exporter)

    def test_sequence_increments_per_call(self, tracer, mock_argus_exporter):
        assert tracer.get_sequence() == 0
        tracer.log_tool_call("market", "get_vix", {}, {})
        assert tracer.get_sequence() == 1
        tracer.log_tool_call("market", "get_futures", {}, {})
        assert tracer.get_sequence() == 2

    def test_non_dict_tool_output_wrapped(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("market", "get_vix", {}, 18.4)
        attrs = _last_span_attrs(mock_argus_exporter)
        assert json.loads(attrs["argus.tool_output"]) == {"value": 18.4}


# ── log_agent_message ──────────────────────────────────────────────────────────

class TestLogAgentMessage:
    def test_exports_with_correct_step_type(self, tracer, mock_argus_exporter):
        tracer.log_agent_message("market", "VIX at 18 — GO", "go",
                                 tokens_input=400, tokens_output=80,
                                 model="claude-haiku-4-5-20251001")
        attrs = _last_span_attrs(mock_argus_exporter)
        assert attrs["argus.step_type"] == "agent_message"
        assert attrs["argus.agent"] == "market"
        assert attrs["argus.agent_reasoning"] == "VIX at 18 — GO"
        assert attrs["argus.outcome"] == "go"
        assert attrs["llm.token_count.input"] == 400
        assert attrs["llm.token_count.output"] == 80

    def test_returns_hex_span_id(self, tracer, mock_argus_exporter):
        span_id = tracer.log_agent_message("market", "reasoning", "go")
        assert isinstance(span_id, str) and len(span_id) == 16


# ── log_decision ───────────────────────────────────────────────────────────────

class TestLogDecision:
    def test_exports_decision_span(self, tracer, mock_argus_exporter):
        tracer.log_decision("orchestrator", "converged", detail={"trades": 3})
        attrs = _last_span_attrs(mock_argus_exporter)
        assert attrs["argus.step_type"] == "decision"
        assert attrs["argus.outcome"] == "converged"
        assert json.loads(attrs["argus.tool_output"]) == {"trades": 3}

    def test_no_entity_id_on_decision(self, tracer, mock_argus_exporter):
        tracer.log_decision("orchestrator", "skip_propagated")
        assert "argus.entity_id" not in _last_span_attrs(mock_argus_exporter)


# ── log_error ─────────────────────────────────────────────────────────────────

class TestLogError:
    def test_exports_error_span(self, tracer, mock_argus_exporter):
        tracer.log_error("research", "JSON parse failed")
        attrs = _last_span_attrs(mock_argus_exporter)
        assert attrs["argus.step_type"] == "error"
        assert attrs["argus.error"] == "JSON parse failed"
        assert attrs["argus.outcome"] == "error"

    def test_error_span_carries_otel_error_status(self, tracer, mock_argus_exporter):
        # The ingest gateway derives ag_traces.outcome from span STATUS, not attributes —
        # so an error step must set OTel ERROR status or it lands as outcome='success'.
        from opentelemetry.trace import StatusCode
        tracer.log_error("research", "JSON parse failed")
        span = _trace_spans(mock_argus_exporter)[-1]
        assert span.status.status_code == StatusCode.ERROR
        assert "JSON parse failed" in (span.status.description or "")

    def test_non_error_span_has_no_error_status(self, tracer, mock_argus_exporter):
        from opentelemetry.trace import StatusCode
        tracer.log_agent_message("research", "reasoning", "approved")
        span = _trace_spans(mock_argus_exporter)[-1]
        assert span.status.status_code != StatusCode.ERROR


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

    def test_total_steps_reflects_logged_calls(self, tracer, mock_ingest_post, mock_argus_exporter):
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
    def test_session_open_posts_session_id(self, mock_argus_exporter, mock_ingest_post, mock_ingest_patch, mock_supabase):
        t = TraceLogger("sess-open-001", session_type="premarket")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["session_id"] == "sess-open-001"
        assert p["session_type"] == "premarket"

    def test_session_type_included(self, mock_argus_exporter, mock_ingest_post, mock_ingest_patch, mock_supabase):
        t = TraceLogger("sess-open-002", session_type="intraday")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["session_type"] == "intraday"

    def test_parent_session_id_included(self, mock_argus_exporter, mock_ingest_post, mock_ingest_patch, mock_supabase):
        t = TraceLogger("sess-open-003", session_type="intraday", parent_session_id="pre-001")
        t._open_thread.join(timeout=1.0)
        p = _open_payload(mock_ingest_post)
        assert p["parent_session_id"] == "pre-001"

    def test_eod_session_type(self, mock_argus_exporter, mock_ingest_post, mock_ingest_patch, mock_supabase):
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
    def test_forwards_to_otlp_endpoint(self, tracer, mock_ingest_post):
        """TypeScript News Analyst spans are forwarded to the OTLP gateway."""
        span = {
            "traceId": "abc123",
            "spanId":  "def456",
            "parentSpanId": None,
            "name": "news.get_ticker_news",
            "startTimeUnixNano": 1000000000,
            "endTimeUnixNano":   1840000000,
            "attributes": [
                {"key": "argus.agent",      "value": {"stringValue": "news"}},
                {"key": "argus.session_id", "value": {"stringValue": "test-session-id-1234"}},
            ],
        }
        with patch("trace.logger._ingest_post_raw") as mock_raw:
            tracer.ingest_otel_span(span)
            assert mock_raw.called
            path = mock_raw.call_args[0][0]
            assert path == "/api/otlp/v1/traces"

    def test_attaches_session_id_when_missing(self, tracer):
        """Session ID is injected if the span doesn't have it."""
        span = {
            "spanId": "abc",
            "name":   "news.fetch",
            "attributes": [{"key": "argus.agent", "value": {"stringValue": "news"}}],
        }
        with patch("trace.logger._ingest_post_raw") as mock_raw:
            tracer.ingest_otel_span(span)
            raw_body = json.loads(mock_raw.call_args[0][1].decode())
            sent_span = raw_body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            session_attrs = [a for a in sent_span["attributes"] if a["key"] == "argus.session_id"]
            assert session_attrs and session_attrs[0]["value"]["stringValue"] == "test-session-id-1234"


# ── log_skip ───────────────────────────────────────────────────────────────────

class TestLogSkip:
    def test_design_skip_exports_correct_span(self, tracer, mock_argus_exporter):
        tracer.log_skip("news", reason="no_candidates", skip_type="design")
        attrs = _last_span_attrs(mock_argus_exporter)
        assert attrs["argus.step_type"] == "skip"
        assert attrs["argus.agent"] == "news"
        assert attrs["argus.outcome"] == "skipped"
        assert attrs["argus.payload.reason"] == "no_candidates"
        assert attrs["argus.payload.skip_type"] == "design"

    def test_error_skip_sets_amber_signal(self, tracer, mock_argus_exporter):
        tracer.log_skip("risk", reason="market_skip", skip_type="error")
        assert _last_span_attrs(mock_argus_exporter)["argus.payload.skip_type"] == "error"

    def test_default_skip_type_is_design(self, tracer, mock_argus_exporter):
        tracer.log_skip("research", reason="no_candidates")
        assert _last_span_attrs(mock_argus_exporter)["argus.payload.skip_type"] == "design"

    def test_returns_hex_span_id(self, tracer, mock_argus_exporter):
        result = tracer.log_skip("news", reason="no_candidates")
        assert isinstance(result, str) and len(result) == 16


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
        for agent in ("market", "risk", "news", "research", "research_NVDA", "research_AAPL"):
            assert _agent_model(agent) == "claude-haiku-4-5-20251001"

    def test_agent_model_sonnet_agents(self):
        for agent in ("orchestrator", "learner"):
            assert _agent_model(agent) == "claude-sonnet-4-6"
