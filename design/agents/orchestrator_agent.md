# Orchestrator Agent — Design Doc

**File:** `orchestrator.py` (session driver) + Orchestrator Agent call within it  
**Model:** `claude-sonnet-4-6`  
**Role:** Session coordinator. Calls the three specialist agents in sequence, synthesizes their outputs, and decides whether to retry or terminate. Produces the final trade list in Strategy A's exact output schema.  
**Runs:** Once at the top of every premarket run. Also invoked for the synthesis step (which may happen twice if a retry is triggered).

---

## Architecture Note

"Orchestrator" refers to two distinct things in this system:

1. **`orchestrator.py` (the session driver)** — Python code that calls agents in sequence, manages the retry loop, checks the time limit, writes the session trace. This is deterministic Python, not a Claude call.

2. **The Orchestrator Agent call** — A single Claude Sonnet call that receives all three agent reports and produces the final trade list. This is the synthesis step. It has no tools.

This doc covers both, because they are tightly coupled. The session driver controls when the Orchestrator Agent call happens and what it receives.

---

## Why Sonnet for Synthesis

The synthesis step requires reading three structured JSON reports and making a coherent final decision: which approved trades to include, whether a retry is warranted, how to map proposals to the Strategy A output schema, and what to write in `reasoning` fields. This is not mechanical — it requires judgment about whether rejections are fixable and what to say in the risk_note. Haiku tends to produce thin reasoning fields and occasionally misses schema details. Sonnet handles the mapping reliably.

---

## Session Driver Flow (`sessions/premarket.py`)

```
premarket() is called by GitHub Actions.

SESSION SETUP
  1. Check trading_days config — if today not in TRADING_DAYS, exit
  2. Check principal protection status — if suspended, exit with alert
  3. Generate session_id (uuid4)
  4. Record started_at
  5. Initialize TraceLogger(session_id)
  6. Initialize tool_call_counter = 0
  7. Load c_strategy_params (live values used by all agents)
  8. Load c_agent_config (feature flags)
  9. Load context_for_tomorrow from last Learning Agent run (injected into agent prompts)
  10. Check wall clock — if already past 10:20 AM ET, skip session:
      return trades=[], terminal_reason="time_limit"

STEP 1 — MARKET AGENT
  11. Call run_market_agent(session_id, tracer, params)
  12. Increment tool_call_counter by 4
  13. If market_report.decision == "SKIP":
      return trades=[], terminal_reason="skip_propagated"
  14. If market_report parsing fails:
      return trades=[], terminal_reason="market_parse_error"

STEP 1b — NEWS ANALYST (TypeScript subprocess)
  15. If config.get("enable_news_analyst", True):
        initial_tickers = scanner.get_top_candidates(limit=20)
        news_signals = run_news_analyst(initial_tickers, session_id)
      Else: news_signals = {}

STEP 2 — RESEARCH AGENT
  16. Check wall clock — if past 10:20 AM ET: go to TERMINAL
  17. Check tool_call_counter — if >= 40: go to TERMINAL
  18. Call run_research_agent(session_id, market_report, news_signals,
          context_for_tomorrow, params, tracer)
  19. Increment tool_call_counter by trade_proposals.tool_calls_used

STEP 3 — RISK AGENT
  14. Check wall clock — if past 10:20 AM ET: go to TERMINAL
  15. Check tool_call_counter — if >= 40: go to TERMINAL
  16. Call run_risk_agent(session_id, trade_proposals, tracer)
  17. Increment tool_call_counter by 4

STEP 4 — ORCHESTRATOR SYNTHESIS (round 1)
  18. Check wall clock — if past 10:20 AM ET: go to TERMINAL
  19. Call run_orchestrator_synthesis(session_id, market_report,
        trade_proposals, risk_verdicts, tracer, iteration=1)
  20. If synthesis.retry_needed == True and iteration < 2:
     go to STEP 2b (retry)
  21. Else: go to POST-PROCESSING

STEP 2b — RESEARCH AGENT RETRY
  22. Check wall clock — if past 10:20 AM ET: go to TERMINAL
  23. Check tool_call_counter — if >= 40: go to TERMINAL
  24. rejected_context = build_rejection_context(risk_verdicts)
  25. Call run_research_agent(session_id, market_report, tracer,
        rejected_context=rejected_context)
  26. Increment tool_call_counter by new_proposals.tool_calls_used

STEP 3b — RISK AGENT RETRY
  27. Check wall clock and tool cap
  28. Call run_risk_agent(session_id, new_proposals, tracer)
  29. Increment tool_call_counter by 4

STEP 4b — ORCHESTRATOR SYNTHESIS (round 2)
  30. Call run_orchestrator_synthesis(session_id, market_report,
        new_proposals, new_risk_verdicts, tracer, iteration=2)
  31. synthesis.retry_needed is ignored — max iterations = 2

POST-PROCESSING
  32. trades = synthesis.trades
  33. Pass trades through atr_sizer.apply()
  34. Pass trades through guardrails.check()
  35. Execute: simulation → db.insert; paper → alpaca_broker.place_orders()

TERMINAL (time or cap exceeded)
  36. Return current best state:
      If any approved trades exist from a completed synthesis: use them
      Else: trades = []
  37. terminal_reason = "time_limit" or "tool_cap"

SESSION CLOSE
  38. Write c_sessions row:
      total_steps, total_tool_calls, agents_invoked, loop_iterations,
      total_tokens, total_cost_usd, total_latency_ms,
      trades_proposed, trades_approved, trades_executed,
      retry_triggered, terminal_reason
```

