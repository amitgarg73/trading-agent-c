-- Migration: add post-session decision quality scoring columns
-- Purpose: track whether agent decisions were correct, independent of P&L luck.
-- Run once against production and test Supabase projects.

-- Per-trade quality signals on c_positions
ALTER TABLE c_positions
  ADD COLUMN IF NOT EXISTS r_multiple         FLOAT,   -- realized_pnl / initial_risk (stop_distance * shares)
  ADD COLUMN IF NOT EXISTS entry_timing_pct   FLOAT,   -- 0-100: where in day's H-L range we entered (100 = bought the low)
  ADD COLUMN IF NOT EXISTS directional_hit    BOOLEAN, -- did the stock close above our entry price?
  ADD COLUMN IF NOT EXISTS day_pct_move       FLOAT;   -- stock's full-day % move (close vs open), context for our exit

-- Daily decision quality summary on c_daily_performance
ALTER TABLE c_daily_performance
  ADD COLUMN IF NOT EXISTS directional_accuracy  FLOAT,  -- % of trades where directional_hit = true
  ADD COLUMN IF NOT EXISTS avg_entry_timing_pct  FLOAT,  -- avg entry_timing_pct across trades
  ADD COLUMN IF NOT EXISTS avg_r_multiple        FLOAT,  -- avg r_multiple across trades (replaces unused column)
  ADD COLUMN IF NOT EXISTS opportunity_cost      TEXT;   -- JSON: top movers vs our picks that day
