# Research Agent — Design Doc

**File:** `agents/research_agent.py`  
**Model:** `claude-sonnet-4-6`  
**Role:** Quantitative stock analyst. Decides which tickers to investigate, gathers per-ticker evidence, and proposes specific trades.  
**Runs:** Once per session (twice if Orchestrator triggers a retry).

---

## Why Sonnet

Research Agent performs the highest-value reasoning in the system: reading a scored list, deciding which 5 tickers are worth investigating, selecting tools per ticker based on what it already knows, and forming trade proposals with justified confidence assignments. This is genuine multi-step reasoning under uncertainty. Haiku's shorter context window and weaker chain-of-thought make it unsuitable here. Opus is unnecessary — Sonnet handles financial signal interpretation well at a reasonable cost.

---

## System Prompt (production)

```
You are a quantitative stock analyst for an intraday momentum trading system.
Your job: identify the best same-day setups and propose specific trades.

You receive today's market conditions in the user message. Use them to calibrate
your selectivity. On CAUTION days, the bar is higher — only the clearest setups.

────────────────────────────────────────
PHASE 1 — SCREEN
────────────────────────────────────────
Call get_candidates() exactly once. You receive ticker, technical_score, and
current_price only — no signals yet.

Read the scores. Identify at most 5 tickers to investigate. Apply these rules:

  Standard day (decision = GO):
    - Prefer score ≥ 7. Scores below 5 do not qualify.
    - If fewer than 5 tickers score ≥ 7, add scores 5–6 to fill.
    - Skip tickers with avg_volume < 500,000 (insufficient liquidity).

  Caution day (decision = CAUTION):
    - Only investigate tickers with score ≥ 7.
    - Maximum 3 tickers regardless of available candidates.

Do not call any other tool until you have chosen your investigation list.
Calling get_candidates a second time is an error — it will be rejected.

────────────────────────────────────────
PHASE 2 — INVESTIGATE
────────────────────────────────────────
For each chosen ticker, investigate in this order:

STEP A: get_news(ticker) — REQUIRED, always first for every ticker.
  If blackout == true: stop investigating this ticker immediately.
  Drop it. Move to the next candidate on your list.
  Never propose a ticker with blackout == true under any circumstances.

STEP B: get_intraday_signals(ticker) — call for every non-blacked-out ticker.
  Tells you: above VWAP? RS vs SPY? today's % change.
  On CAUTION days: if above_vwap == false, drop the ticker.

STEP C (optional): get_live_price(ticker)
  Call this if the current_price from get_candidates seems stale (e.g. pre-market
  data is often 30+ minutes old). If live price differs from scanner price by
  more than 3%: use the live price as entry_price. If it differs by more than 5%,
  drop the ticker — price moved too far.

STEP D (optional): get_atr(ticker)
  Call this if the ticker is volatile or you need to verify the stop can hold.
  Rule: if atr_pct > 5.0, the 0.67% stop is inside the noise band — drop the ticker.
  If atr_pct > 3.0 but ≤ 5.0, note it in the evidence and reduce confidence to LOW.

STEP E (optional): get_position_history(ticker)
  Call this for HIGH confidence candidates to check if this ticker has been
  reliable for this strategy. A win_rate_pct < 30% over the last 30 days
  is a red flag — downgrade confidence one tier.

────────────────────────────────────────
TRADE PROPOSAL RULES
────────────────────────────────────────
entry_price    = live_price if fetched, else current_price from get_candidates
target_price   = round(entry_price * 1.04, 2)   — +4% ceiling
stop_loss      = round(entry_price * 0.9933, 2)  — -0.67% floor
position_size:
  $3,000 flat for every proposal (confidence does NOT scale size)
shares         = floor(position_size / entry_price)

CONFIDENCE ASSIGNMENT:
  HIGH   — score ≥ 7, above_vwap == true, rs_vs_spy ≥ 1.5
  MEDIUM — score 5–6 with above_vwap == true, OR score ≥ 7 with rs_vs_spy 0.8–1.4
  LOW    — score 5–6 with above_vwap == true and rs_vs_spy < 0.8
  (cannot assign HIGH on CAUTION days without score ≥ 8)

evidence list: at least 3 items. State the signals that drove the pick, not the
formula. E.g. "RS vs SPY 1.8x, above VWAP at $187.20, score 8, ATR 1.4% supports
stop" — not "above_vwap == true and rs_vs_spy == 1.8".

Maximum proposals = market_report.max_positions.
If you investigated 5 tickers but only 3 pass all checks, return 3 proposals.
Do not inflate proposals to hit the ceiling. Quality over quantity.

────────────────────────────────────────
OUTPUT
────────────────────────────────────────
Return JSON only. No commentary before or after.

{
  "proposals": [
    {
      "ticker": str,
      "entry_price": float,
      "target_price": float,
      "stop_loss": float,
      "position_size": float,
      "shares": int,
      "confidence": "HIGH|MEDIUM|LOW",
      "evidence": [str, str, str]
    }
  ],
  "skipped": [
    { "ticker": str, "reason": str }
  ],
  "tool_calls_used": int,
  "summary": str
}

Each skipped entry must name the reason using one of:
  blackout, below_vwap_caution, atr_too_wide, price_moved, low_score,
  history_poor, score_caution_threshold

If no proposals pass, return proposals: [] with all investigated tickers in skipped.
```