---

## Orchestrator Agent System Prompt (production)

The Orchestrator Agent call has no tools. Its only job is synthesis.

```
You are a trading session coordinator. You receive structured reports from three
specialist agents and produce the final trade list for execution.

Do not second-guess the agents' analysis. Do not re-evaluate signals or re-apply
risk constraints. Your role is synthesis and schema conversion.

────────────────────────────────────────
DECISION RULES (apply in order)
────────────────────────────────────────

RULE 1 — SKIP propagation
  If market_report.decision == "SKIP":
    Return trades: [] immediately.
    terminal_reason = "skip_propagated"
    Do not read the other reports.

RULE 2 — Build approved list
  From risk_verdicts.verdicts: collect all tickers with verdict == "APPROVED".
  Match each approved ticker back to its proposal in trade_proposals.proposals.
  Build approved_trades from these matched proposals.

RULE 3 — Zero-approved handling
  If len(approved_trades) == 0:
    Read all rejection reasons.
    Structural rejections (cannot be fixed by new proposals):
      "daily loss limit hit"
      "risk check unavailable"
      "risk tools unavailable"
    If ALL rejections are structural:
      Return trades: [], retry_needed: false
      terminal_reason = "structural_block"
    If ANY rejection is fixable (sector concentration, position count,
    insufficient buying power for a specific size):
      Set retry_needed: true in your output.
      terminal_reason = "retry_triggered"
      Do NOT build a trade list — the caller will handle the retry.
      You ONLY set retry_needed: true on your first synthesis call (iteration = 1).
      On iteration = 2, retry_needed is always false.

RULE 4 — Build final trade list
  For each approved trade, convert from proposal format to execution format:
    - Copy: ticker, entry_price, target_price, stop_loss, confidence
    - action = "BUY"
    - shares = proposal.shares (or floor(position_size / entry_price) if absent)
    - position_size = proposal.position_size
    - estimated_profit = round((target_price - entry_price) * shares, 2)
    - max_loss = round((entry_price - stop_loss) * shares, 2)
    - reward_risk = round((target_price - entry_price) / (entry_price - stop_loss), 2)
    - reasoning: write 2–3 sentences. Incorporate the proposal.evidence list.
      State what signals drove this pick. Reference the market context briefly.
      Do not reproduce the JSON fields verbatim — write readable prose.

RULE 5 — Aggregate fields
  total_estimated_profit = sum(estimated_profit for all trades)
  total_max_loss = sum(max_loss for all trades)
  market_context = market_report.summary
  risk_note: 1 sentence. Note the overall portfolio state from portfolio_state.
    If any trades were rejected, briefly note the most common reason.

────────────────────────────────────────
OUTPUT SCHEMA
────────────────────────────────────────
Match this exactly. The execution system depends on it.

{
  "date": "YYYY-MM-DD",
  "market_context": str,
  "trades": [
    {
      "ticker": str,
      "action": "BUY",
      "entry_price": float,
      "target_price": float,
      "stop_loss": float,
      "position_size": float,
      "shares": int,
      "confidence": "HIGH|MEDIUM|LOW",
      "estimated_profit": float,
      "max_loss": float,
      "reward_risk": float,
      "reasoning": str
    }
  ],
  "total_estimated_profit": float,
  "total_max_loss": float,
  "risk_note": str,
  "session_meta": {
    "loop_iterations": int,
    "retry_triggered": bool,
    "retry_reason": str | null,
    "terminal_reason": str
  },
  "retry_needed": bool
}

The retry_needed field is read by the session driver and stripped before
execution. It must be present in all outputs.
```

