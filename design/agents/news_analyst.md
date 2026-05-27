# News Analyst — Design Doc

**File:** `agents/ts/news_analyst.ts`
**Language:** TypeScript (Node.js)
**SDK:** `@anthropic-ai/sdk`
**Model:** `claude-haiku-4-5-20251001`
**Role:** Headline sentiment classifier. Given a list of tickers from Research Agent's initial screen, fetches recent news for each and classifies the signal as positive/negative/neutral/null. Does not make trade decisions. Adds news context and blackout flags.
**Runs:** Once per premarket session, after Research Agent produces its initial candidate list. Called by the Orchestrator session driver as a subprocess or HTTP call.
**Trace format:** OTel-compatible JSON spans (see trace-formats.md)

---

## Why TypeScript

News Analyst is the simplest agent in the system. Its logic is stateless, the tool set is minimal, and it needs to run fast. It is intentionally chosen as the TypeScript agent to prove that the AI Agent Reliability product can ingest OTel spans from a non-Python agent alongside Python agents, without any changes to the c_traces schema or the Reliability dashboard. The trading logic does not depend on which language it is written in.

---

## System Prompt (production)

```
You are a financial news sentiment classifier.

You receive a list of stock tickers. For each ticker, call get_ticker_news once.
Do not skip any ticker. Do not call get_ticker_news more than once per ticker.

After all news calls, classify each ticker's signal:

  positive:  clear bullish catalyst — earnings beat, analyst upgrade, contract win,
             FDA approval, partnership announcement with revenue impact
  negative:  clear bearish catalyst — earnings miss, analyst downgrade, lawsuit,
             product recall, regulatory action, executive departure (CEO/CFO)
  neutral:   no relevant news today, or mixed signals with no dominant direction
  null:      news API returned empty — treat as neutral for scoring but flag it

Also set blackout: true if any headline mentions earnings today or tomorrow,
or if the news contains "Q[1-4] results", "reports after close", "reports before open",
"earnings call scheduled". Blackout overrides all other classification.

Return JSON only. No commentary before or after.
```

---

## Tools

### get_ticker_news
**Purpose:** Fetch recent headlines for a single ticker via yfinance.
**Language:** TypeScript — calls a local Python helper or uses `yfinance` bindings.
**Input:** `{"ticker": "AAPL"}`
**Output:**
```json
{
  "ticker": "AAPL",
  "headlines": [
    {
      "title": "Apple unveils redesigned MacBook Pro with M4 chip",
      "published": "2026-05-27T10:30:00Z",
      "publisher": "Reuters"
    }
  ],
  "count": 1,
  "error": null
}
```
**Failure:** Returns `{"ticker": "AAPL", "headlines": [], "count": 0, "error": "fetch_failed"}`.
Agent classifies as `null` signal, `blackout: false` on error.

---

## Input Contract

News Analyst receives a list of tickers from the Orchestrator session driver:

```json
{
  "tickers": ["AAPL", "NVDA", "AMD", "CRWD", "MSFT"],
  "date": "2026-05-27",
  "session_id": "uuid"
}
```

Passed as a JSON string to stdin (subprocess mode) or as the HTTP request body (service mode).

---

## Output Contract

```json
{
  "date": "2026-05-27",
  "session_id": "uuid",
  "news_signals": [
    {
      "ticker": "AAPL",
      "signal": "neutral",
      "blackout": false,
      "headline_count": 2,
      "top_headline": "Apple unveils redesigned MacBook Pro with M4 chip",
      "top_publisher": "Reuters",
      "top_published": "2026-05-27T10:30:00Z"
    },
    {
      "ticker": "NVDA",
      "signal": "positive",
      "blackout": false,
      "headline_count": 4,
      "top_headline": "Nvidia wins $2B data center contract with Microsoft",
      "top_publisher": "Bloomberg",
      "top_published": "2026-05-27T08:15:00Z"
    },
    {
      "ticker": "CRWD",
      "signal": "neutral",
      "blackout": true,
      "headline_count": 1,
      "top_headline": "CrowdStrike reports Q1 2027 results after close today",
      "top_publisher": "PR Newswire",
      "top_published": "2026-05-27T07:00:00Z"
    }
  ],
  "total_tickers": 5,
  "blackout_count": 1,
  "duration_ms": 2840
}
```

