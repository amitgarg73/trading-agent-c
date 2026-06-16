from __future__ import annotations

import pytest

from trace.normalizer import normalize_log_line, normalize_otel_span


# ── normalize_otel_span ────────────────────────────────────────────────────────

def _make_span(**overrides) -> dict:
    span = {
        "traceId": "abc123",
        "spanId": "def456",
        "parentSpanId": None,
        "name": "news.get_ticker_news",
        "startTimeUnixNano": 1_000_000_000,
        "endTimeUnixNano":   1_840_000_000,
        "attributes": {
            "agent.name":        "news",
            "agent.language":    "typescript",
            "session.id":        "sess-001",
            "tool.name":         "get_ticker_news",
            "tool.input.ticker": "AAPL",
            "tool.output.signal": "neutral",
            "model":             "claude-haiku-4-5-20251001",
        },
    }
    span.update(overrides)
    return span


class TestNormalizeOtelSpan:
    def test_returns_none_without_agent_name(self):
        span = _make_span(attributes={})
        assert normalize_otel_span(span, 1) is None

    def test_returns_none_without_session_id(self):
        attrs = {
            "agent.name": "news",
            "agent.language": "typescript",
        }
        assert normalize_otel_span({"attributes": attrs}, 1) is None

    def test_session_id_from_attribute(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["session_id"] == "sess-001"

    def test_session_id_arg_overrides_attribute(self):
        row = normalize_otel_span(_make_span(), 1, session_id="override-sid")
        assert row["session_id"] == "override-sid"

    def test_agent_field_populated(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["agent"] == "news"

    def test_tool_name_extracted(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["tool_name"] == "get_ticker_news"

    def test_tool_input_extracted_from_flat_attributes(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["payload"]["tool_input"] == {"ticker": "AAPL"}

    def test_tool_output_extracted_from_flat_attributes(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["payload"]["tool_output"] == {"signal": "neutral"}

    def test_step_type_tool_call_when_tool_name_present(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["step_type"] == "tool_call"

    def test_step_type_decision_when_span_name_ends_with_session(self):
        span = _make_span(name="news.session")
        attrs = span["attributes"].copy()
        del attrs["tool.name"]
        span["attributes"] = attrs
        row = normalize_otel_span(span, 1)
        assert row["step_type"] == "decision"

    def test_step_type_agent_message_when_no_tool(self):
        span = _make_span(name="news.analyze")
        attrs = span["attributes"].copy()
        del attrs["tool.name"]
        span["attributes"] = attrs
        row = normalize_otel_span(span, 1)
        assert row["step_type"] == "agent_message"

    def test_latency_calculated_from_nano_timestamps(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["latency_ms"] == 840

    def test_latency_zero_when_end_not_after_start(self):
        span = _make_span(startTimeUnixNano=1_000_000_000, endTimeUnixNano=500_000_000)
        row = normalize_otel_span(span, 1)
        assert row["latency_ms"] == 0

    def test_entity_id_from_ticker_input(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["payload"]["entity_id"] == "AAPL"

    def test_entity_id_none_when_no_ticker(self):
        span = _make_span()
        del span["attributes"]["tool.input.ticker"]
        row = normalize_otel_span(span, 1)
        assert row["payload"]["entity_id"] is None

    def test_span_id_from_span(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["payload"]["span_id"] == "def456"

    def test_span_id_generated_when_missing(self):
        span = _make_span()
        del span["spanId"]
        row = normalize_otel_span(span, 1)
        assert len(row["payload"]["span_id"]) == 36

    def test_parent_span_id_preserved(self):
        span = _make_span(parentSpanId="parent-xyz")
        row = normalize_otel_span(span, 1)
        assert row["payload"]["parent_span_id"] == "parent-xyz"

    def test_sequence_stored(self):
        row = normalize_otel_span(_make_span(), 7)
        assert row["payload"]["sequence"] == 7

    def test_model_extracted(self):
        row = normalize_otel_span(_make_span(), 1)
        assert row["payload"]["model"] == "claude-haiku-4-5-20251001"

    def test_tool_input_none_when_no_tool_input_attrs(self):
        span = _make_span()
        attrs = {k: v for k, v in span["attributes"].items() if not k.startswith("tool.input.")}
        span["attributes"] = attrs
        row = normalize_otel_span(span, 1)
        assert row["payload"]["tool_input"] is None

    def test_tool_output_none_when_no_tool_output_attrs(self):
        span = _make_span()
        attrs = {k: v for k, v in span["attributes"].items() if not k.startswith("tool.output.")}
        span["attributes"] = attrs
        row = normalize_otel_span(span, 1)
        assert row["payload"]["tool_output"] is None

    def test_multiple_tool_input_fields(self):
        span = _make_span()
        span["attributes"]["tool.input.limit"] = 10
        row = normalize_otel_span(span, 1)
        assert row["payload"]["tool_input"]["ticker"] == "AAPL"
        assert row["payload"]["tool_input"]["limit"] == 10

    def test_required_keys_present(self):
        row = normalize_otel_span(_make_span(), 1)
        for key in ("session_id", "agent", "step_type", "created_at"):
            assert key in row
        for key in ("span_id", "sequence", "date"):
            assert key in row["payload"]

    def test_agent_reasoning_extracted_from_attribute(self):
        span = _make_span(name="news.reasoning")
        del span["attributes"]["tool.name"]
        span["attributes"]["agent.reasoning"] = "AAPL: bullish · NVDA: neutral"
        row = normalize_otel_span(span, 1)
        assert row["step_type"] == "agent_message"
        assert row["payload"]["agent_reasoning"] == "AAPL: bullish · NVDA: neutral"

    def test_agent_reasoning_absent_when_not_in_attributes(self):
        row = normalize_otel_span(_make_span(), 1)
        assert "agent_reasoning" not in row["payload"]


# ── normalize_log_line ────────────────────────────────────────────────────────

class TestNormalizeLogLine:
    _SAMPLE = (
        '2026-05-27T20:15:04Z [learning_agent] session=sess-abc '
        'event=tool_call tool=read_today_trades'
    )

    def test_returns_none_for_non_matching_line(self):
        assert normalize_log_line("not a valid log line", 1) is None

    def test_returns_none_for_empty_string(self):
        assert normalize_log_line("", 1) is None

    def test_agent_extracted(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["agent"] == "learning_agent"

    def test_session_id_extracted(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["session_id"] == "sess-abc"

    def test_step_type_tool_call_for_tool_call_event(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["step_type"] == "tool_call"

    def test_step_type_agent_message_for_other_events(self):
        line = '2026-05-27T20:15:04Z [learning_agent] session=sess-abc event=adjustment action=hold'
        row = normalize_log_line(line, 1)
        assert row["step_type"] == "agent_message"

    def test_tool_name_extracted(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["tool_name"] == "read_today_trades"

    def test_outcome_set_from_action_on_adjustment(self):
        line = '2026-05-27T20:15:04Z [learning_agent] session=sess-abc event=adjustment action=hold'
        row = normalize_log_line(line, 1)
        assert row["outcome"] == "hold"

    def test_outcome_none_for_non_adjustment_event(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["outcome"] is None

    def test_tool_output_populated_from_finding(self):
        line = (
            '2026-05-27T20:15:04Z [learning_agent] session=sess-abc '
            'event=summary finding="momentum works" sample_size=5 confidence=0.8'
        )
        row = normalize_log_line(line, 1)
        assert row["payload"]["tool_output"]["finding"] == "momentum works"
        assert row["payload"]["tool_output"]["sample_size"] == "5"

    def test_tool_output_none_when_no_finding(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["payload"]["tool_output"] is None

    def test_sequence_stored(self):
        row = normalize_log_line(self._SAMPLE, 5)
        assert row["payload"]["sequence"] == 5

    def test_entity_from_kv(self):
        line = '2026-05-27T20:15:04Z [learning_agent] session=sess-abc event=tool_call entity=AAPL'
        row = normalize_log_line(line, 1)
        assert row["payload"]["entity_id"] == "AAPL"

    def test_entity_none_when_missing(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["payload"]["entity_id"] is None

    def test_required_keys_present(self):
        row = normalize_log_line(self._SAMPLE, 1)
        for key in ("session_id", "agent", "step_type", "created_at"):
            assert key in row
        for key in ("span_id", "sequence", "date"):
            assert key in row["payload"]

    def test_created_at_matches_timestamp_in_line(self):
        row = normalize_log_line(self._SAMPLE, 1)
        assert row["created_at"] == "2026-05-27T20:15:04Z"