---

## Tools

### get_candidates
**Purpose:** Fetch the scored universe. Returns scores and prices only, not full signals. This is the key architectural difference from Strategy A — the agent decides what to investigate, not the orchestrator.  
**Called:** Exactly once per session. The tool implementation rejects a second call.  
**Input:** `{ "min_score": int }` — default 5  
**Output:**
```json
[
  { "ticker": "NVDA", "technical_score": 9, "current_price": 875.20, "avg_volume": 45000000 },
  { "ticker": "AAPL", "technical_score": 8, "current_price": 187.42, "avg_volume": 62000000 },
  ...
]
```
Sorted by `technical_score` descending. Max 100 results.  
**Failure behavior:** If this tool fails, the agent cannot proceed. Return `proposals: []`, `summary: "get_candidates failed: {error}"`. Orchestrator ends session with `terminal_reason = "no_candidates"`.

### get_news
**Purpose:** Earnings blackout check + headline context. Must be first call per ticker.  
**Called:** Once per investigated ticker. Required.  
**Input:** `{ "ticker": str }`  
**Output:**
```json
{
  "blackout": false,
  "reason": null,
  "headlines": ["Apple supply chain strong heading into summer", "iPhone demand steady in Asia"]
}
```
**Failure behavior:** If the call returns `{ "error": ... }`, treat as `blackout: false` with empty headlines. Log in evidence. Do not skip the ticker on a news tool failure alone.

### get_intraday_signals
**Purpose:** VWAP position, relative strength vs SPY, today's move. The most important signal for momentum entries.  
**Called:** Once per non-blacked-out ticker. Always call after get_news passes.  
**Input:** `{ "ticker": str }`  
**Output:**
```json
{
  "above_vwap": true,
  "vwap": 186.90,
  "rs_vs_spy": 1.8,
  "today_pct_change": 0.65
}
```
Note: `rs_vs_spy` may be `null` if SPY move is too small to compute a meaningful ratio. Treat null RS as neutral (0.8 default for confidence purposes).  
**Failure behavior:** If this tool fails, treat as `above_vwap: false`. On a CAUTION day, this means the ticker is dropped. On a GO day, continue but cap confidence at LOW.

### get_live_price
**Purpose:** Confirm price hasn't moved more than 3% from scanner price before proposing entry.  
**Called:** Optional. Use when scanner data may be stale (before 9:45 AM ET, or when current_price looks like a pre-market print).  
**Input:** `{ "ticker": str }`  
**Output:**
```json
{ "price": 187.42, "source": "yfinance", "stale_minutes": 1 }
```
**Decision rules:**
- `abs(live_price - scanner_price) / scanner_price > 0.05` → drop ticker (`reason: "price_moved"`)
- `abs(live_price - scanner_price) / scanner_price > 0.03` → use live_price as entry_price, note in evidence
- `stale_minutes > 5` → note staleness in evidence  

**Failure behavior:** Skip. Use scanner price. Do not drop the ticker.

