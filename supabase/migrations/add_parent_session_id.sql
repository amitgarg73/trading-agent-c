-- Migration: add parent_session_id to ag_sessions
-- Purpose: decouple intraday polls from the premarket session.
-- Each intraday poll now creates its own ag_sessions row with session_type='intraday'
-- and parent_session_id pointing to the premarket session for that day.
-- Positions (c_positions) remain keyed by the premarket session_id.
-- Run once against production and test Supabase projects.

ALTER TABLE ag_sessions
  ADD COLUMN IF NOT EXISTS parent_session_id UUID REFERENCES ag_sessions(id);

CREATE INDEX IF NOT EXISTS idx_ag_sessions_parent
  ON ag_sessions(parent_session_id)
  WHERE parent_session_id IS NOT NULL;
