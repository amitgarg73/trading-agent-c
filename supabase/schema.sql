-- Trading Agent C — Database Schema
-- Run this in the Supabase SQL Editor for the trading-agent-c project.
-- After this, run seed.sql to populate initial config and parameters.
-- For the test project (trading-agent-c-test), run schema.sql only.

-- ── Observability ──────────────────────────────────────────────────────────────

CREATE TABLE c_sessions (
  id                   UUID PRIMARY KEY,
  date                 DATE NOT NULL,
  total_steps          INT NOT NULL DEFAULT 0,
  total_tool_calls     INT NOT NULL DEFAULT 0,
  agents_invoked       TEXT[],
  loop_iterations      INT NOT NULL DEFAULT 1,
  total_tokens_input   INT NOT NULL DEFAULT 0,
  total_tokens_output  INT NOT NULL DEFAULT 0,
  total_cost_usd       FLOAT NOT NULL DEFAULT 0,
  total_latency_ms     INT NOT NULL DEFAULT 0,
  trades_proposed      INT NOT NULL DEFAULT 0,
  trades_approved      INT NOT NULL DEFAULT 0,
  trades_executed      INT NOT NULL DEFAULT 0,
  risk_rejections      INT NOT NULL DEFAULT 0,
  retry_triggered      BOOL NOT NULL DEFAULT false,
  terminal_reason      TEXT NOT NULL,
  started_at           TIMESTAMPTZ NOT NULL,
  completed_at         TIMESTAMPTZ
);

CREATE INDEX idx_c_sessions_date ON c_sessions(date DESC);


