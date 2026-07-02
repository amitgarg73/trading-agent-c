-- Migration: allow NULL entry_price on c_positions
-- Purpose: the entry redesign (decide premarket, buy at the open via an OPG/MOO order)
-- writes a pending_open position BEFORE the fill is known. The real entry price is the
-- day's open, which is not printed until the auction; the position watchdog backfills
-- entry_price and attaches the trailing stop post-open. The original NOT NULL constraint
-- rejected these inserts, crashing the premarket run AFTER the order was already submitted
-- (leaving a live, untracked position). Making the column nullable lets pending_open rows
-- exist so the watchdog can adopt and complete them.
-- Run once against the production and test Supabase projects. Idempotent.

ALTER TABLE c_positions ALTER COLUMN entry_price DROP NOT NULL;
