# Trace Formats — Trading Agent C

Three agents emit traces in three different formats. The AI Agent Reliability product
ingests all three and normalizes them to a unified view. This doc defines each format,
the normalization spec, and how session correlation works across language boundaries.

---

## Why Three Formats

This is deliberate. Strategy C is the live proof-of-concept for the Reliability product.
A real enterprise AI system will have heterogeneous agents — some in Python, some in
TypeScript or Go, some emitting OTel spans, some emitting structured logs. The Reliability
product must handle this without requiring agents to change their instrumentation.

| Agent | Language | Trace format | Why |
|---|---|---|---|
| Market Agent | Python | Custom JSON (Format 1) | Direct c_traces insert from Python |
| Research Agent | Python | Custom JSON (Format 1) | Same |
| Risk Agent | Python | Custom JSON (Format 1) | Same |
| Orchestrator | Python | Custom JSON (Format 1) | Same |
| News Analyst | TypeScript | OTel-compatible JSON spans (Format 2) | Demonstrates TS SDK + OTel |
| Learning Agent | Python | Structured log lines + summary (Format 3) | Analysis agent writes narrative logs |

---

## Format 1: Custom JSON (Python Agents)

Written directly to c_traces by `trace/logger.py`. No intermediate format.

### Tool Call Row

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "span_id": "7f3b8a2c-4d1e-4f9a-b3c5-8e2d1f0a6b4c",
  "parent_span_id": "2a9c5e7f-1b3d-4e6a-8f2b-0c4d6e8a1b3e",
  "entity_id": "AAPL",
  "date": "2026-05-27",
  "sequence": 7,
  "agent": "research",
  "step_type": "tool_call",
  "tool_name": "get_intraday_signals",
  "tool_input": {"ticker": "AAPL"},
  "tool_output": {
    "above_vwap": true,
    "rs_vs_spy": 1.4,
    "today_pct": 0.82
  },
  "agent_reasoning": null,
  "outcome": null,
  "tokens_input": 0,
  "tokens_output": 0,
  "latency_ms": 340,
  "model": "claude-sonnet-4-6",
  "error": null,
  "created_at": "2026-05-27T13:47:22.840Z"
}
```

### Agent Decision Row

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "span_id": "9c1a3e5b-7d2f-4b8e-a6c4-0e2b4d6f8a0c",
  "parent_span_id": "2a9c5e7f-1b3d-4e6a-8f2b-0c4d6e8a1b3e",
  "entity_id": "AAPL",
  "date": "2026-05-27",
  "sequence": 9,
  "agent": "research",
  "step_type": "agent_message",
  "tool_name": null,
  "tool_input": null,
  "tool_output": null,
  "agent_reasoning": "AAPL is above VWAP with RS 1.4x vs SPY. Score 7. Entry at bid 187.40.",
  "outcome": "proposed",
  "tokens_input": 3200,
  "tokens_output": 480,
  "latency_ms": 1840,
  "model": "claude-sonnet-4-6",
  "error": null,
  "created_at": "2026-05-27T13:47:24.680Z"
}
```

### Logger API (trace/logger.py)

```python
class TraceLogger:
    def __init__(self, session_id: str, db: SupabaseClient): ...

    def log_tool_call(
        self, agent: str, block: ToolUseBlock, result: dict,
        entity_id: str | None = None, latency_ms: int = 0
    ) -> str:
        """Write tool_call row. Returns span_id."""

    def log_agent_message(
        self, agent: str, reasoning: str, outcome: str,
        entity_id: str | None = None, tokens: Usage = None
    ) -> str:
        """Write agent_message row. Returns span_id."""

    def log_decision(
        self, agent: str, outcome: str, detail: dict | None = None
    ) -> str:
        """Write decision row (no tool, no entity). Returns span_id."""

    def log_tokens(self, agent: str, usage: Usage) -> None:
        """Accumulate tokens — written to c_sessions at close."""

    def start_agent_span(self, agent: str) -> str:
        """Create parent span for an agent invocation. Returns span_id."""

    def close_session(self, terminal_reason: str, trades_count: int) -> None:
        """Write c_sessions row."""
```

---

## Format 2: OTel-Compatible JSON Spans (TypeScript Agents)

Emitted to stdout by TypeScript agents with the prefix `OTEL_SPAN: `.
The Python session driver reads these lines and passes them to `trace/normalizer.py`.

### Tool Call Span

