# Strategy C — Architecture

True multi-agent agentic system. Six specialized Claude agents coordinate through a shared session context, each with its own tool set and a hard turn cap. Built trace-first: every tool call, agent message, and decision is logged to `c_traces` for full observability. Three daily sessions: premarket, intraday polling, and EOD analysis with adaptive learning.

---

## Daily Schedule

```
 7:15 AM ET   premarket.yml            Market scan → Research → Risk → Orchestrator → orders
 9:15–3:50 PM intraday.yml             Every 15 min: sync positions, optional new entries
 3:55 PM ET   eod.yml                  Force-close → Learning Agent → param updates
 Every hour   watchdog.yml             Kill orphaned in-progress sessions > 60 min old
```

---

## Agent Roster

```mermaid
flowchart LR
    subgraph "Premarket & Intraday"
        MA["Market Agent\nmarket_agent_shadow.py\nclaude-haiku-4-5\n6 mandatory tools\nDecision: GO|CAUTION|SKIP"]:::haiku
        RA["Research Agent\nresearch_agent.py\nclaude-haiku-4-5\n4 tools per ticker\nParallel mini-agents\n(ThreadPoolExecutor)\nup to 6 tickers × 120s"]:::haiku
        RKA["Risk Agent\nrisk_agent.py\nclaude-haiku-4-5\n4 mandatory tools\nPortfolio constraint check"]:::haiku
        OA["Orchestrator\norchestrator.py\nclaude-sonnet-4-6\n0 tools (synthesis only)\n1 optional retry"]:::sonnet
    end

    subgraph "EOD"
        LA["Learning Agent\nlearning_agent.py\nclaude-sonnet-4-6\n7 tools\nParam tuning with cooldown\nPattern recognition"]:::sonnet
    end

    subgraph "Phase 0: Stubbed"
        NA["News Analyst\nts/news_analyst.ts\nclaude-haiku-4-5\nTypeScript / OTel spans\nCurrently returns empty list"]:::stub
    end

    subgraph "Shadow (comparison only)"
        MAV1["Market Agent V1\nmarket_agent.py\nclaude-haiku-4-5\n4 tools\nLogs to c_market_evals\nNot used for decisions"]:::shadow
    end

    MA --> OA
    RA --> OA
    RKA --> OA
    OA --> LA

    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef sonnet fill:#fef3c7,stroke:#f59e0b,color:#78350f
    classDef stub fill:#e5e7eb,stroke:#9ca3af,color:#374151
    classDef shadow fill:#f3e8ff,stroke:#a855f7,color:#4a044e
```

---

## Premarket Pipeline

