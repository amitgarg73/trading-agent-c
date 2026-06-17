"""
ArgusExporter — OTel SpanExporter that ships spans to Argus OTLP gateway.
Used by TraceLogger to convert internal OTel spans → Argus trace rows.
"""
from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


class ArgusExporter:
    """
    Minimal OTel SpanExporter that POSTs OTLP JSON to Argus.
    Avoids importing SpanExporter base class at module level so the file
    can be imported even when opentelemetry is not installed (fails gracefully).
    """

    def __init__(self, api_key: str, endpoint: str) -> None:
        self._api_key  = api_key
        self._endpoint = endpoint.rstrip("/") + "/api/otlp/v1/traces"

    def export(self, spans: list["ReadableSpan"]) -> int:
        """Returns 0=SUCCESS, 1=FAILURE."""
        otlp_spans = []
        for span in spans:
            ctx        = span.get_span_context()
            parent_ctx = span.parent

            attrs = []
            for k, v in (span.attributes or {}).items():
                if isinstance(v, bool):    attrs.append({"key": k, "value": {"boolValue":   v}})
                elif isinstance(v, int):   attrs.append({"key": k, "value": {"intValue":    v}})
                elif isinstance(v, float): attrs.append({"key": k, "value": {"doubleValue": v}})
                else:                      attrs.append({"key": k, "value": {"stringValue": str(v)}})

            events = []
            for ev in (span.events or []):
                ev_attrs = [{"key": k, "value": {"stringValue": str(v)}} for k, v in (ev.attributes or {}).items()]
                events.append({"name": ev.name, "attributes": ev_attrs})

            otlp_spans.append({
                "spanId":            format(ctx.span_id,  "016x") if ctx else None,
                "parentSpanId":      format(parent_ctx.span_id, "016x") if parent_ctx else None,
                "traceId":           format(ctx.trace_id, "032x") if ctx else None,
                "name":              span.name,
                "startTimeUnixNano": str(span.start_time),
                "endTimeUnixNano":   str(span.end_time),
                "status":            {"code": span.status.status_code.value if span.status else 0},
                "attributes":        attrs,
                "events":            events,
            })

        payload = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": otlp_spans}]}]}).encode()
        try:
            req = urllib.request.Request(
                self._endpoint,
                data=payload,
                headers={"Content-Type": "application/json", "x-argus-key": self._api_key},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            return 0  # SUCCESS
        except Exception:
            return 1  # FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
