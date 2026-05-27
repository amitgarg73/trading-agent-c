# Learning Agent — Design Doc

**File:** `agents/learning_agent.py`
**Model:** `claude-sonnet-4-6`
**Role:** EOD performance analyst. Reads today's trade outcomes, identifies patterns, adjusts strategy parameters within bounds, and writes structured learnings for tomorrow's session context.
**Runs:** Once per EOD session, after reconciliation. Skipped if no trades executed today.

---

## Why Sonnet

Learning requires reading multi-dimensional data, spotting patterns across trades, and making judgment calls about whether sample sizes are sufficient and whether a parameter change is warranted. Haiku will over-fit to single data points. Sonnet handles the nuance of "3 trades is borderline — is this worth acting on?" and writes coherent finding summaries that are readable in tomorrow's trace context.

---

## System Prompt (production)

```
You are an EOD performance analyst for an autonomous trading system.

After each trading day, you receive today's trade record, market context,
current strategy parameters, and recent learnings from the past 14 days.

Your job:
1. Analyze trade outcomes across 5 dimensions (listed below).
2. Identify parameters that should be adjusted based on evidence.
3. Write findings to c_learnings using write_learning.
4. Adjust parameters using adjust_param if evidence warrants it.
5. Recommend new goals if you see a persistent winning pattern.

────────────────────────────────────────
ANALYSIS DIMENSIONS
────────────────────────────────────────

DIMENSION 1 — Entry quality
  Examine: entry_time (9:30-10:00 vs 10:00-11:00 vs 11:00+)
  Examine: entry_type (bid / mid — from hybrid_limit_price)
  Examine: score_at_entry vs realized outcome
  Ask: Did tighter entries (bid) produce better P&L than mid entries?
  Ask: Which entry time windows had highest win rate today?

DIMENSION 2 — Exit quality
  Examine: exit_reason (target / stop / eod_forced / partial_then_target)
  Examine: partial exits — did partial profit help or reduce final P&L?
  Ask: Did trades hitting target have higher R:R than projected?
  Ask: Were stop losses too tight (stopped out, then recovered)?

DIMENSION 3 — Market regime correlation
  Examine: VIX level vs win rate
  Examine: market_signal (GO vs CAUTION) — did CAUTION day rules help?
  Examine: sector performance vs sector of winning trades
  Ask: Is the current VIX-to-max-positions table producing good results?

DIMENSION 4 — Ticker patterns
  Examine: repeat winners (same ticker traded before, outcome comparison)
  Examine: tickers with 2+ losses in last 30 days
  Ask: Should any tickers be added to a soft-avoid list?
  (Soft avoid = lower score required, not a hard block)

DIMENSION 5 — Parameter effectiveness
  Examine: strategy_min_score — were all today's winners score >= current threshold?
  Examine: atr_multiplier — were stops sized correctly?
  Examine: rr_ratio — did targets get hit, or were they too ambitious?
  Ask: What would win rate look like if min_score were 1 point higher or lower?

────────────────────────────────────────
ADJUSTMENT RULES
────────────────────────────────────────

Before adjusting any parameter, apply ALL of these checks in order:

CHECK 1 — Sample size
  Count matching trades for the pattern you observed.
  If count < 3: write as observation (learning_type = "observation"). Do not adjust.
  If count >= 3: proceed to check 2.

CHECK 2 — Cooldown
  Read the cooldown_until field from the parameter row.
  If today is before cooldown_until: do not adjust this parameter today.
  Write as observation instead.

CHECK 3 — Bounds
  Your proposed new value must be within [min_bound, max_bound].
  If it falls outside: do NOT apply it. Set requires_human_review = true.

CHECK 4 — Daily adjustment limit
  You may adjust at most 2 parameters per day.
  If you have already made 2 adjustments, write remaining findings as observations.

CHECK 5 — False positive check
  Search recent_learnings for any adjustment to this parameter that was
  later marked false_positive. If found in the last 30 days: increase your
  evidence threshold — require count >= 5 before adjusting again.

────────────────────────────────────────
WHAT YOU MAY NOT CHANGE
────────────────────────────────────────

Never call adjust_param for:
  - daily_loss_limit (Tier 1-2 principal protection)
  - account_drawdown_thresholds (Tier 3-4)
  - session_time_limit (10:20 AM hard stop)
  - session_tool_cap (40 calls)
  - max_consecutive_losing_days (Tier 5 or 6)

These require human review. Flag them with requires_human_review = true if you
believe they are misconfigured.

────────────────────────────────────────
GOAL RECOMMENDATIONS
────────────────────────────────────────

After analysis, if you see a persistent winning pattern spanning 5+ sessions,
call recommend_goal. Examples of valid recommendations:
  - Sector performing consistently well → recommend sector_max_allocation increase
  - Specific time window producing all wins → recommend time_of_day_filter
  - Specific score range outperforming → recommend score_threshold_by_sector

Do not recommend more than 1 new goal per day.

────────────────────────────────────────
OUTPUT
────────────────────────────────────────

After all tool calls, return a structured summary. This is written to c_traces.

{
  "session_date": "YYYY-MM-DD",
  "trades_analyzed": int,
  "win_rate": float,
  "total_pnl": float,
  "learnings_written": int,
  "params_adjusted": int,
  "goal_recommended": bool,
  "top_finding": str,
  "context_for_tomorrow": str
}

context_for_tomorrow: 2-3 sentences injected into tomorrow's Research Agent
and Orchestrator context. Make it specific and actionable. Not a recap.
Example: "Technology sector showed 80% win rate today on 5 trades. Healthcare
was 0/2. For tomorrow, weight Technology candidates more aggressively and
require score >= 6 for Healthcare."
```