```mermaid
flowchart TD
    GHA["GitHub Actions\n7:15 AM ET"]:::infra --> PM["sessions/premarket.main()"]

    PM --> GUARD["Pre-checks\n─────────────────\ncheck_protection_status() ← c_protection_events\nis_trading_day() ← c_agent_config\nload_params() ← c_strategy_params\n_existing_session_guard() ← c_sessions\n(skips if completed session today)"]:::filter

    GUARD --> SCANNER["scanner.run_scanner()\n─────────────────\nyfinance bars per universe\nWrites c_scan_results\n{ticker, score, price, sector}"]:::agent

    SCANNER --> MA["① Market Agent Shadow\nrun_market_agent_shadow()\n─────────────────\nModel: claude-haiku-4-5\nPre-LLM circuit breakers (Python):\n  VIX > 35 → SKIP\n  avg futures < -2% → SKIP\n  all 3 indices < -1% → SKIP\n6 tools called (all mandatory)"]:::haiku

    MA --> MAGATE{decision?}
    MAGATE -->|SKIP| HALT["Log to c_sessions\nterminal_reason=market_skip\nSend alert"]:::filter
    MAGATE -->|GO / CAUTION| PREGATE

    PREGATE["Pre-research gate\nget_candidates() ← c_scan_results\nSkip research if no candidates"]:::filter --> RA

    RA["② Research Agent\nrun_research_agent()\n─────────────────\nModel: claude-haiku-4-5\nPhase 1: deterministic screening\n  get_candidates() + get_gap_up_tickers()\n  get_premarket_snapshot()\n  Select up to 6 tickers\nPhase 2: parallel mini-agents\n  ThreadPoolExecutor\n  360s wall-clock · 120s per ticker\n  4 tools per ticker (in order)"]:::haiku

    RA --> RKA["③ Risk Agent\nrun_risk_agent()\n─────────────────\nModel: claude-haiku-4-5\n4 mandatory tools\nConstraints in order:\n  loss_limit → duplicate\n  → capital → concentration\n  → position count"]:::haiku

    RKA --> OA["④ Orchestrator\n_run_synthesis_call()\n─────────────────\nModel: claude-sonnet-4-6\nNo tools — pure synthesis\nInput: market + research + risk\nOutputs: trades[] + session_meta\nretry_needed flag"]:::sonnet

    OA --> RETRY{retry_needed\nAND not CAUTION?}
    RETRY -->|yes| RERUN["Re-run Research (if all rejected)\nor re-run Risk (if some survived)\nthen second synthesis\nloop_iteration=2"]:::filter
    RETRY -->|no| EXEC
    RERUN --> EXEC

    EXEC["⑤ _execute_trades()\n─────────────────\nOnly if now ≥ 9:30 AM ET\nElse: store in c_sessions.pending_trades\nfor intraday 9:45 AM execution"]:::agent

    EXEC --> BRACKET["submit_bracket_order()\n─────────────────\nAlpaca LimitOrderRequest\nOrderClass.BRACKET\n  entry: limit (bid-adjusted)\n  take-profit: limit leg\n  stop-loss: leg\nPolls 30s for fill\nReturns (order_id, fill_price)\nPrefix: stratc_{ticker}_{ts}"]:::infra

    BRACKET --> TRAIL["submit_trailing_stop()\n─────────────────\n_cancel_bracket_stop_leg(order_id)\nAlpaca TrailingStopOrderRequest\ntrain_percent = trail_pct × 100\nReturns trail_order_id"]:::infra

    TRAIL --> DBWRITE[("c_positions INSERT\nsession_id · ticker · entry_price\ntarget_price · stop_loss\nshares · position_size\nconfidence · entry_context\nalpaca_order_id · trail_order_id")]:::db

    DBWRITE --> MAV1["⑥ Market Agent V1 (shadow)\n─────────────────\n4 tools (subset of V2)\nResults written to c_market_evals\nFor V1 vs V2 comparison only\nDoes not affect trades"]:::shadow

    classDef agent fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef sonnet fill:#fef3c7,stroke:#f59e0b,color:#78350f
    classDef filter fill:#f3e8ff,stroke:#a855f7,color:#4a044e
    classDef infra fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef db fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
    classDef shadow fill:#e5e7eb,stroke:#9ca3af,color:#374151
```

---

## Tools by Agent

### Market Agent Shadow — 6 Tools

```mermaid
flowchart LR
    MA["Market Agent\nclaude-haiku-4-5"]:::haiku

    MA --> T1["get_vix()\n→ {value, level}\nyfinance ^VIX"]:::tool
    MA --> T2["get_futures()\n→ {S&P500, Nasdaq, Dow\n   avg_change_pct, bias}\nyfinance ES=F NQ=F YM=F"]:::tool
    MA --> T3["get_fear_greed()\n→ {value 0–100, classification}\nalternative.me API"]:::tool
    MA --> T4["get_sector_rotation()\n→ [{ticker, change_pct} × 11]\nAlpaca day bars\nXLK XLV XLF XLE XLI\nXLY XLP XLB XLU XLRE XLC"]:::tool
    MA --> T5["get_economic_calendar()\n→ {has_high_impact, events[]}\nForexFactory JSON feed"]:::tool
    MA --> T6["get_treasury_yields()\n→ {yield_10y, change_bp, direction}\nyfinance ^TNX"]:::tool

    MA --> OUT["Output\n─────────────────\n{decision: GO|CAUTION|SKIP\n max_positions: int\n bias: BULLISH|BEARISH|NEUTRAL\n confidence: HIGH|MEDIUM|LOW\n key_factors: [str × 3]\n summary: str\n skip_reason: str|null}"]:::out

    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef tool fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef out fill:#fef3c7,stroke:#f59e0b,color:#78350f
```

### Research Agent — 4 Tools per Ticker (Parallel)

