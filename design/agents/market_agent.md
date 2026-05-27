# Market Agent — Design Doc

**File:** `agents/market_agent.py`  
**Model:** `claude-haiku-4-5-20251001`  
**Role:** Macro market analyst. Assesses daily conditions and sets a position ceiling before any stock research begins.  
**Runs:** Once per session, before Research Agent.

---

## Why Haiku

Market Agent performs data aggregation, not multi-step reasoning. Four tool calls, four data points, one structured JSON output. The decision rules are explicit and enumerated — Claude does not need to reason about ambiguity, it applies a decision matrix. Haiku handles this correctly at ~30% the cost of Sonnet.

---

## System Prompt (production)

```
You are a macro market analyst for an intraday trading system. Your sole job is
to assess today's pre-market conditions and output a position ceiling and bias
for the session.

You have exactly 4 tools. Call all 4 before forming any view. Do not stop after
fewer. Call order: get_vix, get_futures, get_fear_greed, get_sector_rotation.

After all 4 tool calls, return a single JSON object. No commentary before or
after the JSON.

DECISION RULES (apply in order, stop at first match):

1. SKIP if futures.avg_change_pct < -1.5
   Reason: strong pre-market selloff invalidates intraday long setups.
   Set max_positions = 0.

2. SKIP if vix.value > 45
   Reason: extreme volatility; bracket orders cannot contain risk reliably.
   Set max_positions = 0.

3. CAUTION if vix.value > 20 AND futures.avg_change_pct < 0
   Reason: elevated volatility confirmed by bearish overnight.

4. CAUTION if fear_greed.value < 25
   Reason: extreme fear — momentum setups fail more often on panic-sentiment days.

5. GO otherwise.

MAX_POSITIONS SCALING (apply after decision):
  vix < 20         → 15
  vix 20 to 24.99  → 10
  vix 25 to 29.99  →  5
  vix 30 to 44.99  →  3
  vix ≥ 45         →  0 (SKIP)

On CAUTION: multiply the VIX-scaled max by 0.6 and round down. Minimum 1 if
decision is CAUTION and scaled value rounds to 0.

BIAS:
  futures.bias = "BULLISH" AND sector rotation shows technology or consumer
  discretionary leading → BULLISH
  futures.bias = "BEARISH" OR top sector down more than 0.5% → BEARISH
  Otherwise → NEUTRAL

OUTPUT SCHEMA (return exactly this, no extra keys):
{
  "decision": "GO | CAUTION | SKIP",
  "max_positions": <int 0–15>,
  "bias": "BULLISH | BEARISH | NEUTRAL",
  "vix": <float>,
  "fear_greed": <int>,
  "avg_futures_pct": <float>,
  "leading_sector": <str or null>,
  "skip_reason": <str or null>,
  "summary": <str, 2–3 sentences, what the data shows and why>
}

If a tool returns an error field, use the conservative value:
  get_vix error → assume vix = 25 (CRISIS level, CAUTION)
  get_futures error → assume avg_change_pct = 0.0 (NEUTRAL)
  get_fear_greed error → assume value = 40 (Fear, no CAUTION trigger)
  get_sector_rotation error → assume leading_sector = null
Do not change the decision solely because a tool errored if other data
supports a different conclusion. Document the error in the summary field.
```

---

## Tools

### get_vix
**Purpose:** Check whether market volatility is within operating range.  
**Called:** First, always.  
**Input:** `{}`  
**Output:** `{ "value": float, "level": "LOW|ELEVATED|HIGH|CRISIS|EXTREME" }`  
**Failure behavior:** Assume `value = 25` (conservative CRISIS default). Log in summary.

```
Level thresholds:
  LOW      < 15   — calm, normal operation
  ELEVATED 15–20  — watchful, no constraint change
  HIGH     20–25  — CAUTION trigger (with bearish futures)
  CRISIS   25–30  — CAUTION or SKIP depending on futures
  EXTREME  > 30   — hard SKIP above 45
```

### get_futures
**Purpose:** Pre-market directional bias from index futures.  
**Called:** Second, always.  
**Input:** `{}`  
**Output:**
```json
{
  "S&P500":  { "change_pct": float },
  "Nasdaq":  { "change_pct": float },
  "Dow":     { "change_pct": float },
  "avg_change_pct": float,
  "bias": "BULLISH|BEARISH|NEUTRAL"
}
```
**Failure behavior:** Assume `avg_change_pct = 0.0`, `bias = "NEUTRAL"`. Does not trigger SKIP on its own.

### get_fear_greed
**Purpose:** Sentiment check. Extreme fear days have lower momentum follow-through.  
**Called:** Third, always.  
**Input:** `{}`  
**Output:** `{ "value": int, "classification": str }`  
**Failure behavior:** Assume `value = 40` (Fear zone). No CAUTION trigger from F&G alone.

```
Classification bands:
  0–24  Extreme Fear
  25–44 Fear
  45–55 Neutral
  56–75 Greed
  76–100 Extreme Greed
```

### get_sector_rotation
**Purpose:** Identifies which sectors are leading or lagging today. Feeds bias calculation and Research Agent context.  
**Called:** Fourth, always.  
**Input:** `{}`  
**Output:** `[ { "etf": "XLK", "change_pct": float }, ... ]` — sorted best to worst, all 11 ETFs.  
**Failure behavior:** Return `leading_sector = null` in output. Does not affect decision.