---

## Tools

### read_today_trades
**Purpose:** Load all executed trades from today's session.
**Input:** `{}`
**Output:**
```json
[
  {
    "ticker": "AAPL",
    "entry_price": 187.42,
    "exit_price": 192.10,
    "position_size": 3500.0,
    "shares": 18,
    "entry_time": "2026-05-27T13:45:00Z",
    "exit_time": "2026-05-27T20:10:00Z",
    "realized_pnl": 84.24,
    "exit_reason": "target",
    "score_at_entry": 7,
    "vix_at_entry": 18.4,
    "market_signal": "GO",
    "news_signal": "neutral",
    "sector": "Technology",
    "entry_type": "bid"
  }
]
```

### read_session_context
**Purpose:** Load today's session summary and market conditions.
**Input:** `{}`
**Output:**
```json
{
  "session_id": "uuid",
  "date": "2026-05-27",
  "vix": 18.4,
  "futures_pct": 0.4,
  "fear_greed": 52,
  "market_signal": "GO",
  "leading_sector": "Technology",
  "trades_proposed": 5,
  "trades_approved": 3,
  "trades_executed": 3,
  "retry_triggered": false,
  "total_cost_usd": 0.052
}
```

### read_strategy_params
**Purpose:** Load all current parameters and their bounds.
**Input:** `{}`
**Output:**
```json
[
  {
    "param_key": "strategy_min_score",
    "param_value": 5.0,
    "min_bound": 4.0,
    "max_bound": 7.0,
    "cooldown_until": "2026-05-29",
    "last_updated": "2026-05-24T20:00:00Z",
    "updated_by": "learning_agent"
  }
]
```

### read_recent_learnings
**Purpose:** Load learnings from the past 14 days for context and false-positive checks.
**Input:** `{"days": 14}`
**Output:**
```json
[
  {
    "learning_id": "uuid",
    "session_date": "2026-05-26",
    "learning_type": "adjustment",
    "entity_id": "Technology",
    "finding": "Technology sector 75% win rate on 4 trades",
    "confidence": "medium",
    "action_taken": "sector_max_allocation_technology raised from 0.35 to 0.40",
    "requires_human_review": false,
    "expires_date": "2026-06-09",
    "outcome": "in_evaluation"
  }
]
```

**Outcome values for learnings:**
- `pending` — written, not yet evaluable
- `in_evaluation` — adjustment made, monitoring performance
- `validated` — adjustment produced measurable improvement
- `false_positive` — adjustment hurt performance, reverted
- `expired` — past expires_date

### write_learning
**Purpose:** Persist a finding to c_learnings.
**Input:**
```json
{
  "learning_type": "observation|adjustment|avoid_ticker|sector_signal|goal_trigger",
  "entity_id": "AAPL|Technology|null",
  "finding": "str — specific, quantified",
  "confidence": "low|medium|high",
  "action_taken": "str or null",
  "requires_human_review": false,
  "expires_days": 7
}
```
**Output:** `{"learning_id": "uuid", "status": "written"}`

### adjust_param
**Purpose:** Update a strategy parameter within bounds.
**Input:**
```json
{
  "param_key": "strategy_min_score",
  "new_value": 6.0,
  "reason": "str — specific evidence behind the change"
}
```
**Output:**
```json
{
  "status": "applied|rejected",
  "rejection_reason": "out_of_bounds|cooldown_active|not_found",
  "old_value": 5.0,
  "new_value": 6.0,
  "cooldown_until": "2026-06-01"
}
```
The tool enforces bounds and cooldown server-side. If rejected, the agent must write an observation instead and set `requires_human_review = true` if the proposed value was out of bounds.

### recommend_goal
**Purpose:** Write a pending goal recommendation for human review.
**Input:**
```json
{
  "goal_type": "sector_max_allocation|score_threshold_by_sector|time_of_day_filter|ticker_affinity",
  "target_value": 0.40,
  "entity_id": "Technology",
  "evidence": "75% win rate over 5 sessions, 12 trades total",
  "confidence": "medium"
}
```
**Output:** `{"goal_id": "uuid", "status": "pending_approval"}`
An alert is sent to the user when a goal is recommended.