```mermaid
flowchart TD
    RA["Research Agent\nclaude-haiku-4-5\nOrchestrates parallel mini-agents"]:::haiku

    SCREEN["Deterministic screening\n(before LLM calls)\n─────────────────\nget_candidates() ← c_scan_results\nget_gap_up_tickers() ← Alpaca movers ≥2%\nget_premarket_snapshot()\n  ← Alpaca StockLatestQuoteRequest\nSelect up to 6 tickers\nApply CAUTION filter: score≥7 + pmkt≥0.3%"]:::filter

    SCREEN --> TP["ThreadPoolExecutor\n360s total · 120s per ticker\nUp to 6 parallel mini-agents"]

    TP --> T1["① get_news(ticker)\n→ {blackout: bool, headlines: []}\nyfinance .calendar + .news\n(SKIP if blackout=true)"]:::tool
    TP --> T2["② get_ticker_fundamentals(ticker)\n→ {float_shares_m, short_pct_float\n   low_float, squeeze_potential\n   prev_day_high, low, close, range_pct}\nyfinance .info + .history(5d)"]:::tool
    TP --> T3["③ get_ticker_market_data(ticker)\n→ {atr_pct, avg_daily_volume\n   premarket_volume, conviction\n   above_vwap, vwap, rs_vs_spy\n   today_pct_change, orb_pct, live_price}\nAlpaca daily + premarket + intraday bars"]:::tool
    TP --> T4["④ get_position_history(ticker, days=30)\n→ {win_rate, avg_pnl}\nc_positions table"]:::tool

    T1 & T2 & T3 & T4 --> PROP["Per-ticker proposal\n─────────────────\n{ticker, entry_price\n target_price (entry × 1.08)\n stop_loss, position_size\n shares, confidence: H|M|L\n evidence: [str]}"]:::out

    PROP --> RAOUT["Research output\n─────────────────\n{proposals: []\n skipped: [{ticker, reason}]\n summary: str}"]:::out

    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef filter fill:#f3e8ff,stroke:#a855f7,color:#4a044e
    classDef tool fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef out fill:#fef3c7,stroke:#f59e0b,color:#78350f
```

### Risk Agent — 4 Tools

```mermaid
flowchart LR
    RKA["Risk Agent\nclaude-haiku-4-5"]:::haiku

    RKA --> T1["get_open_positions()\n→ [{ticker, position_size\n    entry_price, unrealized_pnl, sector}]\nc_positions (status=open)"]:::tool
    RKA --> T2["get_today_pnl()\n→ {realized_pnl, trades_closed\n   loss_limit, limit_hit}\nc_positions (closed today)"]:::tool
    RKA --> T3["get_buying_power()\n→ {buying_power, total_capital, deployed}\nc_positions + c_agent_config"]:::tool
    RKA --> T4["get_portfolio_exposure()\n→ {positions_open, total_deployed\n   by_sector: {sector: pct}\n   max_sector_pct}\nc_positions + c_agent_config"]:::tool

    RKA --> OUT["Output per proposal\n─────────────────\n{ticker\n verdict: APPROVED|REJECTED\n reason: str}"]:::out

    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef tool fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef out fill:#fef3c7,stroke:#f59e0b,color:#78350f
```

### Learning Agent — 7 Tools (EOD only)

```mermaid
flowchart LR
    LA["Learning Agent\nclaude-sonnet-4-6\nEOD only · 2 param adjustments/day\n1 goal recommendation/day\nMin 3 sample trades required"]:::sonnet

    LA --> R1["read_today_trades()\n← c_positions (today)"]:::rtool
    LA --> R2["read_session_context()\n← c_sessions (today)"]:::rtool
    LA --> R3["read_strategy_params()\n← c_strategy_params"]:::rtool
    LA --> R4["read_recent_learnings()\n← c_learnings (last 14d)"]:::rtool

    LA --> W1["write_learning()\n→ c_learnings\ntypes: observation\nadjustment · avoid_ticker"]:::wtool
    LA --> W2["adjust_param()\n→ c_strategy_params\nvia core.params.adjust_param()\nCooldown enforced\nBounds enforced\nSafety params blocked"]:::wtool
    LA --> W3["recommend_goal()\n→ c_learnings\ntype=goal_recommendation\n(human approval required)"]:::wtool

    classDef sonnet fill:#fef3c7,stroke:#f59e0b,color:#78350f
    classDef rtool fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef wtool fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
```

---

## Agent Handshakes — Full Sequence

