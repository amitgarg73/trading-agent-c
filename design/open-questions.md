# Open Design Questions — Trading Agent C

---

## Q1: Loop termination limit — RESOLVED

**Decision:** Per-agent caps + hard time limit (Option D).

| Agent | Cap | Rationale |
|---|---|---|
| Market Agent | 1 round, all 4 tools required | Haiku, structured data — no loops needed |
| Research Agent | 1 get_candidates + max 5 deep-dives (max 6 tool calls per deep-dive ticker) | Bounds both cost and latency |
| Risk Agent | 1 round, all 4 portfolio tools required | Rules-based — no loops needed |
| Orchestrator | Max 2 rounds (initial synthesis + 1 retry) | Hard limit; 3rd round never happens |
| Session total | 40 tool calls hard cap | Safety net across all agents |
| Time limit | 10:20 AM ET hard stop | Orchestrator decides with current state; late entries are negative EV |

If any agent hits its cap mid-reasoning, it returns whatever it has so far.
The Orchestrator's "decide with what I have" instruction fires unconditionally at limit.

---

## Q2: Market Agent and Research Agent sequencing — RESOLVED

**Decision:** Sequential — Market Agent runs first, Research Agent receives its output (Option A).

Haiku is fast (~2 seconds). The 2-second cost is worth it to avoid Research Agent
proposing 10 trades on a SKIP day or ignoring a BEARISH futures signal.

Execution order: Market Agent → Research Agent (with market_report) → Risk Agent → Orchestrator.

---

## Q3: Research Agent depth vs breadth — RESOLVED

**Decision:** Tiered approach (Option C).

Phase 1: One `get_candidates()` call. Read scores and signals. Select top 5 tickers
to investigate deeply (agent chooses which 5).

Phase 2: For each selected ticker, agent may call: `get_news`, `get_live_price`,
`get_intraday_signals`, `get_atr`, `get_position_history`. All 5 tools are optional
per ticker — agent decides which signals it needs for each one.

Max tool calls: 1 (candidates) + 5 tickers × 5 tools = 26. Well within the 40-call session cap.

The "which 5 tickers" decision is where the agentic value lives — Claude is making
a screening judgment, not just receiving pre-filtered data.

---

## Q4: Risk Agent retry — RESOLVED

**Decision:** Orchestrator decides whether to retry (Option C), not the Risk Agent.

Risk Agent verdict is always final. If it rejects all proposals, it states why.
Orchestrator reads the rejection reasons and decides: is this fixable (e.g. "sector
concentration") or structural (e.g. "daily loss limit hit"). Fixable → 1 Research
Agent retry with rejection context. Structural → 0 trades, done.

This keeps the retry logic in the Orchestrator where it belongs, and prevents
the Risk Agent from having any agency over its own scope.

---

## Q5: Alpaca account — RESOLVED

**Decision:** Simulation mode first, no Alpaca account needed.

Simulation substitutes for the two Alpaca-dependent tools:
- `get_live_price(ticker)` → yfinance 1-min close (same fallback as current guardrails.py)
- `get_buying_power()` → `TOTAL_CAPITAL - sum(open position sizes from DB)`

Order execution stays simulation (DB writes only, no broker calls).

When ready for paper trading: new Alpaca account with separate email (not shared with A/B).
STRATEGY_TAG = "c", order prefix = "stratc_".

---

## Q6: Trace granularity — RESOLVED

**Decision:** Full trace from day one. See trace-schema.md for the complete schema.

Every tool call, agent reasoning block, inter-agent message, and Orchestrator
synthesis step is persisted. The trace table is the observability POC — half-traces
don't demonstrate Layer 2 value.

Token counts and latency per step are required fields, not optional.

---

## Q7: Shadow mode mechanics — RESOLVED

**Decision:** Option C first — log what C would have selected, compare to A's
actual selections post-hoc. No orders placed.

Process:
1. C runs at the same time as A (or delayed by 5 min to avoid rate limits)
2. C's Orchestrator produces a final trade list — written to c_scan_results only
3. Compare C's selections vs A's actual positions at EOD: same tickers? better R:R?
   more evidence behind the pick? different tool call depth?
4. Run shadow for minimum 10 trading days before any paper capital decision

This requires zero infrastructure beyond the trace tables and a comparison script.

---

## Q8: Orchestrator model — RESOLVED

**Decision:** Claude Sonnet 4.6 (Option B).

The Orchestrator's output directly becomes trade orders. It also decides whether
to retry, which determines total session cost. Not the place to save $0.005.

---

## Cost Estimate (post-resolution)

Per session (simulation mode, typical day):

| Agent | Model | Est. input tokens | Est. output tokens | Est. cost |
|---|---|---|---|---|
| Market Agent | Haiku | 800 | 200 | $0.001 |
| Research Agent | Sonnet | 4,000 | 800 | $0.024 |
| Risk Agent | Haiku | 1,200 | 300 | $0.002 |
| Orchestrator | Sonnet | 3,000 | 600 | $0.018 |
| **Total** | | **~9,000** | **~1,900** | **~$0.045/day** |

Monthly (22 trading days): ~$1.00/month.
With retry triggered (20% of days): ~$1.10/month.
Running alongside A+B: total combined Claude spend ~$1.90/month.

---

## Remaining Open Questions (new ones surfaced during resolution)

### Q9: Tool call implementation pattern

**Question:** When Research Agent calls a tool, does it happen inside one
`messages.create()` loop or does the Orchestrator drive the tool execution?

**Options:**
- A: Agent-internal loop — each agent runs its own tool use loop until `end_turn`
- B: Orchestrator-driven — Orchestrator intercepts every tool call across all agents

**Recommendation:** A. Standard Anthropic tool use pattern. Each agent is self-contained.
The Orchestrator receives the agent's final output, not its intermediate tool calls.
Trace logging happens inside each agent's loop.

**Status:** RESOLVED — Option A

---

### Q10: How does Research Agent receive market_report?

**Question:** Is market_report injected as part of the system prompt, the user
message, or as a tool result?

**Options:**
- A: System prompt injection (baked in before the agent starts)
- B: User message (first message to the agent contains the report)
- C: Synthetic tool result (agent "calls" get_market_context and receives the report)

**Recommendation:** B. User message is the cleanest — market_report is dynamic
(changes every day) so it doesn't belong in the system prompt. Option C is clever
but introduces a fake tool call into the trace, which pollutes observability.

**Status:** RESOLVED — Option B

---

### Q11: What does Research Agent receive — full candidate JSON or summary?

**Question:** After get_candidates() is called, does the agent receive the full
JSON for all candidates (same as Strategy A), or a summary with scores only?

**Options:**
- A: Full JSON — same as Strategy A, agent sees all signals upfront
- B: Scores-only summary — agent sees ticker + score + price, fetches signals via tools
- C: Tiered — agent sees scores + top-3 signals per ticker, fetches rest via tools

**Recommendation:** B. This is what differentiates C from A. get_candidates()
returns ticker + score + price only. The agent decides which tickers are worth
fetching full signals for. If it returns full JSON, the Research Agent becomes
Strategy A with extra steps.

**Status:** RESOLVED — Option B
