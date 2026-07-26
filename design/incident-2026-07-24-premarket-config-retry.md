# Incident 2026-07-24 — premarket died on a transient Supabase error

_Filed as a design note because GitHub issues are disabled on this repo._

**Status:** fixed 2026-07-26.

## What happened

Friday 2026-07-24, premarket run [30095209658](https://github.com/amitgarg73/trading-agent-c/actions/runs/30095209658) failed. Supabase sits behind Cloudflare and returned **Cloudflare error 525** (SSL handshake failed between Cloudflare and the origin), so `postgrest` got an HTML error page where it expected JSON:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for APIErrorFromJSON
  Invalid JSON: expected value at line 1 column 1
  [input_value=b'<!DOCTYPE html>...']
postgrest.exceptions.APIError: {'message': 'JSON could not be generated', 'code': 525, ...}
```

It was transient. Everything from 14:00 onward that day was fine:

| workflow | 2026-07-24 |
|---|---|
| premarket 13:00 | **failure** (only run of the day) |
| intraday 14:00-19:45 | 24 runs, all success |
| intraday_scan 14:30, 16:30 | success |
| eod 20:05 | success |

**Impact:** no premarket session on Friday, so no premarket entry decisions (Market -> News -> Research -> Risk -> Orchestrator). Intraday and EOD were unaffected. The failure alert email did send.

## Where it died

`sessions/premarket.py:301` -> `is_trading_day()` -> `get_config()` -> `load_agent_config()`, the first DB read of the session. It died before it could work out whether Friday was a trading day.

## The part worth fixing

The code reads as though it already tolerates this. `core/agent_config.py:25` says "Falls back to `_DEFAULTS` for any key not in the DB", and `is_trading_day()` passes `_DEFAULTS["trading_days"]` as its default. But that fallback only applies when the query **succeeds** and the key is absent. `load_agent_config()` raises on a failed query, so the default never runs. The safety net exists, strung under the wrong hole.

## Fix

**Retry transient DB failures.** A shared `execute_with_retry()` in `core/db.py`: retry on 5xx (502/503/504/520-527), 429, and transport/timeout errors, with backoff. Do not retry 4xx — a permission or schema error will not improve on the second attempt. `load_agent_config()` uses it.

**Deliberately NOT doing: falling back to `_DEFAULTS` when the DB is unreachable.** That would make the docstring true and the system worse. It would silently run a trading day on default config (`phase: simulation`, `enable_intraday_entries: False`, default trading days and windows) every time Supabase hiccups. In a financial system, running with the wrong config is worse than not running. After retries are exhausted it should still fail loudly, as it does now.

**Deliberately NOT doing: a blanket workflow-level re-run of premarket.** `premarket.yml` is `workflow_dispatch` only (driven by cron-job.org), and re-running the whole session risks duplicate trades. `_existing_session_guard` does make it idempotent per day and the premarket window (06:00-10:30 ET) bounds it, so a retry would probably be safe, but "probably safe" is not the standard for something that places orders. The DB-layer retry addresses the actual failure without touching re-run semantics.

## Acceptance

- A 525/503/timeout on the config read is retried and the session continues.
- A 4xx is not retried and surfaces immediately.
- Exhausted retries still raise, and the alert still fires.
- Config values are unchanged on the happy path (no behaviour change when the DB is healthy).
- Tests cover: transient-then-success, non-transient no-retry, exhaustion, and that `_DEFAULTS` is never substituted for a failed query.

## What shipped (2026-07-26)

- `core/db.py`: `execute_with_retry()`, `is_transient()`, `_status_of()`. Retries 408/429/5xx and
  the Cloudflare 52x band plus transport failures; three attempts, linear backoff (2s, 4s).
- `core/agent_config.py`: `load_agent_config()` runs its read through it, with the reasoning for
  NOT defaulting written into the docstring so the next reader does not "fix" it.
- `_status_of()` distinguishes an HTTP status from a Postgres SQLSTATE by length. `int('42501')` is
  a valid integer, so permission-denied would otherwise have been compared against HTTP statuses.

Tests: `tests/core/test_db_retry.py` (23 cases) and a `TestConfigReadResilience` class in
`tests/core/test_agent_config.py`, including one asserting the defaults are NEVER substituted for a
failed read. Suite 970 passing.

## Not done, on purpose

The retry ladder is short (roughly 6 seconds total). Premarket runs inside a fixed 06:00-10:30 ET
window, so a long ladder would trade one failure mode for another. Friday's outage lasted minutes,
not seconds, so this fix would not have saved that particular run on its own; it covers the far more
common brief blip. If a multi-minute outage recurs, the answer is a workflow-level retry, and the
prerequisite work is already there: `_existing_session_guard` makes the session idempotent per day.
That was left alone because re-running a session that places orders needs more care than a
transient-read fix warrants.