```mermaid
sequenceDiagram
    participant GHA as GitHub Actions
    participant PRE as premarket.py
    participant MA as Market Agent
    participant RA as Research Agent
    participant RKA as Risk Agent
    participant ORC as Orchestrator
    participant ALP as Alpaca API
    participant DB as Supabase DB
    participant TRC as TraceLogger

    GHA->>PRE: trigger premarket (7:15 AM ET)
    PRE->>DB: check c_sessions (concurrent guard)
    PRE->>DB: load c_strategy_params
    PRE->>DB: check c_protection_events

    PRE->>PRE: scanner.run_scanner()
    PRE->>DB: write c_scan_results

    PRE->>MA: run_market_agent_shadow()
    Note over MA: Circuit breakers checked BEFORE LLM
    MA->>MA: get_vix() → yfinance ^VIX
    MA->>MA: get_futures() → yfinance ES=F NQ=F YM=F
    MA->>MA: get_fear_greed() → alternative.me
    MA->>MA: get_sector_rotation() → Alpaca 11 ETFs
    MA->>MA: get_economic_calendar() → ForexFactory
    MA->>MA: get_treasury_yields() → yfinance ^TNX
    MA-->>TRC: log 6 tool calls + decision
    MA-->>PRE: {decision, max_positions, bias, key_factors}

    alt decision == SKIP
        PRE->>DB: update c_sessions (terminal_reason=market_skip)
        PRE-->>GHA: exit
    end

    PRE->>RA: run_research_agent(market_report)
    Note over RA: Deterministic screening first
    RA->>DB: get_candidates() from c_scan_results
    RA->>ALP: get_gap_up_tickers() via ScreenerClient
    RA->>ALP: get_premarket_snapshot() batch quotes

    Note over RA: Parallel mini-agents (up to 6 tickers)
    par Per ticker (360s total budget)
        RA->>RA: get_news() → yfinance calendar+news
        RA->>RA: get_ticker_fundamentals() → yfinance info
        RA->>RA: get_ticker_market_data() → Alpaca bars
        RA->>DB: get_position_history() → c_positions
    end
    RA-->>TRC: log per-ticker tool calls + proposals
    RA-->>PRE: {proposals[], skipped[]}

    PRE->>RKA: run_risk_agent(proposals)
    RKA->>DB: get_open_positions() → c_positions
    RKA->>DB: get_today_pnl() → c_positions
    RKA->>DB: get_buying_power() → c_positions+config
    RKA->>DB: get_portfolio_exposure() → c_positions
    RKA-->>TRC: log 4 tool calls + verdicts
    RKA-->>PRE: {verdicts[], portfolio_state}

    PRE->>ORC: _run_synthesis_call(market+research+risk)
    Note over ORC: claude-sonnet-4-6, no tools
    ORC-->>TRC: log synthesis decision
    ORC-->>PRE: {trades[], retry_needed, terminal_reason}

    opt retry_needed AND not CAUTION
        Note over PRE: Re-run risk (if some proposals survived)<br/>or re-run research (if all rejected)
        PRE->>RKA: run_risk_agent(retry_proposals)
        PRE->>ORC: _run_synthesis_call() [loop_iteration=2]
    end

    loop Each approved trade
        PRE->>ALP: submit_bracket_order()
        ALP-->>PRE: (order_id, fill_price)
        PRE->>ALP: _cancel_bracket_stop_leg(order_id)
        PRE->>ALP: submit_trailing_stop(trail_pct)
        PRE->>DB: insert c_positions (OPEN)
        PRE-->>TRC: log execution
    end

    PRE->>DB: update c_sessions (completed)
    PRE->>PRE: send_alert() → Gmail
```

---

## Intraday Session