---

## Execution Flow

```
1. EOD session calls run_learning_agent(session_id, daily_performance)
2. Agent calls read_today_trades()
3. Agent calls read_session_context()
4. Agent calls read_strategy_params()
5. Agent calls read_recent_learnings(days=14)
6. Agent analyzes across 5 dimensions
7. For each finding:
   a. If sample_size < 3 → write_learning(type="observation")
   b. If sample_size >= 3 + passes all checks → adjust_param or write_learning(type="adjustment")
8. If goal recommendation warranted → recommend_goal
9. Agent returns summary JSON
10. EOD session writes summary to c_traces
11. context_for_tomorrow stored in c_sessions for next premarket injection
```

---

## Evaluation Window (detecting false positives)

When Learning Agent makes a parameter adjustment, the `outcome` field is set to `in_evaluation`.

After 5 sessions, a background check (run during EOD) compares:
- Average P&L in 5 sessions before adjustment
- Average P&L in 5 sessions after adjustment

```python
def evaluate_recent_adjustments():
    """Called during EOD after Learning Agent completes."""
    adjustments = db.get_learnings(
        learning_type="adjustment",
        outcome="in_evaluation",
        min_sessions_elapsed=5
    )
    for adj in adjustments:
        before_pnl = db.avg_pnl_before(adj.session_date, window=5)
        after_pnl = db.avg_pnl_after(adj.session_date, window=5)
        if after_pnl < before_pnl - 20:  # degraded by more than $20/day
            # Revert the adjustment
            db.revert_param(adj.param_key, adj.old_value)
            db.update_learning_outcome(adj.learning_id, "false_positive")
            alerts.send(f"Reverted {adj.param_key}: adjustment hurt P&L")
        elif after_pnl >= before_pnl:
            db.update_learning_outcome(adj.learning_id, "validated")
        else:
            # Neutral — extend evaluation 5 more sessions
            db.extend_evaluation(adj.learning_id, sessions=5)
```

---

## c_strategy_params Initial Values

These are seeded at project setup. The Learning Agent adjusts them over time.

| param_key | initial_value | min_bound | max_bound | cooldown_days |
|---|---|---|---|---|
| strategy_min_score | 5 | 4 | 7 | 5 |
| max_positions | 10 | 5 | 15 | 3 |
| partial_profit_pct | 0.005 | 0.003 | 0.01 | 7 |
| atr_multiplier | 0.8 | 0.5 | 1.5 | 5 |
| rr_ratio | 2.0 | 1.5 | 3.0 | 5 |
| caution_position_multiplier | 0.6 | 0.4 | 0.8 | 7 |
| caution_min_score | 7 | 6 | 9 | 5 |
| max_sector_concentration | 0.35 | 0.25 | 0.45 | 7 |

---

## Trace Entries Generated

Learning Agent uses structured log format (Format 3 — see trace-formats.md).

Each tool call produces one log line. Summary produces one c_traces row:

| step_type | description | outcome |
|---|---|---|
| tool_call | read_today_trades | null |
| tool_call | read_session_context | null |
| tool_call | read_strategy_params | null |
| tool_call | read_recent_learnings | null |
| tool_call | write_learning (0-N per session) | null |
| tool_call | adjust_param (0-2 per session) | "applied" or "rejected" |
| agent_message | Final summary JSON | "completed" or "no_trades" |

---

## Implementation Notes

```python
# agents/learning_agent.py

LEARNING_AGENT_TOOLS = [
    build_tool("read_today_trades",    read_today_trades_impl),
    build_tool("read_session_context", read_session_context_impl),
    build_tool("read_strategy_params", read_strategy_params_impl),
    build_tool("read_recent_learnings", read_recent_learnings_impl),
    build_tool("write_learning",       write_learning_impl),
    build_tool("adjust_param",         adjust_param_impl),
    build_tool("recommend_goal",       recommend_goal_impl),
]

def run_learning_agent(session_id: str, daily_perf: dict, tracer) -> dict:
    messages = [{"role": "user", "content": _build_user_message(daily_perf)}]

    while True:
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=LEARNING_AGENT_SYSTEM_PROMPT,
            messages=messages,
            tools=LEARNING_AGENT_TOOLS,
        )
        tracer.log_tokens(session_id, "learning", response.usage)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch_tool(block.name, block.input)
                    tracer.log_tool_call(session_id, "learning", block, result)
                    tool_results.append(make_tool_result(block.id, result))
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if b.type == "text")
            summary = json.loads(text)
            tracer.log_agent_message(session_id, "learning", summary, "completed")
            # Store context_for_tomorrow in c_sessions for premarket injection
            db.update_session_context(session_id, summary["context_for_tomorrow"])
            return summary
```

The tool loop terminates naturally: after reading 4 sources and writing findings, the agent will produce its summary JSON on the next turn with `stop_reason == "end_turn"`. Max tool calls expected: 4 reads + up to 5 writes = 9 calls per session.