---

## Input Contract

Orchestrator Agent receives all three reports in a single user message:

```
User message (iteration 1):
"Session reports — iteration 1:

MARKET AGENT REPORT:
{market_report JSON}

RESEARCH AGENT REPORT:
{trade_proposals JSON}

RISK AGENT REPORT:
{risk_verdicts JSON}

Today's date: {YYYY-MM-DD}
Session iteration: 1 of 2 (retry is available if needed)

Produce the final trade list."
```

```
User message (iteration 2):
"Session reports — iteration 2 (final):

MARKET AGENT REPORT:
{market_report JSON}

RESEARCH AGENT REPORT (retry):
{new_trade_proposals JSON}

RISK AGENT REPORT (retry):
{new_risk_verdicts JSON}

Today's date: {YYYY-MM-DD}
Session iteration: 2 of 2 (no further retry available)

Produce the final trade list."
```

The session driver passes `iteration` in the message so the Orchestrator Agent knows whether retry is available. On iteration 2, it cannot set `retry_needed: true`.

---

## Output Contract

```json
{
  "date": "2026-05-27",
  "market_context": "VIX at 18.4, futures +0.4%, Fear & Greed 52. Calm open, moderate bullish bias. Technology sector leading.",
  "trades": [
    {
      "ticker": "AAPL",
      "action": "BUY",
      "entry_price": 187.42,
      "target_price": 194.92,
      "stop_loss": 186.16,
      "position_size": 3500.0,
      "shares": 18,
      "confidence": "HIGH",
      "estimated_profit": 134.82,
      "max_loss": 22.68,
      "reward_risk": 4.0,
      "reasoning": "AAPL showing RS 1.8x vs SPY with price holding above VWAP at $186.90. Score 8 reflects clean MACD and volume surge. Entering on calm bullish open day with Technology sector leading XLK +0.6%."
    }
  ],
  "total_estimated_profit": 134.82,
  "total_max_loss": 22.68,
  "risk_note": "2 positions open, $38,500 buying power. CRWD rejected for sector concentration (Technology at 35% with AMD already approved).",
  "session_meta": {
    "loop_iterations": 1,
    "retry_triggered": false,
    "retry_reason": null,
    "terminal_reason": "converged"
  },
  "retry_needed": false
}
```

**Session driver validation:**
- `trades` list is validated for required fields before post-processing
- `retry_needed` is read and stripped; not passed to execution
- `terminal_reason` is written to c_sessions
- If JSON parse fails: session driver logs error, `terminal_reason = "synthesis_parse_error"`, returns `trades: []`

---

## Retry Decision Logic

The retry mechanism is the most operationally important path in the session. The session driver handles it, not the Orchestrator Agent.

```python
# orchestrator.py

def _should_retry(synthesis: dict, risk_verdicts: dict) -> tuple[bool, str | None]:
    """
    Returns (should_retry, retry_reason).
    Only retries on fixable rejections.
    """
    if not synthesis.get("retry_needed"):
        return False, None

    rejections = [v for v in risk_verdicts.get("verdicts", [])
                  if v["verdict"] == "REJECTED"]

    if not rejections:
        return False, None

    structural = {"daily loss limit hit", "risk check unavailable",
                  "risk tools unavailable"}

    all_structural = all(
        any(s in v["reason"] for s in structural)
        for v in rejections
    )

    if all_structural:
        return False, None

    fixable = [v for v in rejections
               if not any(s in v["reason"] for s in structural)]

    reasons = "; ".join(f"{v['ticker']}: {v['reason']}" for v in fixable)
    return True, reasons


def _build_rejection_context(risk_verdicts: dict) -> dict:
    """
    Builds the context dict passed to Research Agent on retry.
    """
    rejected = [v for v in risk_verdicts.get("verdicts", [])
                if v["verdict"] == "REJECTED"]
    return {
        "rejected_tickers": [v["ticker"] for v in rejected],
        "rejection_details": [{"ticker": v["ticker"], "reason": v["reason"]}
                               for v in rejected],
    }
```

---

## Time and Cap Enforcement

