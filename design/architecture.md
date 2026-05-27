# Architecture Design — Trading Agent C

Status: DESIGN COMPLETE — ready for implementation

---

## Core Principle

Strategy A hands Claude everything upfront: 50 pre-filtered candidates, all signals
pre-fetched, market context pre-assembled. Claude receives a single large payload
and returns a decision. One call, done.

Strategy C inverts this. Each agent controls its own information gathering. The
Research Agent decides which tickers are worth investigating. The Market Agent
decides which macro signals it needs. The Risk Agent verifies portfolio state
before reviewing proposals. The Orchestrator synthesizes and decides whether to
iterate. Tool calls are the unit of work, not pre-packaged payloads.

Strategy C also serves as the live proof-of-concept for the AI Agent Reliability
product. It runs agents in multiple languages (Python + TypeScript), emits traces
in three different formats, and has a normalization layer that produces a unified
observability view. This is intentional — it demonstrates that the Reliability
product works on real heterogeneous systems.

---

## Phases

### Phase 0 — Simulation (current, no Alpaca needed)
- All tools work without Alpaca (yfinance + DB substitutes for live data)
- Orders written to DB only (no broker calls)
- Full trace logging from day one
- Shadow comparison against Strategy A's actual selections

### Phase 1 — Paper trading (after 10+ shadow days)
- New Alpaca paper account (separate email)
- STRATEGY_TAG = "c", order prefix = "stratc_"
- get_live_price and get_buying_power switch to Alpaca
- Eval script comparing C vs A on same days

### Phase 2 — Real money (gate: 30 paper sessions, Sharpe >= 0.8, max drawdown < 10%)
- Separate real Alpaca account
- Full principal protection active
- Learning Agent running since Phase 0

---

## Three Daily Sessions

Strategy C runs three sessions per trading day (Mon-Fri):

```
07:15 AM ET  PREMARKET SESSION  (sessions/premarket.py, also orchestrator.py)
  Market Agent → News Analyst → Research Agent → Risk Agent → Orchestrator
  Output: trade_plan executed via Alpaca bracket orders

09:15 AM – 03:50 PM ET  INTRADAY SESSION (sessions/intraday.py, every 15 min)
  Position sync → goal gates → optional new entries (if enabled)

03:55 PM ET  EOD SESSION  (sessions/eod.py)
  Force-close → reconcile → performance → protection check → goals → Learning Agent → alert
```

---

## Agent Roster

| Agent | Language | Model | Session | Trace format |
|---|---|---|---|---|
| Market Agent | Python | Haiku | Premarket | Custom JSON |
| News Analyst | TypeScript | Haiku | Premarket | OTel spans |
| Research Agent | Python | Sonnet | Premarket + optional intraday | Custom JSON |
| Risk Agent | Python | Haiku | Premarket + optional intraday | Custom JSON |
| Orchestrator | Python | Sonnet | Premarket synthesis | Custom JSON |
| Learning Agent | Python | Sonnet | EOD | Structured logs + JSON summary |

---

## Premarket Execution Flow

```
[PREMARKET START — target complete by 10:20 AM ET]

0. GUARD CHECKS
   - Check principal protection status (any suspension active?)
   - Check trading_days config — if not a trading day, exit
   - Load c_strategy_params (live values, not hardcoded)
   - Load c_agent_config (feature flags)
   - Inject context_for_tomorrow from last Learning Agent run

1. Market Agent runs
   - Calls get_vix, get_futures, get_fear_greed, get_sector_rotation
   - All 4 calls required, no skipping
   - Returns market_report {decision, max_positions, bias, summary}
   - If decision == SKIP: session ends, trades=[], terminal_reason=skip_propagated

2. News Analyst runs (TypeScript subprocess)
   - Receives initial candidate list tickers
   - Fetches yfinance news for each
   - Returns news_signals [{ticker, signal, blackout}]
   - OTel spans emitted to stdout, normalized and written to c_traces
   - If News Analyst fails: session continues without news signals

3. Research Agent runs (receives market_report + news_signals as context)
   - Calls get_candidates → scores+price only
   - Selects 5 tickers to investigate
   - For each: get_news, get_live_price, get_intraday_signals, get_atr, get_position_history
   - Drops blackout tickers, applies CAUTION rules if market_signal == CAUTION
   - Returns trade_proposals [{ticker, entry, target, stop, confidence, evidence}]

4. Risk Agent runs (receives trade_proposals)
   - Calls get_open_positions, get_today_pnl, get_buying_power, get_portfolio_exposure
   - All 4 required before reviewing
   - Applies 5 constraints in order: loss_limit → duplicate → count → capital → concentration
   - Returns risk_verdicts [{ticker, verdict, reason}]

5. Orchestrator synthesizes
   - Reads all three reports
   - If >0 approved: build final trade list
   - If 0 approved + fixable: 1 retry (Research Agent gets rejection context)
   - If 0 approved + structural: return trades=[], terminal_reason=structural_block
   - Adds session_meta: loop_iterations, retry_triggered, terminal_reason

6. Post-processing
   atr_sizer → guardrails → (simulation: DB write) / (paper/live: Alpaca order)

7. Trace written
   c_sessions row summarizing the full session
   c_traces rows: one per tool call + one per agent message + one per decision
```

