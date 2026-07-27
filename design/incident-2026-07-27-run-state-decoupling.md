# Three days of lost trading, and the coupling that caused it

**Date:** 2026-07-27
**Impact:** Every session failed from 2026-07-25 to 2026-07-27. One full trading day lost
(Monday 27 July). No premarket picks, no intraday entries, no position management.
**Money at risk:** None. The account was flat from Friday's close, all 105 positions closed.

## What happened

Two separate faults, stacked. The first hid the second.

**Fault one, permissions.** Provy enabled row level security on the database Trading Agent C
uses (#416, 25 July). That revoked access for the key the agent's scheduled jobs authenticate
with. Every job that read Provy's tables died on `permission denied`. The migration's own header
argued the change was safe for Trading Agent C because it "connects with the service-role key".
That was true of the local developer config and false of the CI secret, which was never checked.
Fixed by pointing the CI secret at the service-role key.

**Fault two, the real one.** Provy also split its databases in the same change. Trace data began
landing in Provy production while the agent kept reading the pre-production project. Premarket
would create a run, and the very next lookup could not find it, because it was looking in a
different database. Once permissions were fixed, this is what remained:

```
[premarket] Session 8d404a8b — 118 candidates, 3 trades: DE, TMO, RTX
[intraday]  Premarket pipeline produced no session — exiting.
```

The run existed. It was in Provy production. The agent was reading pre-production.

## Why it was possible

Trading Agent C asked Provy questions about itself. "Did I run premarket today?" "What is today's
run id?" "Are there trades still pending?" "When did I last scan for entries?" All four were
answered by querying the observability platform's tables.

That coupling was introduced when the trace logger was migrated off `c_sessions` onto
`ag_sessions` (commit a93d5bf). From that point the agent had no local memory of its own runs.

Trading Agent C exists to model a real customer: its own environment, its own data, telemetry
sent outward. A customer cannot have trading stop because their monitoring vendor changed
something. We shipped a monitoring product whose own reference customer could not survive an
outage of it. That is the dogfooding finding, and it is worth more than the fix.

## The change

Control flow reads `core/run_state.py` and nothing else. The module keeps the agent's own record
of each run in its own database (`c_sessions`, revived and extended).

| Question | Was | Now |
|---|---|---|
| Today's premarket run id | `ag_sessions` | `run_state.today_premarket_run_id()` |
| Is a run already in flight | `ag_sessions` | `run_state.today_premarket_run()` |
| Trades deferred past the open | `ag_sessions.metadata` | `run_state.get_pending_trades()` |
| Time of last entry scan | `ag_sessions` + `ag_traces`, two queries | `run_state.last_entry_scan_at()`, one |
| Run context for the learning agent | `ag_sessions` | `run_state.read_run()` |

**Telemetry is unchanged.** The full run record and every step still flow to Provy production.
This is a local copy written alongside, never instead of. Provy loses nothing.

Writes to the run record are synchronous and raise on failure. Trace emission stays
fire-and-forget. A dropped trace costs a dashboard row; a dropped run record means the agent
forgets it is holding positions. The July outage was silent precisely because the write that
mattered was treated like the write that did not.

## Three bugs found on the way

1. **The premarket concurrency guard failed open.** It stripped a trailing "Z" from the start
   time and subtracted the result from a naive `utcnow()`. Against an offset-aware timestamp that
   raises `TypeError`, which the surrounding `except` swallowed into "no session today". A guard
   that fails open lets the day's trades be placed twice.
2. **The premarket verify step could no longer pass.** It asserted that a row existed in Provy's
   session table *in the agent's database*. After the split no such row can ever exist there, so
   the assertion was guaranteed to fail regardless of how healthy the run was. It now asserts on
   the run record, which is the thing that actually has to exist.
3. **The validation script's simulated flag was set too late.** It opened a run and then updated
   it to `is_simulated`, leaving a window where a real premarket firing in between would see an
   unflagged run and stand down. It is now set at open. The guard also filters on it, which it
   never did, so a mid-morning rehearsal no longer suppresses the next real session.

## Guardrails added

`tests/test_control_flow_isolation.py` fails if any module under `sessions/`, `core/`, `agents/`
or `scanner/` queries a Provy table again. Scripts, dashboards and eval tooling are exempt: they
may read Provy, because nothing they do places an order.

Suite: 1042 passing, up from 979.

## Also changed

The premarket workflow takes a `bypass_window` input, so the pipeline can be run on demand
outside the 06:00-10:30 ET window. Paper account only.

## Both follow-ups closed (same day)

**The watchdog can now fail.** It only ever closed orphaned sessions, and "no orphaned sessions
found" is precisely what a completely dead agent looks like, which is why it reported success
every hour for three days. It now also asserts that the day's work happened:

| Check | Fires when |
|---|---|
| Premarket missing | past 11:00 ET on a trading day with no premarket run |
| Premarket unfinished | a run started and never completed |
| Position watchdog stale | during market hours, last poll over 45 minutes ago |
| End-of-day missing | past 16:30 ET with no EOD run |

Anything found is emailed and exits non-zero so the job goes red. The workflow had **no alert
credentials and no failure notification at all** -- the one scheduled job that could not tell
anyone anything. Both added.

The position watchdog now records a heartbeat on every exit path including the no-op ones,
because a job that writes nothing when idle is indistinguishable from a job that is dead. A
failed heartbeat write is logged and swallowed: managing open positions matters more than
bookkeeping about it, and a missing heartbeat makes the watchdog alert, which is the safe
direction to fail.

Verified against live data before deploying: with the heartbeat absent it correctly reported
"the position watchdog has never reported in" while a position was open.

**The public key can no longer write to the agent's tables.** All 15 `c_` tables granted `anon`
INSERT, UPDATE, DELETE and TRUNCATE. Now SELECT only. SELECT is kept deliberately so the
read-only Streamlit dashboard cannot break whichever key it holds; the agent's own jobs use the
service-role key and are unaffected. Written as a loop so a table added later cannot silently
reopen the hole.

Residual, deliberately left: `anon` can still read these tables and there is no RLS on them.
Closing that means moving the dashboard onto a service-role key first.

## A test was writing to the live trading database

Found while verifying the above, and worth more than the fix. The heartbeat write sits in a
`finally` in `position_watchdog.main()`. The existing test mocked everything `main()` touched at
the time but not the database client, and `core/db.py` loads `.streamlit/secrets.toml` when
`SUPABASE_URL` is unset -- so a local test run carries live credentials. The suite wrote a
fabricated heartbeat into the live table, claiming the agent was healthy when it was not, and
passed while doing it.

`tests/conftest.py` now refuses real client construction in every test, turning that whole class
of mistake into a loud failure. The fabricated row was deleted.

The repo already warned about this ("Do NOT wire SUPABASE_URL_C / SUPABASE_KEY_C into the pytest
CI job... any test that skips mocking get_client would write real data"). The warning was correct
and unenforced.

Suite: 1063 passing.
