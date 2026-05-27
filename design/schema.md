# Database Schema — Trading Agent C

Separate Supabase project from Strategy A/B. All tables use the `c_` prefix.
This doc covers all tables: core observability tables (c_traces, c_sessions),
execution tables (c_trades, c_positions), adaptive system tables
(c_strategy_params, c_learnings, c_goals, c_goal_snapshots), and
infrastructure tables (c_agent_config, c_protection_events, c_daily_performance).

---

## Existing Tables (from trace-schema.md — unchanged)

`c_traces` and `c_sessions` are defined in trace-schema.md. Refer there for columns.
They are not duplicated here.

---

## Execution Tables

### c_trades

One row per executed trade, from entry to exit. The source of truth for P&L.

```sql
CREATE TABLE c_trades (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID NOT NULL,            -- FK to c_sessions
  date            DATE NOT NULL,
  ticker          TEXT NOT NULL,
  entry_price     FLOAT NOT NULL,
  exit_price      FLOAT,                    -- null until trade closes
  position_size   FLOAT NOT NULL,           -- in dollars
  shares          INT NOT NULL,
  entry_time      TIMESTAMPTZ NOT NULL,
  exit_time       TIMESTAMPTZ,
  realized_pnl    FLOAT,                    -- null until closed
  exit_reason     TEXT,                     -- target | stop | eod_forced | bracket_exit_detected | manual
  rr_achieved     FLOAT,                    -- actual R:R at exit
  score_at_entry  INT,
  vix_at_entry    FLOAT,
  market_signal   TEXT,                     -- GO | CAUTION
  news_signal     TEXT,                     -- positive | negative | neutral | null
  sector          TEXT,
  entry_type      TEXT,                     -- bid | mid (from hybrid_limit_price)
  entry_context   TEXT,                     -- premarket | intraday
  alpaca_order_id TEXT,
  status          TEXT NOT NULL DEFAULT 'open',  -- open | closing | closed | eod_timeout
  created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_trades_session ON c_trades(session_id);
CREATE INDEX idx_c_trades_date ON c_trades(date);
CREATE INDEX idx_c_trades_ticker ON c_trades(ticker, date);
```

### c_positions

Mirror of currently open positions. Synced with Alpaca each intraday poll.
Closed trades are removed from here and finalized in c_trades.

```sql
CREATE TABLE c_positions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID NOT NULL,
  ticker          TEXT NOT NULL,
  entry_price     FLOAT NOT NULL,
  position_size   FLOAT NOT NULL,
  shares          INT NOT NULL,
  entry_time      TIMESTAMPTZ NOT NULL,
  target_price    FLOAT NOT NULL,
  stop_price      FLOAT NOT NULL,
  entry_type      TEXT,
  entry_context   TEXT,
  alpaca_order_id TEXT,
  closing_order_id TEXT,                    -- set when EOD close submitted
  status          TEXT NOT NULL DEFAULT 'open',
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_positions_status ON c_positions(status);
CREATE INDEX idx_c_positions_session ON c_positions(session_id);
```

---

## Adaptive System Tables

### c_strategy_params

Live strategy parameters. The Learning Agent adjusts values within bounds.
The session driver reads from this table at the start of each session.

```sql
CREATE TABLE c_strategy_params (
  param_key        TEXT PRIMARY KEY,
  param_value      FLOAT NOT NULL,
  min_bound        FLOAT NOT NULL,
  max_bound        FLOAT NOT NULL,
  default_value    FLOAT NOT NULL,
  cooldown_days    INT NOT NULL DEFAULT 5,
  cooldown_until   DATE,                   -- null if no active cooldown
  last_updated     TIMESTAMPTZ DEFAULT now(),
  updated_by       TEXT,                   -- 'learning_agent' | 'human' | 'seed'
  change_reason    TEXT,
  previous_value   FLOAT                   -- set on each update for easy revert
);

-- Seed data (run once at project setup)
INSERT INTO c_strategy_params VALUES
  ('strategy_min_score',          5, 4, 7, 5, 5, null, now(), 'seed', 'initial value', null),
  ('max_positions',               10, 5, 15, 10, 3, null, now(), 'seed', 'initial value', null),
  ('partial_profit_pct',          0.005, 0.003, 0.01, 0.005, 7, null, now(), 'seed', 'initial value', null),
  ('atr_multiplier',              0.8, 0.5, 1.5, 0.8, 5, null, now(), 'seed', 'initial value', null),
  ('rr_ratio',                    2.0, 1.5, 3.0, 2.0, 5, null, now(), 'seed', 'initial value', null),
  ('caution_position_multiplier', 0.6, 0.4, 0.8, 0.6, 7, null, now(), 'seed', 'initial value', null),
  ('caution_min_score',           7, 6, 9, 7, 5, null, now(), 'seed', 'initial value', null),
  ('max_sector_concentration',    0.35, 0.25, 0.45, 0.35, 7, null, now(), 'seed', 'initial value', null);
```

