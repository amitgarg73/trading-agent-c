from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4


# ── Format 2: OTel span → c_traces row ────────────────────────────────────────

def normalize_otel_span(span: dict, sequence: int, session_id: Optional[str] = None) -> Optional[dict]:
    """
    Convert an OTel-compatible span (emitted by TypeScript agents) to a c_traces row.
    Returns None if the span is missing required fields.

    Required attributes: agent.name, agent.language, session.id (or session_id arg), model
    """
    attrs: dict[str, Any] = span.get("attributes", {})

    agent = attrs.get("agent.name")
    if not agent:
        return None

    # session_id: prefer explicit arg, then attribute
    sid = session_id or attrs.get("session.id")
    if not sid:
        return None

    tool_name = attrs.get("tool.name")
    span_name = span.get("name", "")

    # Determine step_type from span name
    if span_name.endswith(".session"):
        step_type = "decision"
    elif tool_name:
        step_type = "tool_call"
    else:
        step_type = "agent_message"

    # Extract tool_input / tool_output from flattened attributes
    tool_input: dict[str, Any] = {}
    tool_output: dict[str, Any] = {}
    for k, v in attrs.items():
        if k.startswith("tool.input."):
            tool_input[k[len("tool.input."):]] = v
        elif k.startswith("tool.output."):
            tool_output[k[len("tool.output."):]] = v

    # Latency from OTel nano-timestamps
    start_ns = span.get("startTimeUnixNano", 0)
    end_ns   = span.get("endTimeUnixNano",   0)
    latency_ms = int((end_ns - start_ns) / 1_000_000) if end_ns > start_ns else 0

    # entity_id from tool input ticker if present
    entity_id = tool_input.get("ticker") or attrs.get("tool.input.ticker")

    return {
        "session_id":      sid,
        "span_id":         span.get("spanId", str(uuid4())),
        "parent_span_id":  span.get("parentSpanId"),
        "entity_id":       entity_id,
        "date":            datetime.utcnow().date().isoformat(),
        "sequence":        sequence,
        "agent":           agent,
        "step_type":       step_type,
        "tool_name":       tool_name,
        "tool_input":      tool_input or None,
        "tool_output":     tool_output or None,
        "agent_reasoning": None,
        "outcome":         attrs.get("outcome"),
        "tokens_input":    int(attrs.get("tokens.input", 0)),
        "tokens_output":   int(attrs.get("tokens.output", 0)),
        "latency_ms":      latency_ms,
        "model":           attrs.get("model"),
        "error":           attrs.get("error"),
        "created_at":      datetime.utcnow().isoformat(),
    }


# ── Format 3: Structured log line → c_traces row ──────────────────────────────

# Pattern: 2026-05-27T20:15:04Z [learning_agent] session=uuid key=value ...
_LOG_PATTERN = re.compile(
    r"^(?P<ts>\S+)\s+\[(?P<agent>[^\]]+)\]\s+session=(?P<sid>\S+)\s+(?P<kv>.*)$"
)
# Matches key="quoted value" or key=unquoted_value
_KV_PATTERN = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s]+)')


def normalize_log_line(line: str, sequence: int) -> Optional[dict]:
    """
    Convert a structured log line from the Learning Agent to a c_traces row.
    Returns None if the line does not match the expected format.
    """
    m = _LOG_PATTERN.match(line.strip())
    if not m:
        return None

    agent = m.group("agent")
    sid   = m.group("sid")
    ts    = m.group("ts")
    kv_str = m.group("kv")

    kv: dict[str, str] = {}
    for k, v in _KV_PATTERN.findall(kv_str):
        kv[k] = v.strip('"')

    event = kv.get("event", "unknown")
    step_type = "tool_call" if event == "tool_call" else "agent_message"
    outcome   = kv.get("action") if event == "adjustment" else None

    tool_output: Optional[dict] = None
    if kv.get("finding") or kv.get("sample_size"):
        tool_output = {
            k: kv[k] for k in ("finding", "sample_size", "confidence", "param", "old", "new")
            if k in kv
        }

    return {
        "session_id":      sid,
        "span_id":         str(uuid4()),
        "parent_span_id":  None,
        "entity_id":       kv.get("entity"),
        "date":            datetime.utcnow().date().isoformat(),
        "sequence":        sequence,
        "agent":           agent,
        "step_type":       step_type,
        "tool_name":       kv.get("tool"),
        "tool_input":      None,
        "tool_output":     tool_output,
        "agent_reasoning": kv.get("finding"),
        "outcome":         outcome,
        "tokens_input":    0,
        "tokens_output":   0,
        "latency_ms":      0,
        "model":           "claude-sonnet-4-6",
        "error":           None,
        "created_at":      ts,
    }
