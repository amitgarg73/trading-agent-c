#!/usr/bin/env bash
# argus#580 — has the premarket pipeline run research and risk since 27 July?
# Read-only. Runs against Provy PROD, which is where trading-agent-c sends its traces.
PGPASSWORD='8KgNHFc43vER$9YY' psql \
  -h aws-1-us-west-2.pooler.supabase.com -p 5432 \
  -U postgres.eckthcvacrkfjihluubt -d postgres -At -F' | ' -c "
select case when t.agent like 'research%' then 'research' else t.agent end agent,
       count(*) filter (where t.created_at::date <= date '2026-07-27') thru_jul27,
       count(*) filter (where t.created_at::date >  date '2026-07-27') after_jul27
  from ag_traces t join ag_sessions s on s.id = t.session_id
 where s.session_type = 'premarket' and t.created_at > date '2026-07-15'
 group by 1 order by 1;"
