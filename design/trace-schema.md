# Trace Schema — Trading Agent C

The trace table is the core observability artifact. Every tool call, agent
message, and decision is persisted here. This is the data source for the
AI Agent Reliability POC.

---

## Table: c_traces

| Column | Type | Description |
|---|---|---|
| id | uuid | Primary key |
| session_id | uuid | Groups all rows from one premarket run |
| span_id | uuid | Unique identifier for this span (OTel-compatible) |
| parent_span_id | uuid | FK to span_id of parent — null for session root, agent span_id for tool calls |
| entity_id | str | Ticker this step relates to (e.g. "AAPL"), or null for session-level steps |
| date | date | Trading date |
| sequence | int | Order within the session (1, 2, 3...) |
| agent | str | "orchestrator" / "market" / "research" / "risk" |
| step_type | str | "tool_call" / "agent_message" / "decision" / "error" |
| tool_name | str | Name of tool called (null if step_type != tool_call) |
| tool_input | jsonb | Exact input passed to the tool |
| tool_output | jsonb | Exact output returned by the tool |
| agent_reasoning | text | Claude's reasoning text before/after the tool call |
| outcome | str | Controlled vocabulary — see Outcome Vocabulary below |
| tokens_input | int | Input tokens for this step |
| tokens_output | int | Output tokens for this step |
| latency_ms | int | Wall time for this step in milliseconds |
| model | str | Which Claude model handled this step |
| error | str | Error message if step_type == "error" |
| created_at | timestamptz | Wall clock timestamp |

---

## Outcome Vocabulary

Controlled values for the `outcome` field. Each agent uses a subset.
No free-text outcomes — unknown values are flagged as anomalies.

| Agent | outcome value | Meaning |
|---|---|---|
| market | `go` | Market conditions acceptable, proceed |
| market | `caution` | Elevated risk — research agent uses higher bar |
| market | `skip` | Session ends, no trades today |
| research | `proposed` | Ticker made it into the final proposal list |
| research | `skipped_blackout` | Dropped — earnings today or tomorrow |
| research | `skipped_below_vwap` | Dropped — price below VWAP (especially on CAUTION day) |
| research | `skipped_atr` | Dropped — ATR too wide for stop sizing |
| research | `skipped_score` | Dropped — score below threshold for current market conditions |
| research | `skipped_price_moved` | Dropped — live price drifted too far from scanner price |
| risk | `approved` | All constraints satisfied |
| risk | `rejected_loss_limit` | Daily loss limit already hit |
| risk | `rejected_capital` | Insufficient buying power for this position size |
| risk | `rejected_concentration` | Adding this position would exceed sector concentration limit |
| risk | `rejected_duplicate` | Ticker already in open positions |
| risk | `rejected_count` | Position count limit reached |
| orchestrator | `converged` | Approved trades exist — final list returned |
| orchestrator | `retry_triggered` | Fixable rejections — Research Agent called again |
| orchestrator | `structural_block` | Structural rejections — zero trades, no retry |
| orchestrator | `time_limit` | Hard stop at 10:20 AM ET |
| orchestrator | `tool_cap` | 40-call session cap reached |
| orchestrator | `skip_propagated` | Market Agent returned SKIP — session ended early |
| any | `error` | Unhandled exception at this step |

---

## Span Hierarchy

Every trace row fits into one of three levels:

```
session root (span_id = S1, parent_span_id = null)
    ├── market_agent span (span_id = M1, parent_span_id = S1)
    │     ├── get_vix tool call (span_id = M1-T1, parent_span_id = M1, entity_id = null)
    │     ├── get_futures tool call (span_id = M1-T2, parent_span_id = M1, entity_id = null)
    │     ├── get_fear_greed tool call (span_id = M1-T3, parent_span_id = M1, entity_id = null)
    │     └── get_sector_rotation tool call (span_id = M1-T4, parent_span_id = M1, entity_id = null)
    ├── research_agent span (span_id = R1, parent_span_id = S1)
    │     ├── get_candidates tool call (span_id = R1-T1, parent_span_id = R1, entity_id = null)
    │     ├── get_news/AAPL (span_id = R1-T2, parent_span_id = R1, entity_id = "AAPL")
    │     ├── get_intraday_signals/AAPL (span_id = R1-T3, parent_span_id = R1, entity_id = "AAPL")
    │     └── get_news/NVDA (span_id = R1-T4, parent_span_id = R1, entity_id = "NVDA")
    ├── risk_agent span (span_id = K1, parent_span_id = S1)
    │     └── verdict/AAPL (span_id = K1-V1, parent_span_id = K1, entity_id = "AAPL")
    └── orchestrator decision (span_id = O1, parent_span_id = S1)
```