---

## Goal System

Goals stored in c_goals, not hardcoded. The session driver reads active goals at startup.

Principal protection is separate — hardcoded in `core/protection.py`, not configurable
by the Learning Agent.

```
Principal protection tiers (hardcoded):
  Tier 1: daily_pnl < -$200     → reduce new entries to 1, log
  Tier 2: daily_pnl < -$500     → stop day
  Tier 3: rolling 3-day < -$1k  → max_positions * 0.5 for 2 days
  Tier 4: drawdown > 10%        → suspend 24h, alert
  Tier 5: drawdown > 20%        → suspend 7 days, human unlock required
  Tier 6: 5 consecutive losses  → suspend, human review required

Configurable goals (c_goals):
  daily_pnl_target  → lock-in mode when hit (no new entries)
  daily_pnl_floor   → extra caution gate
  weekly_pnl_target → tracked by Learning Agent, informs Friday behavior
  win_rate_target   → Learning Agent scores this weekly
```

---

## Caps and Limits

| Constraint | Value | Enforcement |
|---|---|---|
| Market Agent tool calls | 4 (all required) | Prompt + tool implementation |
| Research Agent get_candidates | 1 | Tool implementation (errors on 2nd call) |
| Research Agent deep-dives | max 5 tickers (premarket), max 2 (intraday) | Prompt + context |
| Research Agent total tool calls | 25 max | Prompt + MAX_TOOL_CALLS check |
| Risk Agent tool calls | 4 (all required) | Prompt |
| Orchestrator retries | 1 | Prompt + session driver iteration counter |
| Session total tool calls | 40 hard cap | Session driver counter |
| Session time limit | 10:20 AM ET | Session driver wall clock check |
| Intraday new entries | before 1:00 PM ET only | intraday.py time check |
| Learning Agent adjustments | 2 per day max | Prompt + tool enforcement |

---

## Tool Data Sources by Phase

| Tool | Phase 0 (simulation) | Phase 1+ (paper/live) |
|---|---|---|
| get_vix | yfinance ^VIX | yfinance ^VIX |
| get_futures | yfinance ES=F, NQ=F, YM=F | yfinance |
| get_fear_greed | alternative.me API | alternative.me |
| get_sector_rotation | yfinance sector ETFs | yfinance |
| get_candidates | scanner (yfinance) | scanner |
| get_news | yfinance news | yfinance news |
| get_ticker_news (News Analyst) | yfinance news | yfinance news |
| get_live_price | yfinance 1-min close | Alpaca ask price |
| get_intraday_signals | yfinance 1-min bar calc | Alpaca bars API |
| get_atr | yfinance daily bars | yfinance |
| get_position_history | Supabase c_positions | Supabase |
| get_open_positions | Supabase c_positions | Supabase |
| get_today_pnl | Supabase c_trades | Supabase |
| get_buying_power | TOTAL_CAPITAL - deployed | Alpaca account.buying_power |
| get_portfolio_exposure | Supabase c_positions | Supabase |

3 of 15 tools change when switching to paper/live trading.

---

## Infrastructure

