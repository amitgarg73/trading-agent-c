-- Verify that the agents report their work, not only their failures.  argus#679
--
--   psql "$PROVY_DB_URL" -f scripts/verify_tracing_gaps.sql
--
-- Two gaps, same family, fixed 28 Aug 2026:
--   orchestrator  submit_bracket_order was traced ONLY on rejection (b578841)
--   scanner       tool calls were not traced at all since the 4 Aug rewrite (e5d500f)
--
-- Run this after a few trading days have accumulated. Every session before the relevant fix carries
-- the old behaviour and is the control group, which is why each section reports before and after
-- rather than only the current state.
--
-- ⛔ EVERY SECTION CARRIES ITS DENOMINATOR, AND "NOTHING YET" IS NOT A PASS.
-- The defect being verified was a check that could not see: `submit_bracket_order` was traced only
-- on rejection, so an absence of fill traces looked exactly like a clean run. A verification that
-- reports PASS when no orders have been placed would repeat that mistake in a new place. Section 1
-- returns INCONCLUSIVE until the denominator is non-zero.

-- ⛔ ONE CONSTANT PER FIX. The two fixes shipped 14 minutes apart, and a single cutoff scored the
-- scanner against a timestamp that PRECEDED its own commit: sections 1-3 test b578841 (committed
-- 16:18:40+00) while section 4 tests e5d500f (16:32:58+00). Each is rounded up to the next minute
-- after its commit, so a session is only ever called "after fix" when the code it exercises was
-- actually live. Adding a third fix means adding a third constant, not widening one of these.
\set FIX_ORDERS  '2026-08-28 16:19:00+00'
\set FIX_SCANNER '2026-08-28 16:33:00+00'

\echo ''
\echo '=== 1. COVERAGE: does every session that placed entries now carry a fill trace? ==='
\echo '    Denominator is sessions whose terminal_reason says entries were placed, so a session'
\echo '    that legitimately traded nothing cannot inflate the result.'
\echo ''

-- ⛔ BOTH PERIODS ARE LISTED EXPLICITLY, so the "after fix" row always renders. Grouping the rows
-- that exist would print NOTHING for a period with no sessions, and an empty result reads as
-- "nothing wrong" when it means "nothing looked at". That is the same defect this script verifies.
with periods(after_fix) as (values (false), (true)),
placed as (
  select s.id, (s.started_at >= :'FIX_ORDERS'::timestamptz) as after_fix
  from ag_sessions s
  where s.session_type = 'intraday'
    and s.terminal_reason = 'intraday_entries_placed'
),
traced as (
  select p.after_fix, p.id,
         count(*) filter (where t.tool_name like '%submit_bracket_order%'
                            and coalesce(t.outcome,'') in ('filled','accepted')) as fill_traces
  from placed p
  left join ag_traces t on t.session_id = p.id
  group by 1,2
),
rolled as (
  select pr.after_fix,
         count(tr.id)                                  as sessions_placed,
         count(tr.id) filter (where tr.fill_traces > 0) as sessions_traced
  from periods pr left join traced tr on tr.after_fix = pr.after_fix
  group by 1
)
select case when after_fix then 'after fix' else 'before fix (control)' end as period,
       sessions_placed as sessions_that_placed_entries,
       sessions_traced as sessions_with_a_fill_trace,
       case
         when sessions_placed = 0 and after_fix
           then 'INCONCLUSIVE: no entries placed since the fix yet, so nothing has exercised it'
         when sessions_placed = 0
           then 'INCONCLUSIVE: no entries placed in this period'
         when not after_fix
           then 'expected 0: the old code never traced a fill'
         when sessions_traced = sessions_placed
           then 'PASS: every session that placed entries carries a fill trace'
         else 'FAIL: ' || (sessions_placed - sessions_traced)
              || ' session(s) placed entries with no fill trace'
       end as verdict
from rolled order by after_fix;

\echo ''
\echo '=== 2. STATES: the order call has three outcomes. Are all three reaching the column? ==='
\echo '    Before the fix only rejections were traced AND the OTLP gateway flattened every'
\echo '    non-error outcome to success, so the old rows read as one undifferentiated value.'
\echo ''

select case when t.created_at >= :'FIX_ORDERS'::timestamptz
            then 'after fix' else 'before fix (control)' end as period,
       coalesce(t.outcome, '(null)') as outcome,
       count(*) as traces,
       count(distinct t.session_id) as sessions