```json
{
  "traceId": "a3f4b8d2e1c09f3a4b7d2e1c0a3f4b8d",
  "spanId": "b7d2e1c0a3f4",
  "parentSpanId": "f9a2b4c6d8e0",
  "name": "news_analyst.get_ticker_news",
  "kind": 3,
  "startTimeUnixNano": 1748383504000000000,
  "endTimeUnixNano": 1748383504840000000,
  "attributes": {
    "agent.name": "news_analyst",
    "agent.language": "typescript",
    "tool.name": "get_ticker_news",
    "tool.input.ticker": "AAPL",
    "tool.output.count": 2,
    "tool.output.signal": "neutral",
    "tool.output.blackout": false,
    "session.id": "550e8400-e29b-41d4-a716-446655440000",
    "model": "claude-haiku-4-5-20251001"
  },
  "status": {"code": 1}
}
```

### Session Summary Span

```json
{
  "traceId": "a3f4b8d2e1c09f3a4b7d2e1c0a3f4b8d",
  "spanId": "f9a2b4c6d8e0",
  "parentSpanId": null,
  "name": "news_analyst.session",
  "kind": 1,
  "startTimeUnixNano": 1748383502000000000,
  "endTimeUnixNano": 1748383505840000000,
  "attributes": {
    "agent.name": "news_analyst",
    "agent.language": "typescript",
    "session.id": "550e8400-e29b-41d4-a716-446655440000",
    "tickers.count": 5,
    "tickers.analyzed": 5,
    "blackout.count": 1,
    "tokens.input": 840,
    "tokens.output": 220,
    "model": "claude-haiku-4-5-20251001"
  },
  "status": {"code": 1}
}
```

### Required OTel Attributes (normalizer will reject spans missing these)

| Attribute | Required | Description |
|---|---|---|
| `agent.name` | yes | Which agent emitted this span |
| `agent.language` | yes | "typescript" — used to route to OTel normalizer |
| `session.id` | yes | UUID matching the Python session_id |
| `tool.name` | if tool call | Name of the tool |
| `model` | yes | Claude model used |

---

## Format 3: Structured Log Lines (Learning Agent)

The Learning Agent emits structured log lines to stdout during execution,
then returns a JSON summary on `end_turn`. The EOD session driver reads both.

### Log Line Format

```
{ISO8601_TIMESTAMP} [learning_agent] session={session_id} {key=value ...}
```

Examples:

```
2026-05-27T20:15:04Z [learning_agent] session=550e8400 event=tool_call tool=read_today_trades trades_found=4
2026-05-27T20:15:05Z [learning_agent] session=550e8400 event=pattern dimension=entry_quality finding="bid_entries_win_rate=0.80" sample_size=4 confidence=medium
2026-05-27T20:15:06Z [learning_agent] session=550e8400 event=adjustment param=strategy_min_score old=5 new=6 reason="score>=6 trades won 3/3" cooldown_until=2026-06-01
2026-05-27T20:15:07Z [learning_agent] session=550e8400 event=observation dimension=sector entity=Technology finding="100pct win rate" sample_size=3 action=none note="already near max_bound for sector_allocation"
2026-05-27T20:15:08Z [learning_agent] session=550e8400 event=complete learnings_written=2 params_adjusted=1 goal_recommended=false
```

### JSON Summary (returned by Claude on end_turn, written to c_traces)

```json
{
  "session_date": "2026-05-27",
  "trades_analyzed": 4,
  "win_rate": 0.75,
  "total_pnl": 284.0,
  "learnings_written": 2,
  "params_adjusted": 1,
  "goal_recommended": false,
  "top_finding": "Tickers with score >= 6 won 3/3 today. Strategy_min_score raised to 6.",
  "context_for_tomorrow": "Technology entries went 3/3 today with 100% win rate. CRWD stopped at entry — review entry pricing for names with wide spread. Score 6+ outperformed score 5 in today's session."
}
```

---

## Normalization Spec (trace/normalizer.py)

Converts Format 2 and Format 3 to c_traces rows.

### OTel → c_traces