This structure enables:
- Flame graph rendering: latency_ms at each span level shows where time was spent
- Entity-scoped queries: `WHERE entity_id = 'AAPL'` returns the full story of AAPL across all agents
- Agent boundary queries: `WHERE parent_span_id = R1` returns everything Research Agent did

---

## Cross-Agent Correlation Queries

These are the queries the observability product enables — not log archaeology.

**Full story of one ticker in one session:**
```sql
SELECT sequence, agent, step_type, tool_name, outcome, agent_reasoning, latency_ms
FROM c_traces
WHERE session_id = 'X' AND entity_id = 'AAPL'
ORDER BY sequence;
```

**Sessions where Research Agent proposed HIGH confidence but Risk Agent rejected:**
```sql
SELECT t.session_id, t.entity_id, t.outcome, r.outcome as risk_outcome
FROM c_traces t
JOIN c_traces r ON t.session_id = r.session_id AND t.entity_id = r.entity_id
WHERE t.agent = 'research' AND t.outcome = 'proposed'
  AND r.agent = 'risk' AND r.outcome LIKE 'rejected_%'
  AND t.tool_input->>'confidence' = 'HIGH';
```

**Retry rate by week:**
```sql
SELECT DATE_TRUNC('week', date), COUNT(*) FILTER (WHERE retry_triggered) / COUNT(*)::float
FROM c_sessions
GROUP BY 1 ORDER BY 1;
```

**Which tickers get investigated but never proposed:**
```sql
SELECT entity_id, COUNT(DISTINCT session_id) as sessions_investigated,
       COUNT(*) FILTER (WHERE outcome = 'proposed') as times_proposed
FROM c_traces
WHERE agent = 'research' AND entity_id IS NOT NULL
GROUP BY entity_id
HAVING COUNT(*) FILTER (WHERE outcome = 'proposed') = 0
ORDER BY sessions_investigated DESC;
```

---

## Table: c_sessions

One row per premarket run. Summary of the full session.

| Column | Type | Description |
|---|---|---|
| id | uuid | session_id (FK to c_traces) |
| date | date | Trading date |
| total_steps | int | Total trace rows for this session |
| total_tool_calls | int | How many tool calls were made |
| agents_invoked | str[] | Which agents ran |
| loop_iterations | int | How many Orchestrator rounds |
| total_tokens_input | int | Across all agents |
| total_tokens_output | int | Across all agents |
| total_cost_usd | float | Estimated cost for the session |
| total_latency_ms | int | Wall time from start to final trade list |
| trades_proposed | int | Research Agent proposals |
| trades_approved | int | After Risk Agent |
| trades_executed | int | After guardrails and Alpaca |
| risk_rejections | int | How many Risk Agent rejections |
| retry_triggered | bool | Did Orchestrator request a Research Agent retry |
| terminal_reason | str | "converged" / "time_limit" / "tool_cap" / "no_candidates" |
| started_at | timestamptz | |
| completed_at | timestamptz | |

---

## What This Enables (Observability POC)

**Layer 1 — Detect:**
- Constraint adherence: query tool_output for reward_risk on each trade_proposal
- Output quality: flag sessions where trades_proposed = 0 but candidates existed
- Cost per session: sum tokens across session_id
- Model tracking: which model ran which agent on which date

**Layer 2 — Understand:**
- Full causal trace: for any executed trade, replay the exact tool calls that
  led to its selection (sequence column + agent_reasoning)
- Agent disagreement: sessions where risk_rejections > 0 + retry_triggered
- Research depth: which tickers did Research Agent deep-dive vs skim
  (count tool_calls per ticker in tool_input)
- Latency breakdown: latency_ms per agent to find the slow step

**Layer 3 — Control (future):**
- Session-level circuit breaker: if total_tool_calls approaching cap, alert
- Budget enforcement: if total_cost_usd > threshold, flag before session completes
- Anomaly detection: session with unusually high loop_iterations
