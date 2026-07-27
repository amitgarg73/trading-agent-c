-- Let a job prove it ran, even when it had nothing to do.
--
-- The position watchdog polls every 15 minutes and, by design, writes nothing when there are no
-- positions to manage. So "ran and had nothing to do" looks identical to "did not run at all".
-- That ambiguity is why three days of total outage went unnoticed between 25 and 27 July 2026:
-- the hourly session watchdog had no way to tell a quiet agent from a dead one, and reported
-- success throughout.
--
-- One row per job, overwritten on each run. No history is kept -- the only question being asked
-- is "when did this last complete", and a growing table would be one more thing to prune.

CREATE TABLE IF NOT EXISTS public.c_job_heartbeat (
  job          text PRIMARY KEY,
  last_run_at  timestamptz NOT NULL,
  last_status  text NOT NULL DEFAULT 'ok',
  detail       text
);