---

## Input Contract

None. Market Agent is the first agent in the session. It receives no user message from the Orchestrator — only the system prompt and its tool set.

Orchestrator call:
```python
response = anthropic.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    system=MARKET_AGENT_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": "Assess today's market conditions."}],
    tools=MARKET_AGENT_TOOLS,
)
```

---

## Output Contract

```json
{
  "decision": "GO",
  "max_positions": 10,
  "bias": "BULLISH",
  "vix": 18.4,
  "fear_greed": 52,
  "avg_futures_pct": 0.38,
  "leading_sector": "XLK",
  "skip_reason": null,
  "summary": "VIX at 18.4 (LOW), futures +0.4% led by Nasdaq, Fear & Greed 52 (Neutral). Technology sector leading with XLK up 0.6%. Calm open with moderate bullish bias — standard position ceiling applies."
}
```

**Validation before passing to Research Agent:**
- `decision` must be one of "GO", "CAUTION", "SKIP"
- `max_positions` must be an int 0–15
- If `decision == "SKIP"`, orchestrator ends session immediately; Research Agent is not called
- If JSON parse fails, orchestrator logs error and ends session with `terminal_reason = "parse_error"`

---

## Execution Flow

```
1. Orchestrator calls Market Agent
2. Market Agent calls get_vix()
3. Market Agent calls get_futures()
4. Market Agent calls get_fear_greed()
5. Market Agent calls get_sector_rotation()
6. Market Agent applies decision rules → constructs market_report JSON
7. Market Agent returns JSON as final text response
8. Orchestrator parses response
   → If decision == SKIP: session ends, trades = [], terminal_reason = "skip_propagated"
   → If decision == GO or CAUTION: pass market_report to Research Agent
```

Total tool calls: exactly 4. Cap: 4. No partial execution allowed.

---

## Constraint Enforcement

| Constraint | Enforcement |
|---|---|
| All 4 tools required | Prompt states "Call all 4 before forming any view. Do not stop after fewer." |
| Call order | Prompt specifies order (vix, futures, fear_greed, sector_rotation) |
| No extra tool calls | Tool registration: only these 4 tools are passed. Claude cannot call tools not in the registered set. |
| 1 round only | Orchestrator does not loop Market Agent. Single call, parse output, done. |

---

## Error Handling

| Scenario | Behavior |
|---|---|
| One tool returns `{ "error": "..." }` | Agent uses conservative fallback value (see system prompt), documents in summary, continues |
| Two or more tools fail | Agent uses all fallbacks; if result would be SKIP, returns SKIP; otherwise CAUTION |
| Agent returns invalid JSON | Orchestrator catches `json.JSONDecodeError`, logs error, ends session with `terminal_reason = "market_parse_error"`, returns `trades: []` |
| Agent times out (>30s) | Orchestrator has a 30-second timeout on this API call; on timeout, ends session with `terminal_reason = "market_timeout"` |
| Missing required field in output | Orchestrator validates schema; missing `decision` → session ends with `terminal_reason = "market_schema_error"` |

---

## Trace Entries Generated

Market Agent produces 5 trace rows per normal session:

| sequence | step_type | tool_name | outcome | entity_id |
|---|---|---|---|---|
| 1 | tool_call | get_vix | (null — tool call rows record input/output, not outcome) | null |
| 2 | tool_call | get_futures | — | null |
| 3 | tool_call | get_fear_greed | — | null |
| 4 | tool_call | get_sector_rotation | — | null |
| 5 | agent_message | null | `go` / `caution` / `skip` | null |

All 5 rows share the same `parent_span_id` (the market_agent span). The market_agent span is a child of the session root span.

On tool error, an additional `error` step_type row is inserted at that position.

---

## Implementation Notes

```python
# agents/market_agent.py

MARKET_AGENT_TOOLS = [
    build_tool("get_vix",            get_vix_impl),
    build_tool("get_futures",        get_futures_impl),
    build_tool("get_fear_greed",     get_fear_greed_impl),
    build_tool("get_sector_rotation", get_sector_rotation_impl),
]

def run_market_agent(session_id: str, tracer: TraceLogger) -> dict:
    """
    Runs Market Agent tool loop until stop_reason == "end_turn".
    Returns parsed market_report dict.
    Raises MarketAgentError on parse failure or timeout.
    """
    messages = [{"role": "user", "content": "Assess today's market conditions."}]
    tool_call_count = 0

    while True:
        response = anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=MARKET_AGENT_SYSTEM_PROMPT,
            messages=messages,
            tools=MARKET_AGENT_TOOLS,
        )
        tracer.log_tokens(session_id, "market", response.usage)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_call_count += 1
                    result = dispatch_tool(block.name, block.input)
                    tracer.log_tool_call(session_id, "market", block, result)
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": json.dumps(result)})
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if b.type == "text")
            market_report = json.loads(text)
            tracer.log_agent_message(session_id, "market", market_report)
            return market_report

        else:
            raise MarketAgentError(f"unexpected stop_reason: {response.stop_reason}")
```

The tool loop runs until `stop_reason == "end_turn"`. Market Agent always terminates after 4 tool calls and 1 final text response because the system prompt instructs it to return JSON after all tools are called. There is no need for a loop iteration counter here — if the agent loops unexpectedly, the session-level 40-call cap will terminate it.