CREATE TABLE c_traces (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       UUID NOT NULL REFERENCES c_sessions(id),
  span_id          UUID NOT NULL,
  parent_span_id   UUID,
  entity_id        TEXT,
  date             DATE NOT NULL,
  sequence         INT NOT NULL,
  agent            TEXT NOT NULL,
  step_type        TEXT NOT NULL,  -- tool_call | agent_message | decision | error
  tool_name        TEXT,
  tool_input       JSONB,
  tool_output      JSONB,
  agent_reasoning  TEXT,
  outcome          TEXT,
  tokens_input     INT NOT NULL DEFAULT 0,
  tokens_output    INT NOT NULL DEFAULT 0,
  latency_ms       INT NOT NULL DEFAULT 0,
  model            TEXT,
  error            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_traces_session    ON c_traces(session_id);
CREATE INDEX idx_c_traces_entity     ON c_traces(entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX idx_c_traces_agent_date ON c_traces(agent, date DESC);
CREATE INDEX idx_c_traces_step_type  ON c_traces(step_type, session_id);


-- ── Execution ──────────────────────────────────────────────────────────────────

-- Phase 0: c_positions is the single source of truth for both open positions
-- and closed trade history. Columns cover the full lifecycle (entry → exit).
CREATE TABLE c_positions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       UUID NOT NULL,
  ticker           TEXT NOT NULL,
  action           TEXT NOT NULL DEFAULT 'BUY',
  entry_price      FLOAT NOT NULL,
  target_price     FLOAT NOT NULL,
  stop_loss        FLOAT NOT NULL,
  position_size    FLOAT NOT NULL,
  shares           INT NOT NULL,
  confidence       TEXT,
  status           TEXT NOT NULL DEFAULT 'open',  -- open | closed | eod_forced
  open_date        DATE NOT NULL,
  close_date       DATE,
  entry_time       TIMESTAMPTZ NOT NULL,
  close_time       TIMESTAMPTZ,
  exit_reason      TEXT,         -- target | stop | eod_forced | manual
  realized_pnl     FLOAT,
  score_at_entry   INT,
  entry_context    TEXT,         -- premarket | intraday
  alpaca_order_id  TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_positions_session    ON c_positions(session_id);
CREATE INDEX idx_c_positions_status     ON c_positions(status, open_date);
CREATE INDEX idx_c_positions_close_date ON c_positions(close_date) WHERE close_date IS NOT NULL;
CREATE INDEX idx_c_positions_ticker     ON c_positions(ticker, open_date);


-- ── Scanner ────────────────────────────────────────────────────────────────────

-- Populated by the daily scanner (external process). Research Agent reads from
-- here via get_candidates(). Must have rows for today before premarket runs.
CREATE TABLE c_scan_results (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  UUID,
  date        DATE NOT NULL,
  ticker      TEXT NOT NULL,
  score       INT NOT NULL,
  price       FLOAT NOT NULL,
  sector      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_scan_results_date   ON c_scan_results(date DESC);
CREATE INDEX idx_c_scan_results_score  ON c_scan_results(date, score DESC);


-- ── Adaptive Parameters ────────────────────────────────────────────────────────

CREATE TABLE c_strategy_params (
  param_key        TEXT PRIMARY KEY,
  param_value      FLOAT NOT NULL,
  min_bound        FLOAT NOT NULL,
  max_bound        FLOAT NOT NULL,
  default_value    FLOAT NOT NULL,
  cooldown_days    INT NOT NULL DEFAULT 5,
  cooldown_until   DATE,
  last_updated     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by       TEXT NOT NULL DEFAULT 'seed',
  change_reason    TEXT,
  previous_value   FLOAT
);


CREATE TABLE c_learnings (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date          DATE NOT NULL,
  session_id            UUID,
  learning_type         TEXT NOT NULL,  -- observation | adjustment | avoid_ticker | sector_signal | goal_recommendation
  dimension             TEXT,           -- entry_quality | exit_timing | sector_bias | risk_sizing | etc.
  entity_id             TEXT,           -- ticker, sector name, or null for market-wide
  finding               TEXT NOT NULL,
  confidence            TEXT NOT NULL,  -- low | medium | high
  action_taken          TEXT,
  param_key             TEXT,
  old_param_value       FLOAT,
  new_param_value       FLOAT,
  requires_human_review BOOL NOT NULL DEFAULT false,
  outcome               TEXT NOT NULL DEFAULT 'pending',  -- pending | in_evaluation | validated | false_positive | expired
  expires_date          DATE NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  evaluated_at          TIMESTAMPTZ
);

CREATE INDEX idx_c_learnings_date    ON c_learnings(session_date DESC);
CREATE INDEX idx_c_learnings_type    ON c_learnings(learning_type, outcome);
CREATE INDEX idx_c_learnings_entity  ON c_learnings(entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX idx_c_learnings_active  ON c_learnings(expires_date) WHERE outcome IN ('pending', 'in_evaluation', 'validated');


-- ── Goals ──────────────────────────────────────────────────────────────────────

CREATE TABLE c_goals (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  goal_type       TEXT NOT NULL,    -- daily_pnl_target | daily_pnl_floor | weekly_pnl_target | win_rate_target
  entity_id       TEXT,             -- sector, ticker, or null for portfolio-wide
  target_value    FLOAT NOT NULL,
  current_value   FLOAT NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'active',  -- active | achieved | missed | suspended | pending_approval
  effective_from  DATE NOT NULL DEFAULT CURRENT_DATE,
  effective_until DATE,
  created_by      TEXT NOT NULL,    -- human | learning_agent
  evidence        TEXT,
  confidence      TEXT,
  auto_approve    BOOL NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_goals_active ON c_goals(status) WHERE status = 'active';


CREATE TABLE c_goal_snapshots (
  goal_id       UUID NOT NULL REFERENCES c_goals(id),
  snapshot_date DATE NOT NULL,
  value         FLOAT NOT NULL,
  status        TEXT NOT NULL,
  PRIMARY KEY (goal_id, snapshot_date)
);

CREATE INDEX idx_c_goal_snapshots_date ON c_goal_snapshots(snapshot_date DESC);


-- ── Infrastructure ─────────────────────────────────────────────────────────────

CREATE TABLE c_agent_config (
  config_key     TEXT PRIMARY KEY,
  config_value   JSONB NOT NULL,
  applies_to     TEXT NOT NULL DEFAULT 'all',  -- all | premarket | intraday | eod
  is_active      BOOL NOT NULL DEFAULT true,
  last_modified  TIMESTAMPTZ NOT NULL DEFAULT now(),
  modified_by    TEXT NOT NULL DEFAULT 'human',
  change_note    TEXT
);


CREATE TABLE c_protection_events (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_date        DATE NOT NULL,
  tier              INT NOT NULL,
  trigger_field     TEXT NOT NULL,
  trigger_value     FLOAT NOT NULL,
  threshold         FLOAT NOT NULL,
  action            TEXT NOT NULL,
  description       TEXT,
  suspended_until   TIMESTAMPTZ,
  human_unlocked    BOOL NOT NULL DEFAULT false,
  human_unlocked_at TIMESTAMPTZ,
  human_unlocked_by TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_protection_events_date ON c_protection_events(event_date DESC);
CREATE INDEX idx_c_protection_events_tier ON c_protection_events(tier);
CREATE INDEX idx_c_protection_active      ON c_protection_events(suspended_until)
  WHERE suspended_until IS NOT NULL AND human_unlocked = false;


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
  protection_tier  INT NOT NULL DEFAULT 0,
  total_cost_usd   FLOAT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_daily_performance_date ON c_daily_performance(date DESC);