### get_atr
**Purpose:** Validate that the fixed 0.67% stop is compatible with this ticker's noise band.  
**Called:** Optional. Use for high-score candidates where volatility is uncertain, or when today's move is already large.  
**Input:** `{ "ticker": str }`  
**Output:**
```json
{ "atr_pct": 1.4, "orb_pct": 0.38 }
```
**Decision rules:**
- `atr_pct > 5.0` → drop ticker (`reason: "atr_too_wide"`)
- `atr_pct > 3.0` → downgrade confidence one tier, note in evidence
- `orb_pct < 0.3` → note choppy open; ATR sizer will halve shares later

**Failure behavior:** Skip. Do not drop the ticker on ATR failure.

### get_position_history
**Purpose:** Check this ticker's recent behavioral track record for this strategy.  
**Called:** Optional. Use for HIGH-confidence candidates to confirm past reliability.  
**Input:** `{ "ticker": str, "days": int }` — default days = 30  
**Output:**
```json
{ "trades": 4, "wins": 1, "win_rate_pct": 25.0, "avg_pnl": -12.5, "last_exit": "STOP" }
```
**Decision rules:**
- `trades >= 3 AND win_rate_pct < 30` → downgrade confidence one tier, note in evidence
- `trades < 3` → insufficient data, ignore
- `last_exit == "STOP"` on the most recent trade → note in evidence (not a block)

**Failure behavior:** Skip. Do not drop the ticker.

---

## Input Contract

Research Agent receives `market_report` as a user message from the Orchestrator:

```
User message:
"Today's market conditions (from Market Agent):
{market_report JSON}

Investigate candidates and return trade proposals."
```

On retry, it receives:
```
User message:
"Today's market conditions (from Market Agent):
{market_report JSON}

Your previous proposals were rejected:
{list of rejected tickers with rejection reasons}

Avoid these tickers: {rejected_ticker_list}.
Investigate alternative candidates and return new proposals."
```

---

## Output Contract

```json
{
  "proposals": [
    {
      "ticker": "AAPL",
      "entry_price": 187.42,
      "target_price": 194.92,
      "stop_loss": 186.16,
      "position_size": 3500,
      "shares": 18,
      "confidence": "HIGH",
      "evidence": [
        "Score 8, VWAP above at $186.90, RS vs SPY 1.8x — momentum confirmed",
        "ATR 1.4% — stop at 0.67% is inside noise band but acceptable for this score",
        "No earnings blackout, supply chain headlines positive"
      ]
    }
  ],
  "skipped": [
    { "ticker": "NVDA", "reason": "blackout" },
    { "ticker": "TSLA", "reason": "atr_too_wide" }
  ],
  "tool_calls_used": 18,
  "summary": "Investigated 5 tickers. NVDA blacked out (earnings), TSLA ATR 5.8% exceeds threshold. Proposing AAPL, AMD, CRWD — all above VWAP with RS > 1.5x on a GO day."
}
```

**Validation before passing to Risk Agent:**
- `proposals` must be a list (may be empty)
- Each proposal must have all required fields
- `confidence` must be one of HIGH, MEDIUM, LOW
- If JSON parse fails: Orchestrator ends session, `terminal_reason = "research_parse_error"`

---

## Execution Flow

```
1. Orchestrator calls Research Agent (sends market_report as context)
2. Agent calls get_candidates(min_score=5)
3. Agent reads scores, selects up to 5 tickers to investigate
4. For each ticker:
   a. get_news(ticker)
      → blackout == true: skip, log reason, pick next candidate
   b. get_intraday_signals(ticker)
      → CAUTION + above_vwap == false: skip
   c. [optional] get_live_price(ticker)
   d. [optional] get_atr(ticker)
   e. [optional] get_position_history(ticker)
   f. Build proposal or add to skipped list
5. Agent returns trade_proposals JSON
6. Orchestrator parses and passes to Risk Agent
```

On retry (second call with rejection context):
- Same flow, but agent avoids previously rejected tickers
- Same tool caps apply (1 get_candidates + max 5 deep-dives)
- get_candidates may return some new tickers if market is still open; agent investigates fresh candidates

---

## Constraint Enforcement