| Decision | Choice | Rationale |
|---|---|---|
| Supabase project | Separate from A/B | C schema diverges; traces must not pollute A/B |
| Alpaca account | New paper account, separate email | No order collisions with A/B (prefix: stratc_) |
| GitHub repo | trading-agent-c | Clean isolation; no shared CI/CD |
| Trading days | Mon-Fri (c_agent_config) | Configurable; default is full week |
| TypeScript runtime | Node.js 20 (installed in GitHub Actions) | News Analyst + future TS agents |

---

## Folder Structure

```
trading-agent-c/
├── sessions/
│   ├── premarket.py             — Premarket session driver (calls agents in sequence)
│   ├── intraday.py              — Intraday polling service (every 15 min)
│   └── eod.py                   — EOD session driver (force-close, reconcile, learn)
├── agents/
│   ├── market_agent.py          — Python, Haiku
│   ├── research_agent.py        — Python, Sonnet
│   ├── risk_agent.py            — Python, Haiku
│   ├── learning_agent.py        — Python, Sonnet
│   ├── ts/                      — TypeScript agents
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── news_analyst.ts      — TypeScript, Haiku, OTel traces
│   │   └── tests/
│   │       ├── news_analyst.test.ts
│   │       └── otel_spans.test.ts
│   └── tools/
│       ├── market_tools.py
│       ├── research_tools.py
│       ├── risk_tools.py
│       ├── learning_tools.py
│       └── news_tools_helper.py — Python helper called by TypeScript News Analyst
├── core/
│   ├── db.py
│   ├── alerts.py
│   ├── protection.py            — Principal protection checks (all 6 tiers)
│   ├── goals.py                 — Goal evaluation + lock-in logic
│   └── params.py                — c_strategy_params loader (DB-backed)
├── trace/
│   ├── logger.py                — Custom JSON writer (c_traces, c_sessions)
│   └── normalizer.py            — OTel + structured log → c_traces
├── scanner/                     — Candidate scanner (adapted from Strategy A)
├── config/
│   ├── settings.py              — Static config (TOTAL_CAPITAL, order prefix, etc.)
│   └── agent_config.py          — c_agent_config loader
├── post_processing/
│   ├── atr_sizer.py             — Position sizing (adapted from Strategy A)
│   └── guardrails.py            — Final safety gate (adapted from Strategy A)
├── tests/                       — Full test suite (see testing.md)
│   ├── conftest.py
│   ├── fixtures/
│   ├── agents/
│   ├── sessions/
│   ├── core/
│   ├── trace/
│   └── tools/
├── design/                      — All design docs
├── eval_c.py                    — Shadow comparison: C vs A selections
├── requirements.txt
├── .env.example
├── generate_agent_docs.py       — Markdown → docx for all agent docs
└── .github/workflows/
    ├── strategy_c_premarket.yml — Mon-Fri 7:15 AM ET (12:15 UTC)
    ├── strategy_c_intraday.yml  — Mon-Fri every 15 min 9:15 AM-3:50 PM ET
    ├── strategy_c_eod.yml       — Mon-Fri 3:55 PM ET (20:55 UTC)
    └── tests.yml                — On push/PR: pytest + jest
```

---

## Design Documents Index

| File | Covers |
|---|---|
| agents/market_agent.md | Market Agent: tools, system prompt, output contract |
| agents/research_agent.md | Research Agent: two-phase prompt, tool set, output contract |
| agents/risk_agent.md | Risk Agent: 5 constraints, tools, output contract |
| agents/orchestrator_agent.md | Orchestrator: session driver + synthesis agent |
| agents/learning_agent.md | Learning Agent: pattern analysis, param adjustment, fail-fast rules |
| agents/news_analyst.md | News Analyst (TypeScript): OTel traces, sentiment classification |
| sessions/intraday_session.md | Intraday: polling loop, lock-in mode, optional entries |
| sessions/eod_session.md | EOD: force-close, reconcile, performance, Learning Agent trigger |
| trace-schema.md | c_traces and c_sessions column definitions |
| trace-formats.md | Three trace formats + normalization spec |
| schema.md | All DB tables: DDL, seed data, common queries |
| testing.md | Test strategy, mock approach, fixture standards, CI setup |
