-- Take write access to the agent's own tables away from the public key.
--
-- Every c_ table granted anon the full set: INSERT, UPDATE, DELETE and TRUNCATE. Anyone holding
-- the project's publishable key could empty the position ledger, the strategy parameters or the
-- protection events. TRUNCATE is not governed by row level security either, so enabling RLS
-- would not have covered it.
--
-- Provy's RLS migration (#416) named this exact hole in its own header on 2026-07-25 and then
-- closed it only for Provy's own tables, leaving the agent's tables wide open.
--
-- SELECT is deliberately kept. The Streamlit dashboard is read-only (verified: no insert, update,
-- upsert or delete anywhere in it) and connects with whatever key its hosted secrets hold. Keeping
-- SELECT means this change cannot break it, whichever key that is. The agent's own jobs are
-- unaffected either way -- they authenticate with the service-role key, which is untouched here.
--
-- Residual exposure, deliberately left: anon can still READ these tables, and there is no RLS on
-- them. Closing that means moving the dashboard onto a service-role key first.
--
-- Written as a loop so a table added later cannot silently reopen the hole.

DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename LIKE 'c\_%'
    ORDER BY tablename
  LOOP
    EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
    EXECUTE format('GRANT SELECT ON public.%I TO anon', t);
  END LOOP;
END $$;