```mermaid
flowchart TD
    GHA["Every 15 min\n9:15 AM–3:50 PM ET"]:::infra --> ID["sessions/intraday.main()"]

    ID --> SESS["get_today_session_id()\n─────────────────\nReads c_sessions WHERE date=today\nIf missing before 10:30 AM:\n  dispatch to premarket.main()\nIf missing after: exit"]:::agent

    SESS --> SYNC["_sync_positions(session_id)\n─────────────────\nRead c_positions (open, today)\nFor each position vs Alpaca:\n  entry not filled yet?\n    → get_bracket_status(order_id)\n    → backfill fill_price\n    → cancel bracket stop leg\n    → submit_trailing_stop()\n  not in Alpaca at all?\n    → get_order_fill(trail_order_id)\n    → mark CLOSED with exit_reason\n    → realized_pnl = (fill - entry) × shares"]:::agent

    SYNC --> GOALS["evaluate_goals(daily_pnl)\n─────────────────\nRead c_goals\nlock_in_mode: pnl ≥ daily_target\npnl_floor_hit: pnl ≤ daily_floor"]:::filter

    GOALS --> GATES{"All gates\npass?"}
    GATES -->|"lock_in_mode OR\npnl_floor_hit OR\nintraday disabled OR\npast 1:00 PM ET OR\nno capacity"| DONE["Exit — no new entries"]:::filter

    GATES -->|pass| RAENTRY["run_research_agent()\n─────────────────\nSynthetic GO report\nscore+1 bonus for\nintraday momentum\nSame parallel tool flow\nas premarket"]:::haiku

    RAENTRY --> RKAENTRY["run_risk_agent()"]:::haiku
    RKAENTRY --> TRADE["_place_intraday_trades()\n─────────────────\nSame bracket + trail flow\nentry_context='intraday'\nWrites c_positions"]:::infra

    classDef agent fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef haiku fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef filter fill:#f3e8ff,stroke:#a855f7,color:#4a044e
    classDef infra fill:#dcfce7,stroke:#22c55e,color:#14532d
```

---

## EOD Session + Learning Agent

```mermaid
flowchart TD
    GHA["3:55 PM ET"]:::infra --> EOD["sessions/eod.main()"]

    EOD --> REC["reconcile_positions(session_id)\n─────────────────\nFor each open c_position:\n  get_bracket_status(order_id)\n  get_order_fill(trail_order_id)\n  Update entry_price if filled differently\n  Mark CLOSED if exit leg filled\n  Calculate realized_pnl"]:::agent

    REC --> FORCE["force_close_positions()\n─────────────────\nSnapshot unrealized_pnl via Alpaca\ncancel_all_orders() — clear brackets\nclose_all_strategy_positions()\n  (market sell, stratc_ prefix)\nDetermine realized_pnl:\n  fill_price > alpaca_snapshot > 0.0\nMark: status=closed, exit_reason=eod_forced"]:::agent

    FORCE --> PERF["compute_performance(session_id)\n─────────────────\nRead all closed c_positions for today\nCompute: realized_pnl · trades_won\n  trades_lost · win_rate\n  largest_win · largest_loss\n  avg_hold_min\nWrite c_daily_performance"]:::agent

    PERF --> PROT["check_protection_status()\n─────────────────\n6 tiers checked:\n  T2: daily_pnl ≤ -$500 → suspend\n  T3: rolling_3d ≤ -$1000 → reduce\n  T4: drawdown ≥ 10% → suspend 24h\n  T5: drawdown ≥ 20% → suspend 7d\n  T6: 5 consecutive losing days → 7d\nWrites c_protection_events (immutable)"]:::agent

    PROT --> GOALS["update_goal_progress(pnl)\nrecord_goal_snapshots(date, pnl)\nWrites c_goals · c_goal_snapshots"]:::agent

    GOALS --> LA["Learning Agent\nrun_learning_agent()\n─────────────────\nModel: claude-sonnet-4-6\n7 tools: 4 read + 3 write\nReads last 5 days + today\nMay write:\n  c_learnings (observations)\n  c_strategy_params (with cooldown)\n  c_learnings (goal recommendations\n    → require human approval)"]:::sonnet

    LA --> CLOSE["tracer.close_session()\nsend_alert() → Gmail"]:::infra

    classDef agent fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef sonnet fill:#fef3c7,stroke:#f59e0b,color:#78350f
    classDef infra fill:#dcfce7,stroke:#22c55e,color:#14532d
```

---

## Observability & Trace System

