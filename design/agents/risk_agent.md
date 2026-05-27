# Risk Agent — Design Doc

**File:** `agents/risk_agent.py`  
**Model:** `claude-haiku-4-5-20251001`  
**Role:** Portfolio risk manager. Checks each proposed trade against current portfolio state and hard constraints. Approves or rejects. Does not modify proposals.  
**Runs:** Once per session (twice if Orchestrator triggers a retry).

---

## Why Haiku

Risk Agent applies a deterministic constraint checklist. The decision logic is enumerated and ordered — no ambiguity, no nuance, no tradeoffs to weigh. Haiku handles rule-following tasks well and keeps cost low on what is essentially a compliance check. The four tool calls fetch state, the constraint rules are in the prompt, the output is a verdict list. This is Haiku's home territory.

---

## System Prompt (production)

```
You are a portfolio risk manager. Your job is to review proposed trades and
approve or reject each one based on current portfolio constraints.

You have 4 tools. Call all 4 before reviewing any proposal. Do not review or
reject any trade until all 4 tool calls are complete. Call order:
  1. get_open_positions
  2. get_today_pnl
  3. get_buying_power
  4. get_portfolio_exposure

After all 4 calls, apply the constraints below in order. Stop at the first
constraint that a trade violates — do not stack multiple reasons.

────────────────────────────────────────
CONSTRAINTS (apply in order, stop at first hit per trade)
────────────────────────────────────────

CONSTRAINT 1 — Daily loss limit
  If today_pnl.limit_hit == true:
    Reject ALL trades. reason = "daily loss limit hit"
    Skip remaining constraints. Return immediately.

CONSTRAINT 2 — Duplicate position
  If proposal.ticker is in any open_positions row:
    Reject. reason = "already in open positions: {ticker}"

CONSTRAINT 3 — Position count ceiling
  approved_so_far = number of trades approved in this review (not existing positions)
  available_slots = max_positions - positions_open - approved_so_far
    (use portfolio_exposure.positions_open for positions_open)
    (max_positions = 15)
  If available_slots <= 0:
    Reject. reason = "position count limit reached ({positions_open} open + {approved_so_far} approved = {max_positions})"

CONSTRAINT 4 — Buying power
  capital_needed = sum(position_size for all proposals approved so far)
                 + proposal.position_size
  If capital_needed > buying_power.buying_power:
    Reject. reason = "insufficient buying power: need ${capital_needed:.0f}, have ${buying_power:.0f}"

CONSTRAINT 5 — Sector concentration
  proposed_sector_deployment = sum(position_size for all proposals in same sector
                                   that are APPROVED so far) + proposal.position_size
  sector_pct = (existing sector deployment + proposed_sector_deployment)
               / (total_deployed + sum of all approved proposals so far)
  If sector_pct > 0.35:
    Reject. reason = "sector concentration: {sector} would reach {sector_pct:.0%} (limit 35%)"
    (use sector from SECTOR_MAP — see note below)

────────────────────────────────────────
SECTOR MAP (approximate — use for concentration check)
────────────────────────────────────────
Technology:   AAPL, MSFT, NVDA, AMD, INTC, QCOM, TSM, AVGO, MU, AMAT, KLAC, LRCX,
              META, GOOGL, GOOG, AMZN, NFLX, SNAP, PINS, TWTR, UBER, LYFT,
              CRM, NOW, WDAY, ADBE, ORCL, SAP, INTU, FTNT, PANW, CRWD, ZS, OKTA,
              DDOG, MDB, SNOW, PLTR, COIN
Healthcare:   JNJ, PFE, MRK, ABBV, LLY, BMY, GILD, AMGN, REGN, BIIB, VRTX,
              UNH, HUM, CVS, CI, ANTM, MOH, CNC
Financials:   JPM, BAC, GS, MS, WFC, C, BLK, AXP, V, MA, PYPL, SQ
Energy:       XOM, CVX, COP, EOG, SLB, BKR, HAL, MPC, VLO, PSX
Consumer:     AMZN, TSLA, HD, LOW, TGT, WMT, COST, MCD, SBUX, NKE, YUM, CMG
Industrials:  CAT, DE, BA, LMT, RTX, GE, HON, UPS, FDX, CSX, UNP
Materials:    FCX, NEM, AA, NUE, CLF, MOS, CF
Utilities:    NEE, DUK, SO, AEP, EXC, XEL, ED
Real Estate:  AMT, PLD, EQIX, CCI, WELL, SPG, VTR
Other:        everything else

If a ticker is not in the map, use "Other". Other is not concentration-capped.

────────────────────────────────────────
IMPORTANT RULES
────────────────────────────────────────
- You may not suggest modifications to a proposal. Only APPROVED or REJECTED.
- You may not propose alternative tickers.
- A REJECTED reason must name the specific constraint violated.
- Process proposals in the order given. Approved trades count toward capital
  and slot availability for subsequent proposals in the same batch.

────────────────────────────────────────
OUTPUT
────────────────────────────────────────
Return JSON only. No commentary before or after.

{
  "verdicts": [
    {
      "ticker": str,
      "verdict": "APPROVED|REJECTED",
      "reason": str
    }
  ],
  "portfolio_state": {
    "buying_power": float,
    "positions_open": int,
    "today_pnl": float,
    "limit_hit": bool
  },
  "approved_count": int,
  "rejected_count": int
}
```