| Constraint | How Enforced |
|---|---|
| get_candidates called once | Tool implementation checks a session flag; returns `{ "error": "get_candidates already called this session" }` on second call |
| Max 5 deep-dives | Prompt instruction. Session-level tool cap (40 total) as backstop. |
| get_news first per ticker | Prompt instruction with explicit ordering: "STEP A: get_news — REQUIRED, always first." |
| No cross-agent tool access | Tool registration: only these 6 tools in the `tools=[]` parameter for this agent. |
| CAUTION day: score ≥ 7 only | Prompt instruction. Not enforced in code — Orchestrator reviews output for compliance. |
| Max proposals = max_positions | Prompt instruction: "Maximum proposals = market_report.max_positions." |

---

## Error Handling

| Scenario | Behavior |
|---|---|
| get_candidates fails | Return `proposals: []`, end session gracefully |
| Per-ticker tool fails | Use failure fallback (documented per-tool above), continue |
| Agent proposes 0 trades | Pass empty proposals to Risk Agent; Orchestrator decides on retry |
| Agent exceeds 5 deep-dives | Session-level 40-call cap catches runaway. Prompt provides strong guidance. |
| Agent calls get_candidates twice | Tool returns error; agent should proceed with original results |
| Agent output JSON invalid | Orchestrator catches parse error, `terminal_reason = "research_parse_error"` |
| Agent times out (>60s) | Orchestrator timeout; `terminal_reason = "research_timeout"` |

---

## Trace Entries Generated

Research Agent produces up to ~22 trace rows per normal session:

| step_type | description | outcome |
|---|---|---|
| tool_call | get_candidates | null (logged, no outcome) |
| tool_call | get_news / TICKER | null |
| tool_call | get_intraday_signals / TICKER | null |
| tool_call | get_live_price / TICKER (optional) | null |
| tool_call | get_atr / TICKER (optional) | null |
| tool_call | get_position_history / TICKER (optional) | null |
| agent_message | final proposal JSON | `proposed` per approved ticker; `skipped_*` per dropped ticker |

For per-ticker outcomes, one `agent_message` row is written per ticker with `entity_id` set to the ticker. This enables the `WHERE entity_id = 'AAPL' AND agent = 'research'` query to return the full AAPL investigation story.

---

## Implementation Notes

```python
# agents/research_agent.py

RESEARCH_AGENT_TOOLS = [
    build_tool("get_candidates",        get_candidates_impl),
    build_tool("get_news",              get_news_impl),
    build_tool("get_live_price",        get_live_price_impl),
    build_tool("get_intraday_signals",  get_intraday_signals_impl),
    build_tool("get_atr",               get_atr_impl),
    build_tool("get_position_history",  get_position_history_impl),
]

def run_research_agent(
    session_id: str,
    market_report: dict,
    tracer: TraceLogger,
    rejected_context: dict | None = None,  # set on retry
) -> dict:
    """
    Runs Research Agent tool loop until stop_reason == "end_turn".
    Returns parsed trade_proposals dict.
    """
    user_content = _build_research_user_message(market_report, rejected_context)
    messages = [{"role": "user", "content": user_content}]
    tool_call_count = 0
    MAX_TOOL_CALLS = 25  # local safety net below session-level 40 cap

    while True:
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=RESEARCH_AGENT_SYSTEM_PROMPT,
            messages=messages,
            tools=RESEARCH_AGENT_TOOLS,
        )
        tracer.log_tokens(session_id, "research", response.usage)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_call_count += 1
                    result = dispatch_tool(block.name, block.input)
                    tracer.log_tool_call(session_id, "research", block, result,
                                        entity_id=block.input.get("ticker"))
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": json.dumps(result)})
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            if tool_call_count >= MAX_TOOL_CALLS:
                # Force termination — add a user message asking for final output
                messages.append({
                    "role": "user",
                    "content": "You have reached the tool call limit. Return your final proposals now based on what you have investigated."
                })

        elif response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if b.type == "text")
            proposals = json.loads(text)
            tracer.log_agent_message(session_id, "research", proposals)
            return proposals

        else:
            raise ResearchAgentError(f"unexpected stop_reason: {response.stop_reason}")
```

The `entity_id=block.input.get("ticker")` call is what enables per-ticker trace queries. Every per-ticker tool call is tagged with the ticker in the trace row.
