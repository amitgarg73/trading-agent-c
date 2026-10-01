"""argus#1444: ArgusExporter must send the input edges a step declares.

trace/logger.py declares "which spans' output this step read" as OTel Links. ArgusExporter builds its OTLP JSON by hand and used
to copy no links at all, so every declared edge was dropped before it left the machine: on 1 Oct 2026, with 93a703b running, 0 of 76
production traces carried `input_span_ids`. These tests pin the exporter, with plain fake spans and with a real OTel tracer.
"""
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from trace.otel_exporter import ArgusExporter

TRACE = 0x0123456789ABCDEF0123456789ABCDEF


def _ctx(span_id):
    return NS(span_id=span_id, trace_id=TRACE)


def _fake_span(links):
    return NS(
        get_span_context=lambda: _ctx(0xAAAAAAAAAAAAAAAA), parent=_ctx(0xBBBBBBBBBBBBBBBB),
        attributes={"argus.agent": "orchestrator"}, events=[], name="agent_message:orchestrator",
        start_time=1, end_time=2, status=NS(status_code=NS(value=0)),
        links=links,
    )


def _export(spans):
    """Run the real exporter with urlopen stubbed; return the span dicts it POSTed."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return NS()

    with patch("trace.otel_exporter.urllib.request.urlopen", fake_urlopen):
        rc = ArgusExporter(api_key="k", endpoint="http://x").export(spans)
    assert rc == 0
    return seen["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_declared_links_are_posted_in_order_with_trace_and_span_ids():
    out = _export([_fake_span([NS(context=_ctx(0x1111111111111111)), NS(context=_ctx(0x2222222222222222))])])
    assert out[0]["links"] == [
        {"traceId": "0123456789abcdef0123456789abcdef", "spanId": "1111111111111111"},
        {"traceId": "0123456789abcdef0123456789abcdef", "spanId": "2222222222222222"},
    ]


@pytest.mark.parametrize("links", [None, (), []])
def test_no_declared_links_means_no_links_key(links):
    # Provy reads an absent `links` as "this step said nothing" and an empty list as "it read nothing": never invent the second.
    assert "links" not in _export([_fake_span(links)])[0]


def test_an_invalid_link_context_is_skipped_not_fatal():
    out = _export([_fake_span([NS(context=None), NS(context=_ctx(0)), NS(context=_ctx(0x3333333333333333))])])
    assert [l["spanId"] for l in out[0]["links"]] == ["3333333333333333"]


def test_a_real_otel_span_with_links_reaches_the_wire():
    """Same path as trace/logger.py: a real tracer, a Link built from SpanContext, the real exporter."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import Link, SpanContext, TraceFlags

    provider = sdk.TracerProvider()
    posted = {}

    def fake_urlopen(req, timeout=None):
        posted.setdefault("bodies", []).append(json.loads(req.data.decode()))
        return NS()

    with patch("trace.otel_exporter.urllib.request.urlopen", fake_urlopen):
        provider.add_span_processor(SimpleSpanProcessor(ArgusExporter(api_key="k", endpoint="http://x")))
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("parent") as parent:
            tid = parent.get_span_context().trace_id
            link = Link(SpanContext(trace_id=tid, span_id=0x1111111111111111, is_remote=False, trace_flags=TraceFlags(0x01)))
            tracer.start_span("agent_message:orchestrator", links=[link]).end()

    spans = [s for b in posted["bodies"] for s in b["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    linked = [s for s in spans if s["name"] == "agent_message:orchestrator"]
    assert linked and linked[0]["links"] == [{"traceId": format(tid, "032x"), "spanId": "1111111111111111"}]
    assert "links" not in [s for s in spans if s["name"] == "parent"][0]