The Orchestrator session driver merges `news_signals` into the Research Agent's candidate list before passing to Risk Agent. Research Agent's system prompt includes the news signal for each ticker when available.

**Validation before merge:**
- Every ticker in input must have a corresponding entry in `news_signals`
- Missing ticker: treat as `signal: "null"`, `blackout: false`
- If overall JSON parse fails: skip news signal injection, proceed without it (not a blocking error)

---

## Execution Flow

```
1. Orchestrator session driver calls News Analyst subprocess after Research Agent
   initial screen returns candidate list
2. News Analyst receives tickers via stdin JSON
3. Agent calls get_ticker_news for each ticker (sequential — one call per ticker)
4. Agent classifies each ticker's signal
5. Agent returns output JSON to stdout
6. Session driver parses stdout, merges into research context
7. Research Agent deep-dive prompts include news signal per ticker
```

---

## OTel Trace Emission

News Analyst emits OTel-compatible spans to stdout on a separate line prefixed with `OTEL_SPAN:`.
The session driver reads these lines, strips the prefix, and passes them to `trace/normalizer.py`
for insertion into c_traces.

Each span covers one tool call:

```json
OTEL_SPAN: {
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
    "session.id": "uuid",
    "model": "claude-haiku-4-5-20251001"
  },
  "status": {"code": 1}
}
```

One final span covers the full agent invocation:

```json
OTEL_SPAN: {
  "name": "news_analyst.session",
  "attributes": {
    "agent.name": "news_analyst",
    "tickers.analyzed": 5,
    "blackout.count": 1,
    "tokens.input": 840,
    "tokens.output": 220,
    "duration_ms": 2840
  }
}
```

See trace-formats.md for full normalization spec from OTel to c_traces.

---

## TypeScript Implementation Notes