### c_learnings

Structured findings from the Learning Agent. Injected into tomorrow's session context.

```sql
CREATE TABLE c_learnings (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date         DATE NOT NULL,
  learning_type        TEXT NOT NULL,   -- observation | adjustment | avoid_ticker | sector_signal | goal_trigger
  entity_id            TEXT,            -- ticker, sector name, or null for market-wide
  finding              TEXT NOT NULL,
  confidence           TEXT NOT NULL,   -- low | medium | high
  action_taken         TEXT,            -- description of param change, or null
  param_key            TEXT,            -- which param was adjusted, if any
  old_param_value      FLOAT,
  new_param_value      FLOAT,
  requires_human_review BOOL DEFAULT false,
  outcome              TEXT DEFAULT 'pending',  -- pending | in_evaluation | validated | false_positive | expired
  expires_date         DATE NOT NULL,
  created_at           TIMESTAMPTZ DEFAULT now(),
  evaluated_at         TIMESTAMPTZ
);

CREATE INDEX idx_c_learnings_date ON c_learnings(session_date);
CREATE INDEX idx_c_learnings_type ON c_learnings(learning_type, outcome);
CREATE INDEX idx_c_learnings_entity ON c_learnings(entity_id) WHERE entity_id IS NOT NULL;
```

### c_goals

Configurable trading goals. Updated by humans and recommended by the Learning Agent.

```sql
CREATE TABLE c_goals (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  goal_type       TEXT NOT NULL,         -- daily_pnl_target | daily_pnl_floor | weekly_pnl_target |
                                         -- win_rate_target | max_drawdown | sector_max_allocation |
                                         -- score_threshold_by_sector | time_of_day_filter | ticker_affinity
  entity_id       TEXT,                  -- sector name, ticker, or null for portfolio-wide
  target_value    FLOAT NOT NULL,
  current_value   FLOAT DEFAULT 0,
  status          TEXT DEFAULT 'active', -- active | achieved | missed | suspended | pending_approval
  effective_from  DATE NOT NULL DEFAULT CURRENT_DATE,
  effective_until DATE,                  -- null = indefinite
  created_by      TEXT NOT NULL,         -- 'human' | 'learning_agent'
  evidence        TEXT,                  -- populated by Learning Agent recommendations
  confidence      TEXT,                  -- low | medium | high (Learning Agent recommendations)
  auto_approve    BOOL DEFAULT false,     -- if true, learning_agent recommendations auto-activate
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_goals_status ON c_goals(status) WHERE status = 'active';

-- Seed: starter goals
INSERT INTO c_goals (goal_type, target_value, created_by) VALUES
  ('daily_pnl_target', 300.0, 'human'),
  ('daily_pnl_floor',  -200.0, 'human');
```

### c_goal_snapshots

Daily progress against each goal. Used for trend analysis and Learning Agent input.

```sql
CREATE TABLE c_goal_snapshots (
  goal_id      UUID NOT NULL REFERENCES c_goals(id),
  snapshot_date DATE NOT NULL,
  value        FLOAT NOT NULL,
  status       TEXT NOT NULL,
  PRIMARY KEY (goal_id, snapshot_date)
);
```

---

## Infrastructure Tables

### c_agent_config

Feature flags and behavioral config. Read at session start by all session drivers.
The Learning Agent can read but never write to this table.

```sql
CREATE TABLE c_agent_config (
  config_key     TEXT PRIMARY KEY,
  config_value   JSONB NOT NULL,
  applies_to     TEXT,              -- 'all' | 'premarket' | 'intraday' | 'eod' | agent name
  is_active      BOOL DEFAULT true,
  last_modified  TIMESTAMPTZ DEFAULT now(),
  modified_by    TEXT DEFAULT 'human',
  change_note    TEXT
);

-- Seed: default config
INSERT INTO c_agent_config (config_key, config_value, applies_to, change_note) VALUES
  ('trading_days',               '["MON","TUE","WED","THU","FRI"]', 'all', 'Mon-Fri schedule'),
  ('enable_intraday_entries',    'false', 'intraday', 'disabled until Phase 2'),
  ('intraday_min_score_bonus',   '1', 'intraday', 'extra bar for intraday entries'),
  ('intraday_max_new_positions', '2', 'intraday', 'cap per poll'),
  ('intraday_entry_window_end',  '"13:00"', 'intraday', 'no entries after 1 PM ET'),
  ('enable_learning_agent',      'true', 'eod', 'learning active from day 1'),
  ('enable_news_analyst',        'true', 'premarket', 'TypeScript agent enabled'),
  ('auto_approve_goal_recs',     'false', 'all', 'require human approval for recommended goals'),
  ('phase',                      '"simulation"', 'all', 'Phase 0: simulation mode');
```

