-- Give Trading Agent C back its own memory of each run.
--
-- Background. The trace logger was migrated off c_sessions onto Provy's ag_sessions (commit
-- a93d5bf). From then on the agent had no local record of its own runs, so every control-flow
-- question it asked about itself -- "did I run premarket today?", "what is today's run id?",
-- "are there trades still pending?" -- was answered by reading the observability platform's
-- database.
--
-- That coupling broke the agent on 2026-07-25 when Provy split its databases (#416). Trace data
-- began landing in Provy production while the agent kept reading the pre-production project, so
-- premarket would create a run and the very next lookup could not find it. Every session since
-- has died at that line.
--
-- Trading Agent C is meant to model a real customer: its own environment, its own data, sending
-- telemetry outward. A customer's trading decisions cannot depend on their monitoring vendor's
-- database being reachable and correct. This restores that separation.
--
-- Telemetry is UNCHANGED by this migration. The full run record and every step still flow to
-- Provy production exactly as before. What is added here is a local copy of the run, used only
-- for the agent's own decisions.
--
-- Additive only: new nullable columns, a default on an existing NOT NULL column, two indexes.
-- No existing row is rewritten and no column is dropped.

ALTER TABLE public.c_sessions
  ADD COLUMN IF NOT EXISTS session_type       text,
  ADD COLUMN IF NOT EXISTS workflow_id        text,
  ADD COLUMN IF NOT EXISTS parent_session_id  uuid,
  ADD COLUMN IF NOT EXISTS status             text,
  ADD COLUMN IF NOT EXISTS metadata           jsonb,
  ADD COLUMN IF NOT EXISTS result_summary     text,
  ADD COLUMN IF NOT EXISTS last_entry_scan_at timestamptz;

-- terminal_reason is NOT NULL and a run has no terminal reason until it ends. Default it so a
-- run can be opened with the facts known at open time.
ALTER TABLE public.c_sessions ALTER COLUMN terminal_reason SET DEFAULT '';

-- "Today's premarket run for this workflow" is the hot lookup: every watchdog poll runs it.
CREATE INDEX IF NOT EXISTS idx_c_sessions_run_lookup
  ON public.c_sessions (workflow_id, session_type, started_at DESC);

-- Intraday runs are found by their premarket parent.
CREATE INDEX IF NOT EXISTS idx_c_sessions_parent
  ON public.c_sessions (parent_session_id);