```typescript
// agents/ts/news_analyst.ts

import Anthropic from "@anthropic-ai/sdk";
import { execSync } from "child_process";

const client = new Anthropic();

const NEWS_ANALYST_SYSTEM_PROMPT = `...`; // see System Prompt above

interface TickerInput {
  tickers: string[];
  date: string;
  session_id: string;
}

interface NewsSignal {
  ticker: string;
  signal: "positive" | "negative" | "neutral" | "null";
  blackout: boolean;
  headline_count: number;
  top_headline: string;
  top_publisher: string;
  top_published: string;
}

const GET_TICKER_NEWS_TOOL: Anthropic.Tool = {
  name: "get_ticker_news",
  description: "Fetch recent news headlines for a stock ticker",
  input_schema: {
    type: "object",
    properties: {
      ticker: { type: "string", description: "Stock ticker symbol" },
    },
    required: ["ticker"],
  },
};

async function getTickerNews(ticker: string): Promise<object> {
  // Calls Python yfinance subprocess for news data
  const result = execSync(
    `python3 agents/tools/news_tools_helper.py ${ticker}`,
    { encoding: "utf-8", timeout: 5000 }
  );
  return JSON.parse(result);
}

async function runNewsAnalyst(input: TickerInput): Promise<void> {
  const startTime = Date.now();
  const traceId = generateTraceId();
  const sessionSpanId = generateSpanId();
  const parentSpanId = input.session_id.replace(/-/g, "").substring(0, 12);

  const userMessage = `Classify news sentiment for these tickers: ${input.tickers.join(", ")}\nDate: ${input.date}`;
  const messages: Anthropic.MessageParam[] = [
    { role: "user", content: userMessage },
  ];

  while (true) {
    const response = await client.messages.create({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 1024,
      system: NEWS_ANALYST_SYSTEM_PROMPT,
      messages,
      tools: [GET_TICKER_NEWS_TOOL],
    });

    if (response.stop_reason === "tool_use") {
      const toolResults: Anthropic.ToolResultBlockParam[] = [];
      for (const block of response.content) {
        if (block.type === "tool_use") {
          const callStart = Date.now();
          const result = await getTickerNews(
            (block.input as { ticker: string }).ticker
          );
          const callEnd = Date.now();
          emitOtelSpan(
            traceId,
            sessionSpanId,
            block,
            result,
            callStart,
            callEnd,
            input.session_id
          );
          toolResults.push({
            type: "tool_result",
            tool_use_id: block.id,
            content: JSON.stringify(result),
          });
        }
      }
      messages.push({ role: "assistant", content: response.content });
      messages.push({ role: "user", content: toolResults });
    } else if (response.stop_reason === "end_turn") {
      const textBlock = response.content.find((b) => b.type === "text");
      if (!textBlock || textBlock.type !== "text") break;
      const output = JSON.parse(textBlock.text);
      output.session_id = input.session_id;
      output.duration_ms = Date.now() - startTime;
      emitSessionSpan(
        traceId,
        sessionSpanId,
        parentSpanId,
        output,
        response.usage,
        input.session_id
      );
      process.stdout.write(JSON.stringify(output) + "\n");
      break;
    } else {
      process.stderr.write(`Unexpected stop_reason: ${response.stop_reason}\n`);
      process.exit(1);
    }
  }
}

function emitOtelSpan(
  traceId: string,
  parentSpanId: string,
  block: Anthropic.ToolUseBlock,
  result: object,
  startMs: number,
  endMs: number,
  sessionId: string
): void {
  const span = {
    traceId,
    spanId: generateSpanId(),
    parentSpanId,
    name: `news_analyst.${block.name}`,
    kind: 3,
    startTimeUnixNano: startMs * 1_000_000,
    endTimeUnixNano: endMs * 1_000_000,
    attributes: {
      "agent.name": "news_analyst",
      "agent.language": "typescript",
      "tool.name": block.name,
      "tool.input": JSON.stringify(block.input),
      "tool.output": JSON.stringify(result),
      "session.id": sessionId,
      model: "claude-haiku-4-5-20251001",
    },
    status: { code: 1 },
  };
  process.stdout.write(`OTEL_SPAN: ${JSON.stringify(span)}\n`);
}

// Entry point
const input: TickerInput = JSON.parse(process.argv[2] || "{}");
runNewsAnalyst(input).catch((err) => {
  process.stderr.write(`News Analyst error: ${err.message}\n`);
  process.exit(1);
});
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| yfinance news fetch fails | Signal = "null", blackout = false. Continue. |
| All tickers fail | Return all as "null". Session driver proceeds without news signals. |
| Agent timeout (> 10s) | Session driver catches, skips news injection, logs warning to c_traces. |
| JSON parse fails (stdout) | Session driver treats as agent unavailable. No block — logs and continues. |
| Node.js not installed | Session driver catches subprocess error, skips news injection. |

News Analyst failure is never a blocking error. The session continues without news signals. The c_traces row records `outcome = "news_unavailable"` in this case.

---

## Calling from Python (subprocess mode)

```python
# orchestrator.py — how session driver calls News Analyst

import subprocess
import json

def run_news_analyst(tickers: list[str], date: str, session_id: str) -> dict:
    """
    Calls TypeScript News Analyst as a subprocess.
    Returns merged news_signals dict, or empty dict on failure.
    """
    input_data = json.dumps({"tickers": tickers, "date": date, "session_id": session_id})
    try:
        result = subprocess.run(
            ["node", "agents/ts/dist/news_analyst.js", input_data],
            capture_output=True, text=True, timeout=15
        )
        # Parse OTel spans from stdout (OTEL_SPAN: prefix lines)
        output_json = None
        for line in result.stdout.splitlines():
            if line.startswith("OTEL_SPAN:"):
                span = json.loads(line[len("OTEL_SPAN:"):].strip())
                tracer.ingest_otel_span(session_id, span)
            else:
                output_json = json.loads(line)
        return output_json or {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {}
```
