-- Trading Agent C — Seed Data
-- Run after schema.sql. Do NOT run on the test project.

-- ── Strategy Parameters ────────────────────────────────────────────────────────
-- These are the starting values. The Learning Agent adjusts within min/max bounds.
-- param_value, min_bound, max_bound, default_value, cooldown_days

INSERT INTO c_strategy_params
  (param_key, param_value, min_bound, max_bound, default_value, cooldown_days, change_reason)
VALUES
  ('strategy_min_score',          5,    4,    7,    5,    5, 'initial value'),
  ('max_positions',               10,   5,    15,   10,   3, 'initial value'),
  ('partial_profit_pct',          0.005,0.003,0.01, 0.005,7, 'initial value'),
  ('atr_multiplier',              0.8,  0.5,  1.5,  0.8,  5, 'initial value'),
  ('rr_ratio',                    2.0,  1.5,  3.0,  2.0,  5, 'initial value'),
  ('caution_position_multiplier', 0.6,  0.4,  0.8,  0.6,  7, 'initial value'),
  ('caution_min_score',           7,    6,    9,    7,    5, 'initial value'),
  ('max_sector_concentration',    0.35, 0.25, 0.45, 0.35, 7, 'initial value');


-- ── Agent Config ───────────────────────────────────────────────────────────────
-- Feature flags read at session start. Learning Agent can read but never write here.

INSERT INTO c_agent_config (config_key, config_value, applies_to, change_note) VALUES
  ('trading_days',               '["MON","TUE","WED","THU","FRI"]', 'all',       'Mon-Fri schedule'),
  ('total_capital',              '50000',                           'all',       'starting capital Phase 0'),
  ('daily_loss_limit',           '-500',                            'all',       'hard stop for the day'),
  ('enable_intraday_entries',         'true',   'intraday',  'hourly scan for additional opportunities'),
  ('intraday_entry_min_interval_mins','55',    'intraday',  'min minutes between entry scans — enforces hourly cadence'),
  ('intraday_min_score_bonus',        '1',     'intraday',  'extra bar for intraday entries'),
  ('intraday_max_new_positions',      '2',     'intraday',  'cap per scan'),
  ('intraday_entry_window_end',       '"13:00"','intraday', 'no entries after 1 PM ET'),
  ('enable_learning_agent',      'true',                            'eod',       'active from day 1'),
  ('enable_news_analyst',        'true',                            'premarket', 'TypeScript agent enabled'),
  ('auto_approve_goal_recs',     'false',                           'all',       'require human approval'),
  ('phase',                      '"paper"',                         'all',       'Phase 1: Alpaca paper trading');


-- ── Starter Goals ─────────────────────────────────────────────────────────────
-- Adjust target_value to match your risk tolerance before going live.

INSERT INTO c_goals (goal_type, target_value, created_by, evidence) VALUES
  ('daily_pnl_target', 300.0, 'human', 'Lock in when up $300 for the day'),
  ('daily_pnl_floor',  -200.0, 'human', 'Stop new entries when down $200 for the day');