```python
def normalize_otel_span(span: dict, sequence: int) -> dict:
    """Convert an OTel span to a c_traces row."""
    attrs = span.get("attributes", {})
    session_id = attrs.get("session.id")
    tool_name = attrs.get("tool.name")
    start_ns = span.get("startTimeUnixNano", 0)
    end_ns = span.get("endTimeUnixNano", 0)
    latency_ms = (end_ns - start_ns) // 1_000_000

    # Determine step_type from span name
    step_type = "tool_call" if tool_name else "agent_message"
    if span["name"].endswith(".session"):
        step_type = "decision"

    # Build tool_input / tool_output from flattened attributes
    tool_input = {}
    tool_output = {}
    for k, v in attrs.items():
        if k.startswith("tool.input."):
            tool_input[k[len("tool.input."):]] = v
        elif k.startswith("tool.output."):
            tool_output[k[len("tool.output."):]] = v

    return {
        "session_id": session_id,
        "span_id": span.get("spanId"),
        "parent_span_id": span.get("parentSpanId"),
        "entity_id": attrs.get("tool.input.ticker"),  # present on per-ticker tool calls
        "date": datetime.utcnow().date().isoformat(),
        "sequence": sequence,
        "agent": attrs.get("agent.name"),
        "step_type": step_type,
        "tool_name": tool_name,
        "tool_input": tool_input or None,
        "tool_output": tool_output or None,
        "agent_reasoning": None,
        "outcome": attrs.get("outcome"),
        "tokens_input": attrs.get("tokens.input", 0),
        "tokens_output": attrs.get("tokens.output", 0),
        "latency_ms": latency_ms,
        "model": attrs.get("model"),
        "error": attrs.get("error"),
        "created_at": datetime.utcnow().isoformat(),
    }
```

### Structured Log → c_traces

```python
import re

LOG_PATTERN = re.compile(
    r'^(?P<ts>\S+)\s+\[learning_agent\]\s+session=(?P<sid>\S+)\s+(?P<kv>.*)$'
)

def normalize_log_line(line: str, sequence: int) -> dict | None:
    """Convert a structured log line to a c_traces row. Returns None if not parseable."""
    m = LOG_PATTERN.match(line)
    if not m:
        return None
    kv_str = m.group("kv")
    kv = dict(re.findall(r'(\w+)=("(?:[^"]+)"|[\S]+)', kv_str))
    kv = {k: v.strip('"') for k, v in kv.items()}

    event = kv.get("event", "unknown")
    step_type = "tool_call" if event == "tool_call" else "agent_message"
    outcome = kv.get("action") if event == "adjustment" else None

    return {
        "session_id": m.group("sid"),
        "span_id": str(uuid4()),
        "parent_span_id": None,
        "entity_id": kv.get("entity"),
        "date": datetime.utcnow().date().isoformat(),
        "sequence": sequence,
        "agent": "learning",
        "step_type": step_type,
        "tool_name": kv.get("tool"),
        "tool_input": None,
        "tool_output": {"finding": kv.get("finding"), "sample_size": kv.get("sample_size")},
        "agent_reasoning": kv.get("finding"),
        "outcome": outcome,
        "tokens_input": 0,
        "tokens_output": 0,
        "latency_ms": 0,
        "model": "claude-sonnet-4-6",
        "error": None,
        "created_at": m.group("ts"),
    }
```

---

## Session Correlation Across Languages

The Python `session_id` is passed to TypeScript agents as part of the input JSON.
TypeScript agents embed it in every OTel span as `attributes["session.id"]`.
The normalizer reads `session.id` and sets `c_traces.session_id` accordingly.

This means the Reliability product can query:
```sql
SELECT agent, step_type, tool_name, latency_ms, outcome
FROM c_traces
WHERE session_id = 'X'
ORDER BY sequence;
```

...and get a unified timeline across Python agents (Format 1) and TypeScript agents
(Format 2, normalized), all in order, with latencies, under the same session.

### Trace ID vs Session ID

OTel uses a `traceId` (32-char hex). The Python system uses `session_id` (UUID).
They are different identifiers. The mapping is stored in c_traces via `session.id` attribute.

The OTel `spanId` (16-char hex) maps to `c_traces.span_id`. The normalizer converts hex
to a consistent format — or stores the original hex string directly (c_traces.span_id is TEXT).

---

## Reliability Product Input Contract

The Reliability product reads from c_traces and c_sessions.
It expects:
1. All rows for a session share the same `session_id`
2. `sequence` is monotonically increasing within a session
3. `outcome` is from the controlled vocabulary (no free-text)
4. `agent` is one of: `market`, `research`, `risk`, `orchestrator`, `learning`, `news_analyst`
5. `step_type` is one of: `tool_call`, `agent_message`, `decision`, `error`
6. `latency_ms` is non-null for all tool_call rows

Any row violating these constraints is logged as an anomaly in the Reliability dashboard.