```mermaid
flowchart LR
    subgraph "Python Agents"
        TL["TraceLogger\ntrace/logger.py\n─────────────────\nlog_tool_call()\nlog_agent_message()\nlog_decision()\nlog_error()\n─────────────────\nSpan hierarchy:\nsession → agent → tool\nparent_span_id links\nentity_id: research_{TICKER}\n─────────────────\nCost tracking per agent:\nHaiku: $0.80/$4.00 per Mtok\nSonnet: $3.00/$15.00 per Mtok\nflush_cost_breakdown() after each agent"]:::logger
    end

    subgraph "TypeScript News Analyst"
        TS["OTel spans\n─────────────────\nEmits: OTEL_SPAN: {json}\nRequired attrs:\n  agent.name · agent.language\n  session.id · model\nCorrelated via session.id\nIngested by:\n  tracer.ingest_otel_span()\n  trace/normalizer.normalize_otel_span()"]:::otel
    end

    subgraph "Learning Agent"
        LOG["Structured log lines\n─────────────────\nFormat: {ISO} [learning_agent]\n  session={sid} event=...\nNormalized by:\n  trace/normalizer.normalize_log_line()"]:::logfmt
    end

    TL & TS & LOG --> DB[("c_traces\n─────────────────\nsession_id · span_id · parent_span_id\nentity_id · sequence\nagent · step_type · tool_name\ntool_input · tool_output\nagent_reasoning · outcome\ntokens_input · tokens_output\nlatency_ms · model · error")]:::db

    DB --> SESS[("c_sessions\n─────────────────\ntotal_steps · total_tool_calls\nagents_invoked · loop_iterations\ntotal_cost_usd · cost_breakdown\ntotal_tokens · total_latency_ms")]:::db

    classDef logger fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef otel fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef logfmt fill:#f3e8ff,stroke:#a855f7,color:#4a044e
    classDef db fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
```

**Outcome vocabulary** (controlled, not free-text):

| Agent | Outcomes |
|---|---|
| Market | `go` · `caution` · `skip` |
| Research | `proposed` · `skipped_blackout` · `skipped_below_vwap` · `skipped_atr` · `skipped_score` · `skipped_price_moved` |
| Risk | `approved` · `rejected_loss_limit` · `rejected_capital` · `rejected_concentration` · `rejected_duplicate` · `rejected_count` |
| Orchestrator | `converged` · `retry_triggered` · `structural_block` · `time_limit` · `tool_cap` · `skip_propagated` · `caution_no_retry` |

---

## Data Model

```mermaid
erDiagram
    c_sessions {
        string id PK
        date date
        string terminal_reason
        int total_steps
        int total_tool_calls
        json agents_invoked
        int loop_iterations
        float total_cost_usd
        json cost_breakdown "per agent"
        int trades_proposed
        int trades_approved
        int trades_executed
        bool retry_triggered
        json pending_trades "pre-9:30 AM queue"
        timestamp started_at
        timestamp completed_at
    }

    c_positions {
        string id PK
        string session_id FK
        string ticker
        string status "open|closed"
        float entry_price
        float exit_price
        float position_size
        int shares
        float target_price
        float stop_price
        float realized_pnl
        string exit_reason "NATIVE_TRAIL|TARGET|STOP|eod_forced|unfilled"
        string entry_context "premarket|intraday"
        string alpaca_order_id
        string trail_order_id
        date open_date
        date close_date
    }

    c_traces {
        string id PK
        string session_id FK
        string span_id
        string parent_span_id
        string entity_id "research_{TICKER}"
        int sequence
        string agent
        string step_type "tool_call|agent_message|decision|error"
        string tool_name
        json tool_input
        json tool_output
        text agent_reasoning
        string outcome "controlled vocabulary"
        int tokens_input
        int tokens_output
        float latency_ms
        string model
        string error
    }

    c_strategy_params {
        string param_key PK
        float param_value
        float min_bound
        float max_bound
        int cooldown_days
        date cooldown_until
        string updated_by
        string change_reason
    }

    c_learnings {
        string id PK
        string session_id FK
        date learning_date
        string learning_type "observation|adjustment|avoid_ticker|goal_recommendation"
        string dimension
        text finding
        string param_adjusted
        float old_value
        float new_value
        int sample_size
        string confidence
        date expires_date
    }

    c_protection_events {
        string id PK
        date event_date
        int tier
        string trigger_field
        float trigger_value
        float threshold
        string action
        date suspended_until
        bool human_unlocked
    }

    c_sessions ||--o{ c_positions : "session_id"
    c_sessions ||--o{ c_traces : "session_id"
    c_sessions ||--o{ c_learnings : "session_id"
```

---

## Protection System