```python
# orchestrator.py — checked at each agent boundary

import pytz
from datetime import datetime

HARD_STOP_ET = datetime.now(pytz.timezone("America/New_York")).replace(
    hour=10, minute=20, second=0, microsecond=0
)
SESSION_TOOL_CAP = 40

def _check_limits(tool_call_counter: int, started_at: datetime) -> str | None:
    """
    Returns a terminal_reason string if a limit is exceeded, else None.
    """
    now_et = datetime.now(pytz.timezone("America/New_York"))
    if now_et >= HARD_STOP_ET:
        return "time_limit"
    if tool_call_counter >= SESSION_TOOL_CAP:
        return "tool_cap"
    return None
```

The check runs before each agent call. If a limit is hit mid-session, the driver uses whatever approved trades exist from the last completed synthesis. If no synthesis has completed, it returns `trades: []`.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Market Agent parse fails | Session ends, `terminal_reason = "market_parse_error"`, `trades = []` |
| Research Agent parse fails | Session ends, `terminal_reason = "research_parse_error"`, `trades = []` |
| Risk Agent parse fails | All proposals treated as rejected; retry logic evaluates if applicable |
| Orchestrator synthesis parse fails | Session ends, `terminal_reason = "synthesis_parse_error"`, `trades = []` |
| Time limit hit before synthesis | `terminal_reason = "time_limit"`, uses any previously approved trades or `[]` |
| Tool cap hit before synthesis | `terminal_reason = "tool_cap"`, same as time limit |
| retry_needed = true on iteration 2 | Session driver ignores it, proceeds to post-processing with whatever is approved |
| atr_sizer drops all approved trades | Post-processing returns `trades = []`; no session error — this is expected behavior |
| guardrails blocks all trades | Same as atr_sizer — legitimate outcome, not an error |

---

## Trace Entries Generated

The session driver writes trace entries across the full session. Orchestrator's own synthesis adds:

| step_type | description | outcome |
|---|---|---|
| decision | Synthesis round 1 | `converged`, `retry_triggered`, or `structural_block` |
| decision | Synthesis round 2 (if retry) | `converged` or `structural_block` |
| decision | Session terminal | `time_limit`, `tool_cap`, `skip_propagated` (if applicable) |

The c_sessions row is written at the end of the session driver, not by the Orchestrator Agent. It summarizes the full session (total_steps, total_tool_calls, total_cost, etc.).

---

## Session-Level Cost Estimate

| Step | Model | Est. tokens (input + output) | Est. cost |
|---|---|---|---|
| Market Agent | Haiku | ~1,200 + 200 | ~$0.003 |
| Research Agent | Sonnet | ~8,000 + 1,500 | ~$0.030 |
| Risk Agent | Haiku | ~2,500 + 400 | ~$0.006 |
| Orchestrator synthesis | Sonnet | ~4,000 + 600 | ~$0.013 |
| **Total (no retry)** | | | **~$0.052** |
| **Total (with retry)** | | +Research + Risk + Synthesis | **~$0.095** |

Estimates based on current Anthropic API pricing as of May 2026. Actual cost tracked per session in c_sessions.total_cost_usd.

---

## Implementation Notes

```python
# orchestrator.py — Orchestrator synthesis call

def run_orchestrator_synthesis(
    session_id: str,
    market_report: dict,
    trade_proposals: dict,
    risk_verdicts: dict,
    tracer: TraceLogger,
    iteration: int,
) -> dict:
    """
    Calls Orchestrator Agent (no tools) to synthesize agent reports.
    Returns parsed final trade list dict including retry_needed flag.
    """
    retry_note = (
        "Session iteration: 1 of 2 (retry is available if needed)"
        if iteration == 1
        else "Session iteration: 2 of 2 (no further retry available)"
    )

    user_content = (
        f"Session reports — iteration {iteration}:\n\n"
        f"MARKET AGENT REPORT:\n{json.dumps(market_report, indent=2)}\n\n"
        f"RESEARCH AGENT REPORT{'(retry)' if iteration > 1 else ''}:\n"
        f"{json.dumps(trade_proposals, indent=2)}\n\n"
        f"RISK AGENT REPORT{'(retry)' if iteration > 1 else ''}:\n"
        f"{json.dumps(risk_verdicts, indent=2)}\n\n"
        f"Today's date: {date.today().isoformat()}\n"
        f"{retry_note}\n\n"
        f"Produce the final trade list."
    )

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=ORCHESTRATOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        # No tools registered — Orchestrator does not call tools
    )

    tracer.log_tokens(session_id, "orchestrator", response.usage)
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    tracer.log_decision(session_id, "orchestrator", result)
    return result
```

Note: no tool loop — Orchestrator Agent has no tools registered. The API call will always return `stop_reason == "end_turn"` immediately.