### c_protection_events

Audit log of all principal protection triggers. Never deleted. Never modified after insert.

```sql
CREATE TABLE c_protection_events (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_date       DATE NOT NULL,
  tier             INT NOT NULL,             -- 1-6
  trigger_field    TEXT NOT NULL,            -- e.g. 'daily_pnl', 'account_drawdown_pct'
  trigger_value    FLOAT NOT NULL,           -- the value that triggered it
  threshold        FLOAT NOT NULL,           -- the configured threshold
  action           TEXT NOT NULL,            -- 'stopped_day' | 'reduced_sizing' | 'suspended_24h' | etc.
  description      TEXT,                     -- human-readable summary
  suspended_until  TIMESTAMPTZ,
  human_unlocked   BOOL DEFAULT false,
  human_unlocked_at TIMESTAMPTZ,
  human_unlocked_by TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_protection_events_date ON c_protection_events(event_date);
CREATE INDEX idx_c_protection_events_tier ON c_protection_events(tier);
```

### c_daily_performance

One row per trading day. Source of truth for trend analysis and Learning Agent input.

```sql
CREATE TABLE c_daily_performance (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       UUID NOT NULL,
  date             DATE NOT NULL UNIQUE,
  realized_pnl     FLOAT NOT NULL DEFAULT 0,
  trades_total     INT NOT NULL DEFAULT 0,
  trades_won       INT NOT NULL DEFAULT 0,
  trades_lost      INT NOT NULL DEFAULT 0,
  win_rate         FLOAT NOT NULL DEFAULT 0,
  largest_win      FLOAT NOT NULL DEFAULT 0,
  largest_loss     FLOAT NOT NULL DEFAULT 0,
  avg_hold_min     FLOAT NOT NULL DEFAULT 0,
  avg_rr_achieved  FLOAT NOT NULL DEFAULT 0,
  vix_at_open      FLOAT,
  market_signal    TEXT,
  protection_tier  INT NOT NULL DEFAULT 0,  -- highest tier triggered, 0 = none
  total_cost_usd   FLOAT,                   -- API cost for the day
  created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_daily_perf_date ON c_daily_performance(date DESC);
```

### c_scan_results

Scanner output for each trading day. Kept for shadow comparison and Learning Agent
access to what was in the candidate pool.

```sql
CREATE TABLE c_scan_results (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  UUID NOT NULL,
  date        DATE NOT NULL,
  ticker      TEXT NOT NULL,
  score       INT NOT NULL,
  price       FLOAT NOT NULL,
  sector      TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_c_scan_results_session ON c_scan_results(session_id);
```

---

## Common Queries

### Today's session state
```sql
SELECT s.date, s.terminal_reason, s.trades_executed, s.total_cost_usd,
       p.realized_pnl, p.win_rate
FROM c_sessions s
LEFT JOIN c_daily_performance p ON p.session_id = s.id
WHERE s.date = CURRENT_DATE;
```

### Rolling 5-day performance (for Learning Agent)
```sql
SELECT date, realized_pnl, win_rate, trades_total, protection_tier
FROM c_daily_performance
ORDER BY date DESC
LIMIT 5;
```

### Active learnings for tomorrow's context injection
```sql
SELECT learning_type, entity_id, finding, confidence, action_taken
FROM c_learnings
WHERE outcome IN ('pending', 'in_evaluation', 'validated')
  AND expires_date >= CURRENT_DATE
ORDER BY session_date DESC, confidence DESC
LIMIT 5;
```

### Parameter change history
```sql
SELECT param_key, previous_value, param_value, change_reason, updated_by, last_updated
FROM c_strategy_params
ORDER BY last_updated DESC;
```

### Protection audit
```sql
SELECT event_date, tier, trigger_field, trigger_value, threshold, action
FROM c_protection_events
ORDER BY event_date DESC, tier DESC;
```

---

## Supabase Setup (three environments)

| Environment | Purpose | Tables seeded |
|---|---|---|
| `trading-agent-c` | Production | All tables + seed data |
| `trading-agent-c-dev` | Development | All tables + seed data |
| `trading-agent-c-test` | CI tests | All tables, no seed (tests set their own state) |

GitHub secrets naming:
- `SUPABASE_URL_C` / `SUPABASE_KEY_C` — production
- `SUPABASE_URL_C_DEV` / `SUPABASE_KEY_C_DEV` — development
- `SUPABASE_URL_C_TEST` / `SUPABASE_KEY_C_TEST` — CI tests