```mermaid
flowchart TD
    CHECK["check_protection_status()\nRuns at start of every session"]:::agent

    CHECK --> T2{daily_pnl\n≤ -$500?}
    T2 -->|yes| S2["Tier 2: Suspend today\nno new trades\nWrite c_protection_events"]:::stop

    CHECK --> T3{rolling_3d\n≤ -$1,000?}
    T3 -->|yes| S3["Tier 3: Reduce max_positions 50%\nfor 2 days"]:::warn

    CHECK --> T4{account\ndrawdown\n≥ 10%?}
    T4 -->|yes| S4["Tier 4: Suspend 24 hours"]:::stop

    CHECK --> T5{account\ndrawdown\n≥ 20%?}
    T5 -->|yes| S5["Tier 5: Suspend 7 days\nhuman unlock required"]:::crit

    CHECK --> T6{5+ consecutive\nlosing days?}
    T6 -->|yes| S6["Tier 6: Suspend 7 days\nhuman review required"]:::crit

    classDef agent fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef warn fill:#fef3c7,stroke:#f59e0b,color:#78350f
    classDef stop fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
    classDef crit fill:#4b0000,stroke:#ef4444,color:#ffffff
```

---

## External Integrations

```mermaid
flowchart LR
    subgraph "Data Sources"
        YF["yfinance\n• Universe scanner bars\n• ^VIX, ^TNX\n• ES=F NQ=F YM=F futures\n• Ticker fundamentals\n• Earnings calendar + news"]
        ALT["alternative.me\n• Fear & Greed index"]
        FF["ForexFactory\n• Economic calendar\n• USD high-impact events"]
        ALP["Alpaca Markets API\n• 11 sector ETF day bars\n• Premarket quotes batch\n• Intraday bars for ATR/ORB\n• Market movers screener\n• Bracket orders + trailing stops\n• Position data + fill prices\n• Paper account (ALPACA_PAPER=true)\n• Order prefix: stratc_"]
    end

    subgraph "AI"
        ANT["Anthropic API\n• Market Agent: claude-haiku-4-5\n• Research Agent: claude-haiku-4-5\n• Risk Agent: claude-haiku-4-5\n• Orchestrator: claude-sonnet-4-6\n• Learning Agent: claude-sonnet-4-6\nAll via base.py _dispatch_with_timeout\n25s per tool call hard limit"]
    end

    subgraph "Storage"
        SB["Supabase (separate project)\n• c_sessions · c_positions\n• c_traces · c_scan_results\n• c_strategy_params · c_learnings\n• c_goals · c_goal_snapshots\n• c_daily_performance\n• c_agent_config · c_protection_events\n• c_market_evals"]
    end

    subgraph "Alerting"
        GM["Gmail\nEOD summary + learning insights\nError alerts"]
        NTFY["ntfy.sh\nPush notifications"]
    end

    YF & ALT & FF & ALP --> AGENTS["Agents"]
    ANT --> AGENTS
    AGENTS --> SB
    AGENTS --> GM & NTFY
```

---

## Key Configuration (c_strategy_params + c_agent_config)

| Param | Default | Bounds | Adjusted by |
|---|---|---|---|
| `strategy_min_score` | 5 | 4–7 | Learning Agent |
| `max_positions` | 10 | 5–15 | Learning Agent |
| `atr_multiplier` | 0.8 | 0.5–1.5 | Learning Agent |
| `trail_pct` | 0.015 | 0.005–0.03 | Learning Agent |
| `intraday_entries` | true | bool | Human only (c_agent_config) |
| `intraday_max_new_positions` | 2 | int | Human only |
| `daily_loss_limit` | -$500 | float | Human only (safety param, blocked from Learning Agent) |
| `enable_learning_agent` | true | bool | Human only |

---

## What Makes Strategy C Different

| Dimension | Strategy A / B | Strategy C |
|---|---|---|
| Claude invocations | 1 per day | 5–8 per day (5 agents + optional retry) |
| Tool use | Agents are Python functions, no LLM tools | Each Claude agent has its own tool set; decides what to look up |
| Research | Batch scan → one Claude call | Parallel per-ticker mini-agents, each with 4 tools and individual context |
| Risk assessment | Deterministic Python rules | Risk Agent (Haiku) reasons about portfolio using 4 live DB queries |
| Synthesis | Claude selects from pre-filtered list | Orchestrator (Sonnet) synthesizes 3 agent reports, can trigger retry loop |
| Adaptive learning | Manual config changes | Learning Agent rewrites its own strategy params with cooldown protection |
| Observability | scan_results JSON blob | Full span-level trace in c_traces: every tool call, token count, latency, cost |
| Protection | Single daily loss limit | 6-tier protection system with immutable audit log |
| Pre-market timing | Order at execution | Trades queued in pending_trades if before 9:30 AM, executed at 9:45 AM by intraday |
| Account | Shared with Strategy A/B | Separate Alpaca paper account, separate Supabase project |