---

## Tools

### get_open_positions
**Purpose:** Fetch all currently open positions to check for duplicates and position count.  
**Called:** First, always.  
**Input:** `{}`  
**Output:**
```json
[
  {
    "ticker": "MSFT",
    "position_size": 3000,
    "entry_price": 415.20,
    "unrealized_pnl": -18.40,
    "sector": "Technology"
  }
]
```
**Failure behavior:** Return `{ "error": "..." }`. Agent must reject all proposals: `reason = "risk check unavailable: get_open_positions failed"`.

### get_today_pnl
**Purpose:** Check whether the daily loss limit has already been hit. This is the most important gate — if it's hit, nothing else matters.  
**Called:** Second, always.  
**Input:** `{}`  
**Output:**
```json
{
  "realized_pnl": -180.50,
  "trades_closed": 3,
  "loss_limit": -500.0,
  "limit_hit": false
}
```
The `loss_limit` field is the configured `DAILY_LOSS_LIMIT` from settings. `limit_hit` is pre-computed: `realized_pnl <= loss_limit`.  
**Failure behavior:** Assume `limit_hit = false`. Log in portfolio_state. Proceed with other constraints.

### get_buying_power
**Purpose:** Verify there is capital available before approving position sizes.  
**Called:** Third, always.  
**Input:** `{}`  
**Output:**
```json
{
  "buying_power": 38500.0,
  "total_capital": 50000.0,
  "deployed": 11500.0
}
```
Phase 1: `buying_power = TOTAL_CAPITAL - sum(open position sizes from DB)`.  
Phase 2: `Alpaca account.buying_power`.  
**Failure behavior:** Assume `buying_power = 0`. Reject all proposals: `reason = "risk check unavailable: buying power unknown"`.

### get_portfolio_exposure
**Purpose:** Get full portfolio sector breakdown and position count for concentration check.  
**Called:** Fourth, always.  
**Input:** `{}`  
**Output:**
```json
{
  "positions_open": 2,
  "total_deployed": 11500.0,
  "by_sector": {
    "Technology": 6500.0,
    "Healthcare": 5000.0
  },
  "max_sector_pct": 0.565
}
```
**Failure behavior:** Assume `positions_open = 0`, `total_deployed = 0`, `by_sector = {}`. Skip sector concentration check (constraint 5) — cannot compute without this data.

---

## Input Contract

Risk Agent receives `trade_proposals` as a user message from the Orchestrator:

```
User message:
"Proposed trades for today (from Research Agent):
{trade_proposals JSON}

Review against portfolio constraints and return your verdicts."
```

The `trade_proposals` JSON is the full Research Agent output including `proposals`, `skipped`, and `summary`. Risk Agent only reviews the `proposals` list.

---

## Output Contract

```json
{
  "verdicts": [
    { "ticker": "AAPL", "verdict": "APPROVED", "reason": "all constraints satisfied" },
    { "ticker": "AMD",  "verdict": "APPROVED", "reason": "all constraints satisfied" },
    { "ticker": "CRWD", "verdict": "REJECTED", "reason": "sector concentration: Technology would reach 38% (limit 35%)" }
  ],
  "portfolio_state": {
    "buying_power": 38500.0,
    "positions_open": 2,
    "today_pnl": 142.50,
    "limit_hit": false
  },
  "approved_count": 2,
  "rejected_count": 1
}
```

**Validation before passing to Orchestrator:**
- `verdicts` must be a list with one entry per proposal
- Each verdict must have `ticker`, `verdict` (APPROVED or REJECTED), `reason`
- `portfolio_state` must have all 4 fields
- If JSON parse fails: Orchestrator treats all proposals as rejected, `terminal_reason = "risk_parse_error"`

---

## Execution Flow

```
1. Orchestrator calls Risk Agent (sends trade_proposals as context)
2. Agent calls get_open_positions()
3. Agent calls get_today_pnl()
4. Agent calls get_buying_power()
5. Agent calls get_portfolio_exposure()
6. Agent applies constraints in order for each proposal
7. Agent returns risk_verdicts JSON
8. Orchestrator parses and proceeds to synthesis
```

Constraint evaluation happens **after** all 4 tool calls, processing proposals sequentially. Approval of an earlier proposal affects capital and slot availability for later ones in the same batch.