from ag_traces t
where t.tool_name like '%submit_bracket_order%'
group by 1,2 order by 1 desc, 3 desc;

\echo ''
\echo '=== 3. SHAPE: has the orchestrator stopped looking like two different agents? ==='
\echo '    Its step count split by whether an order happened to be REFUSED, because refusals were'
\echo '    the only thing it recorded. Before the fix: 57 sessions averaging 3.67 steps against 60'
\echo '    averaging 1.47, with a mean of 2.54 that no session takes. A distribution with a hole at'
\echo '    its own mean has no centre, and conformance cannot judge against one.'
\echo ''
\echo '    near_the_mean is the share of sessions within half a step of the mean. Low means bimodal.'
\echo '    Read it as a trend across periods, not against a fixed target.'
\echo ''

with per_session as (
  select s.id,
         (s.started_at >= :'FIX_ORDERS'::timestamptz) as after_fix,
         count(*) filter (where t.agent = 'orchestrator') as steps
  from ag_sessions s
  join ag_traces t on t.session_id = s.id
  where s.session_type = 'intraday'
  group by 1,2
),
stats as (
  select after_fix, avg(steps) as mean_steps, count(*) as sessions
  from per_session group by 1
)
select case when p.after_fix then 'after fix' else 'before fix (control)' end as period,
       st.sessions,
       round(st.mean_steps::numeric, 2) as mean_steps,
       round(stddev_pop(p.steps)::numeric, 2) as sd_steps,
       round((count(*) filter (where abs(p.steps - st.mean_steps) <= 0.5))::numeric
             / nullif(count(*),0), 2) as near_the_mean,
       case when st.sessions < 15 then 'too few sessions to read the shape yet' else '' end as note
from per_session p join stats st on st.after_fix = p.after_fix
group by 1, st.sessions, st.mean_steps order by 1 desc;

\echo ''
\echo 'Section 1 is the acceptance test. Sections 2 and 3 are the consequences, and 3 needs the most'
\echo 'data: the shape cannot be read until enough post-fix sessions exist to have a shape.'
\echo ''

\echo ''
\echo '=== 4. SCANNER: does it report what it looked at, or only what it concluded? ==='
\echo '    It emitted a span, a decision and a message and no tool calls at all, so 63% of its'
\echo '    visible work vanished on 5 Aug: 7.07 steps a session before, 2.56 after, while the scan'
\echo '    kept running over ~126 tickers a morning. Expect steps to recover toward ~5 and the'
\echo '    tool names below to reappear.'
\echo ''

with periods(after_fix) as (values (false), (true)),
sess as (
  select s.id, (s.started_at >= :'FIX_SCANNER'::timestamptz) as after_fix
  from ag_sessions s where s.session_type = 'premarket'
),
counted as (
  select pr.after_fix,
         count(distinct s.id) as sessions,
         count(t.id) filter (where ag_agent_base(t.agent) = 'scanner') as scanner_steps,
         count(t.id) filter (where ag_agent_base(t.agent) = 'scanner'
                               and t.tool_name like '%fetch_scored_tickers%') as reads,
         count(t.id) filter (where ag_agent_base(t.agent) = 'scanner'
                               and t.tool_name like '%download_prices%')      as downloads
  from periods pr
  left join sess s on s.after_fix = pr.after_fix
  left join ag_traces t on t.session_id = s.id
  group by 1
)
select case when after_fix then 'after fix' else 'before fix (control)' end as period,
       sessions,
       round(scanner_steps::numeric / nullif(sessions,0), 2) as steps_per_session,
       reads, downloads,
       case
         when sessions = 0 and after_fix
           then 'INCONCLUSIVE: no premarket sessions since the fix yet'
         when sessions = 0 then 'INCONCLUSIVE: no sessions in this period'
         when not after_fix then 'control: expect 0 reads and 0 downloads'
         when reads > 0 and downloads > 0
           then 'PASS: the scanner is reporting both of its external calls'
         else 'FAIL: scanner ran but did not report ' ||
              case when reads = 0 and downloads = 0 then 'either call'
                   when reads = 0 then 'the scan_results read'
                   else 'the price download' end
       end as verdict
from counted order by after_fix;

\echo ''
