---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section {
    background: #0F172A;
    color: #CBD5E1;
    font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 22px;
    padding: 48px 64px;
  }
  h1 {
    color: #F59E0B;
    font-size: 1.55em;
    border-bottom: 2px solid #1E3A8A;
    padding-bottom: 6px;
    margin-bottom: 0.5em;
  }
  h2 { color: #3B82F6; font-size: 1.1em; margin: 0.3em 0 0.2em; }
  strong { color: #F59E0B; }
  em { color: #3B82F6; font-style: normal; }
  code {
    background: #1E293B;
    color: #22C55E;
    padding: 2px 7px;
    border-radius: 3px;
    font-size: 0.8em;
  }
  table { width: 100%; border-collapse: collapse; font-size: 0.82em; margin: 0.6em 0; }
  th { background: #1E3A8A; color: #93C5FD; padding: 7px 12px; text-align: left; }
  td { border-bottom: 1px solid #1E293B; padding: 6px 12px; }
  tr:nth-child(even) td { background: #111827; }
  blockquote {
    border-left: 3px solid #F59E0B;
    background: #1E293B;
    padding: 8px 16px;
    margin: 10px 0;
    color: #CBD5E1;
    font-size: 0.88em;
  }
  ul { margin: 0.3em 0; padding-left: 1.2em; }
  li { line-height: 1.65; margin: 1px 0; }
  hr { border: none; border-top: 1px solid #334155; margin: 10px 0; }
  section::after { color: #475569; font-size: 0.7em; }

  /* ── Layout helpers ── */
  .cols  { display: grid; grid-template-columns: 1fr 1fr;     gap: 1.5rem; margin-top: 0.7rem; }
  .cols3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.2rem; margin-top: 0.7rem; }
  .col60 { display: grid; grid-template-columns: 3fr 2fr;     gap: 1.5rem; margin-top: 0.7rem; }

  /* ── Cards ── */
  .card {
    background: #1E293B;
    border-radius: 6px;
    padding: 14px 18px;
    border-left: 3px solid #334155;
    font-size: 0.88em;
  }
  .card h2 { margin: 0 0 8px; font-size: 1em; }
  .card p  { margin: 3px 0; }
  .card ul { margin: 4px 0; }
  .blue   { border-left-color: #3B82F6; }
  .amber  { border-left-color: #F59E0B; }
  .green  { border-left-color: #22C55E; }
  .red    { border-left-color: #EF4444; }
  .purple { border-left-color: #A855F7; }

  /* ── Flow row ── */
  .flow {
    display: flex;
    align-items: center;
    gap: 0;
    margin: 1.2rem 0;
  }
  .fb {
    flex: 1;
    background: #1E3A8A;
    border: 1.5px solid #3B82F6;
    border-radius: 6px;
    padding: 10px 6px;
    text-align: center;
    font-size: 0.78em;
    font-weight: 600;
    color: #93C5FD;
    line-height: 1.4;
  }
  .fb.a { background: #78350F; border-color: #F59E0B; color: #FDE68A; }
  .fb.g { background: #14532D; border-color: #22C55E; color: #86EFAC; }
  .fb.r { background: #7F1D1D; border-color: #EF4444; color: #FCA5A5; }
  .arr  { flex: 0; padding: 0 8px; color: #475569; font-size: 1.1em; }

  /* ── Title slide ── */
  section.title {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    background: #0F172A;
  }
  section.title h1 { font-size: 3em; border: none; color: #FFFFFF; margin: 0; }
  section.title h2 { font-size: 1.8em; color: #F59E0B; margin: 0.15em 0; }
  section.title p  { color: #64748B; font-size: 0.85em; margin: 0.2em 0; }
---

<!-- _class: title -->

# TRADING AGENT

## Strategy C — Agentic Architecture

6 agents · Haiku + Sonnet · Learning Agent · Full trace observability

Phase 0 — Simulation · June 2026

---

# Why Strategy C

<div class="cols">
<div class="card">

## Strategy A / B (pipeline)

- Python pre-packages all context
- One Claude call receives it all
- Claude outputs a list and exits
- No tool access — no agency
- Static config, human-updated only
- Minimal trace logging

</div>
<div class="card amber">

## Strategy C (agentic)

- Agents decide what to look up via tools
- 6 specialized agents, each minimal
- Orchestrator coordinates the loop
- Research Agent runs per ticker in parallel
- Learning Agent adjusts params nightly
- Full trace log to `c_traces` from day one

</div>
</div>

> The inversion: Claude agents control information gathering instead of receiving a pre-built package.

---

# Six-Agent Roster

| Agent | Model | Language | Role | Tools |
|---|---|---|---|---|
| **Market Agent** | Haiku | Python | GO / CAUTION / SKIP + session params | 6 |
| **News Analyst** | Haiku | TypeScript | Per-candidate news *(Phase 0: stubbed)* | 2 |
| **Research Agent** | Haiku | Python ×N | Per-ticker conviction score, parallel | 4 |
| **Risk Agent** | Haiku | Python | Validates each trade proposal individually | 4 |
| **Orchestrator** | **Sonnet** | Python | Coordinates the loop, final trade decisions | 5 |
| **Learning Agent** | **Sonnet** | Python | EOD reflection, adjusts strategy params | 7 |

> Haiku for cost-sensitive data retrieval · Sonnet for reasoning, synthesis, and learning

---

# Premarket Loop

<div class="flow">
<div class="fb">Market Agent<br><small>Haiku · 6 tools</small></div>
<div class="arr">→</div>
<div class="fb">Scanner<br><small>Python · 430+</small></div>
<div class="arr">→</div>
<div class="fb">Research ×N<br><small>Haiku · parallel</small></div>
<div class="arr">→</div>
<div class="fb a">Orchestrator<br><small>Sonnet · synthesis</small></div>
<div class="arr">→</div>
<div class="fb r">Risk Agent<br><small>Haiku · 4 tools</small></div>
<div class="arr">→</div>
<div class="fb g">Orders<br><small>Alpaca bracket</small></div>
</div>

**Retry gate** — triggered when Risk Agent rejects proposals:

- All rejected → re-run Research Agent, then second synthesis (`loop_iteration = 2`)
- Some survived → re-run Risk Agent only
- CAUTION mode: retry disabled regardless

> Hard limit: max 2 loop iterations. Output: `{ trades[], retry_needed, terminal_reason }`

---

# Market Agent — Six Tools

<div class="col60">
<div class="card blue">

## Market Agent
`claude-haiku-4-5`

Pure data retrieval — Haiku chosen for cost.
No reasoning needed, just structured tool calls.

**Output:** `session_params`
→ decision: **GO** / **CAUTION** / **SKIP**
→ `max_positions`, `vix`, `fear_greed`, `sector_bias`

</div>
<div>

| Tool | Source |
|---|---|
| `get_vix()` | yfinance `^VIX` |
| `get_futures()` | yfinance ES=F, NQ=F, YM=F |
| `get_fear_greed()` | alternative.me (0–100) |
| `get_sector_etfs()` | Alpaca: XLK, XLE, XLF… |
| `get_economic_calendar()` | yfinance key events |
| `set_session_params()` | writes decision to context |

</div>
</div>

---

# Research Agent — Parallel Per Ticker

<div class="cols">
<div class="card blue">

## Research Agent
`claude-haiku-4-5`

One Haiku instance per candidate,
all running concurrently via asyncio.

Hard token budget enforced per instance.

**Output per ticker:**
- `research_summary` (text)
- `conviction_score` (0–10)

All summaries fed to Orchestrator.

</div>
<div>

**4 tools per instance:**

| Tool | Fetches |
|---|---|
| `get_price_history()` | RSI, MACD, SMA50/200, ATR |
| `get_technical_indicators()` | Computed, normalized scores |
| `get_sector_context()` | ETF vs ticker performance |
| `get_peer_comparison()` | Ticker vs its closest peers |

Each agent is independent — no shared state between parallel instances.

</div>
</div>

---

# Orchestrator + Retry

<div class="cols">
<div class="card amber">

## Orchestrator
`claude-sonnet-4-6`

Reads all research summaries and market
context. Proposes a trade list with entry,
target, stop, confidence, and rationale.

Loop iteration 1 → Risk Agent.
If retry needed → iteration 2.
Iteration 2 is terminal.

</div>
<div>

**5 tools:**

| Tool | Purpose |
|---|---|
| `load_params()` | Read `c_strategy_params` |
| `get_pending_trades()` | Avoid double-entry |
| `approve_trade()` | Add to pending queue |
| `reject_trade()` | Log rejection + reason |
| `write_session_meta()` | Write cost + outcome |

</div>
</div>

> CAUTION mode skips the retry loop entirely, regardless of `retry_needed` flag.

---

# Protection System — Six Tiers

`check_protection_status()` runs at the start of every session. Events in `c_protection_events` are immutable.

| Tier | Trigger | Action | Duration |
|---|---|---|---|
| 1 | *(reserved)* | Log only | — |
| 2 | `daily_pnl ≤ −$500` | Suspend remainder of today | End of day |
| 3 | `rolling_3d_pnl ≤ −$1,000` | Reduce `max_positions` by 50% | 2 days |
| 4 | Account drawdown ≥ 10% | Suspend all trading | 24 hours |
| **5** | Account drawdown ≥ 20% | Suspend — **human unlock required** | 7 days |
| **6** | 5+ consecutive losing days | Suspend — **human review required** | 7 days |

---

# EOD — Learning Agent

**Sequence:** Force close → Reconcile fills → Compute performance → Check protection → **Learning Agent**

<div class="cols">
<div class="card blue">

## 4 Read tools

- `read_today_trades()`
- `read_session_context()`
- `read_strategy_params()`
- `read_recent_learnings()`

</div>
<div class="card red">

## 3 Write tools

- `write_learning()` → `c_learnings`
- `adjust_param()` → `c_strategy_params`
- `recommend_goal()` → human approval req

</div>
</div>

**Guards:** min 3 trades to run · max 2 param changes/day · cooldown per param · bounds per param · safety params blocked (`daily_loss_limit`, `enable_learning_agent`)

Updated `c_strategy_params` feed into tomorrow's `load_params()`.

---

# Trace Observability — Three Formats, One Table

<div class="cols3">
<div class="card blue">

## Python JSON Traces
`TraceLogger` — all Python agents

`log_tool_call()`
`log_agent_message()`
`log_decision()`
`log_error()`

Session → agent → tool
span hierarchy

</div>
<div class="card green">

## TypeScript OTel Spans
News Analyst

`OTEL_SPAN: {json}` log lines

Fields: `agent.name`,
`session.id`, `model`

`normalize_otel_span()`
ingests into c_traces

</div>
<div class="card purple">

## Structured Log Lines
Learning Agent

`{ISO} [learning_agent]`
`session={sid} event=...`

`normalize_log_line()`
ingests into c_traces

</div>
</div>

**`c_traces`** — single normalized table: `session_id · span_id · parent_span_id · agent · tool_name · tool_input · tool_output · outcome · tokens_in · tokens_out · latency_ms · cost_usd`

---

# Data Model — 12 Tables, Three Domains

<div class="cols3">
<div>

**Execution**

- `c_sessions` — cost rollup, queue
- `c_positions` — fills, P&L, trail
- `c_scan_results` — scanner output
- `c_agent_config` — static config
- `c_market_evals` — shadow compare

</div>
<div>

**Adaptive**

- `c_strategy_params` — tunable params, cooldowns, bounds
- `c_learnings` — observations, adjustments, avoid_ticker
- `c_goals` — daily target + floor
- `c_goal_snapshots` — daily history

</div>
<div>

**Observability**

- `c_traces` — every tool call + cost
- `c_protection_events` — immutable
- `c_daily_performance` — P&L, win rate

</div>
</div>

---

# Cost Model + Deployment Phases

<div class="cols">
<div>

**Cost per session**

| Component | Est. cost |
|---|---|
| Market Agent (Haiku) | ~$0.001 |
| Research ×10 (Haiku) | ~$0.020 |
| Orchestrator (Sonnet) | ~$0.050 |
| Learning Agent (Sonnet) | ~$0.030 |
| **Total** | **~$0.10 – 0.30** |

Strategy A/B for comparison: ~$0.01 – 0.05

</div>
<div>

**Deployment phases**

**Phase 0** — Simulation. No real orders. Traces on.

**Phase 1** — Shadow alongside Strategy A. Compare outcomes.

**Phase 2** — Paper capital. 2-week validation gate.

**Phase 3** — Real capital after gate + 4-week paper run.

---

**Not in Strategy A or B:**
Tool use · Parallel per-ticker research
Retry loop · Adaptive params nightly
Full trace log from day one

</div>
</div>