---

## Constraint Enforcement

| Constraint | How Enforced |
|---|---|
| All 4 tools required | Prompt: "Call all 4 before reviewing any proposal." |
| Call order specified | Prompt lists explicit order. |
| No tool access outside set | Registration: only these 4 tools in `tools=[]`. |
| Cannot modify proposals | Prompt: "You may not suggest modifications. Only APPROVED or REJECTED." |
| Process proposals in order | Prompt: "Process proposals in the order given." |
| 1 round only | Orchestrator does not loop Risk Agent. Single call, parse output, done. |

---

## Error Handling

| Scenario | Behavior |
|---|---|
| get_open_positions fails | Reject all proposals with "risk check unavailable" |
| get_buying_power fails | Reject all proposals with "risk check unavailable: buying power unknown" |
| get_today_pnl fails | Assume limit_hit = false; continue |
| get_portfolio_exposure fails | Skip constraint 5 (sector concentration); approve/reject on constraints 1–4 only |
| All 4 tools fail | Reject all proposals with "risk tools unavailable" |
| Agent returns invalid JSON | Orchestrator catches error, treats all proposals as rejected, `terminal_reason = "risk_parse_error"` |
| Agent times out (>30s) | Orchestrator timeout; `terminal_reason = "risk_timeout"`; treat as all rejected |
| Proposal missing from verdicts | Orchestrator treats missing proposal as REJECTED with reason "missing from risk verdict" |

---

## Trace Entries Generated

Risk Agent produces 5–6 trace rows per normal session:

| step_type | tool_name | description | outcome |
|---|---|---|---|
| tool_call | get_open_positions | Raw portfolio snapshot | null |
| tool_call | get_today_pnl | Loss limit check | null |
| tool_call | get_buying_power | Capital check | null |
| tool_call | get_portfolio_exposure | Sector breakdown | null |
| agent_message | null | Final verdict JSON | one row per proposal with `entity_id = ticker` and `outcome = "approved" or "rejected_*"` |

For the verdict trace rows, one `agent_message` row per ticker is written with `entity_id` set. This allows the trace query `WHERE session_id = X AND entity_id = 'AAPL' AND agent = 'risk'` to return AAPL's verdict and reason.

Outcome vocabulary for Risk Agent verdicts:
- `approved`
- `rejected_loss_limit`
- `rejected_duplicate`
- `rejected_count`
- `rejected_capital`
- `rejected_concentration`
- `rejected_tools_unavailable`

---

## Implementation Notes

```python
# agents/risk_agent.py

RISK_AGENT_TOOLS = [
    build_tool("get_open_positions",    get_open_positions_impl),
    build_tool("get_today_pnl",         get_today_pnl_impl),
    build_tool("get_buying_power",      get_buying_power_impl),
    build_tool("get_portfolio_exposure", get_portfolio_exposure_impl),
]

def run_risk_agent(
    session_id: str,
    trade_proposals: dict,
    tracer: TraceLogger,
) -> dict:
    """
    Runs Risk Agent tool loop until stop_reason == "end_turn".
    Returns parsed risk_verdicts dict.
    """
    user_content = (
        f"Proposed trades for today (from Research Agent):\n"
        f"{json.dumps(trade_proposals, indent=2)}\n\n"
        f"Review against portfolio constraints and return your verdicts."
    )
    messages = [{"role": "user", "content": user_content}]

    while True:
        response = anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=RISK_AGENT_SYSTEM_PROMPT,
            messages=messages,
            tools=RISK_AGENT_TOOLS,
        )
        tracer.log_tokens(session_id, "risk", response.usage)

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch_tool(block.name, block.input)
                    tracer.log_tool_call(session_id, "risk", block, result)
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": json.dumps(result)})
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if b.type == "text")
            verdicts = json.loads(text)
            # Write per-ticker trace rows
            for v in verdicts.get("verdicts", []):
                outcome = "approved" if v["verdict"] == "APPROVED" else _map_rejection(v["reason"])
                tracer.log_verdict(session_id, "risk", v["ticker"], outcome, v["reason"])
            return verdicts

        else:
            raise RiskAgentError(f"unexpected stop_reason: {response.stop_reason}")


def _map_rejection(reason: str) -> str:
    if "loss limit" in reason:      return "rejected_loss_limit"
    if "open positions" in reason:  return "rejected_duplicate"
    if "position count" in reason:  return "rejected_count"
    if "buying power" in reason:    return "rejected_capital"
    if "concentration" in reason:   return "rejected_concentration"
    return "rejected_tools_unavailable"
```

Risk Agent's tool loop terminates naturally because the system prompt instructs it to call all 4 tools, then return a JSON verdict — the same pattern as Market Agent. After 4 tool calls, the next response will be `stop_reason == "end_turn"` with the verdict JSON.
