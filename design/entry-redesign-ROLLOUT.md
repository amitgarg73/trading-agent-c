# Entry Redesign — Rollout & Monitoring Runbook

Read this to pick up the state of the premarket-open entry redesign. Design rationale is in
`design/entry-redesign-premarket-open.md`; this file is the live-operations view.

## Status (2026-07-01)

LIVE on paper via the flag `OPENING_ENTRY_ENABLED`. The old chase logic is kept as the flag-OFF
backup (rollback = unset the flag). Not real money (no near-term plan for it), so we run the new
path as-is on the paper account and watch it.

### Update 2026-07-02 — first live run: two fixes
1. Crash: the pending_open insert hit a `c_positions.entry_price NOT NULL` constraint (migration
   `make_entry_price_nullable.sql`) and the order was submitted before the insert, so a DB failure
   orphaned a live order. Fixed: nullable `entry_price` + cancel-the-order-if-the-insert-fails.
2. **OPG does not fill on Alpaca paper.** The V market-on-open order was accepted (9:05 ET, before
   the cutoff) but expired unfilled at 9:31 — Alpaca's paper env does not simulate the opening
   auction, so OPG/CLS orders always expire. On paper we now submit a **market-at-open DAY order**
   instead (fills in continuous trading at ~the open, the same entry basis the backtests measured);
   live uses true OPG. Controlled by `_use_opg_orders()` (default = market-at-open on paper; set
   `USE_OPG=1` to force OPG). Paper entries are recorded with `entry_context='opening_market'`.

**What changed vs the old behavior**
- Old (flag OFF): premarket defers; intraday runs the funnel mid-morning and CHASES the ask; the
  chase + staleness gates were rejecting ~100% of entries on up-days → zero trades.
- New (flag ON): premarket runs the funnel BEFORE the open and submits market-on-open (OPG) orders;
  the post-open watchdog backfills the real open fill and attaches the trailing stop; intraday runs
  management-only (no new entries). No chase, no gates.

**Why:** the entry was the entire leak. Chasing bought ~+1.6% above the open, which erased a ~+1%
edge (near-breakeven). Buying at the open recovers it (~+1.2%/trade on the traded set; scanner-only
floor +0.33%/trade beating SPY in a down market). An entry-delay sweep showed the edge lives in the
FIRST MINUTE, so we hit the exact open (OPG), not a near-open compromise. Evidence:
`entry_backtest.py`, `entry_backtest_premarket.py` (read-only, feed-selectable: `BACKTEST_FEED=sip`).

## How it works (code map)

- `core/alpaca.submit_opening_order(ticker, shares, limit_price=None)` — MOO (or LOO) via
  `TimeInForce.OPG`. No gates. Protection attaches post-fill.
- `sessions/premarket._execute_opening_orders(...)` — submits OPG for the shortlist, writes
  `c_positions` as `pending_open` (no fill/trail yet).
- `sessions/premarket.main()` — flag-ON + pre-open branch: run `run_premarket_pipeline` → submit OPG.
- `sessions/position_watchdog._reconcile_opening_orders(...)` — post-open: read the open fill,
  backfill `entry_price`, attach the trailing stop, flip `pending_open → open`. Idempotent.
- `sessions/intraday.main()` — flag-ON: management-only, exits immediately (no new entries).
- `sessions/position_watchdog._maybe_opening_fallback(...)` — near-open recovery: ONLY 09:30-09:45 ET
  and ONLY when premarket produced no session, run the funnel once and enter at market (`on_open=False`,
  entry_context `opening_fallback`) so a premarket miss does not cost the day. Hard window gate = never a
  mid-morning chase. Relies on the watchdog cron firing in the 9:30-9:45 window (it runs every ~15 min).
- `sessions/eod._opening_entry_report(...)` — appends "Entry basis vs open" to the daily alert.
- Flag helper: `sessions/premarket._opening_entry_enabled()` reads env `OPENING_ENTRY_ENABLED`.

## Turn on / off

- ON: set `OPENING_ENTRY_ENABLED=true` in the workflow env of `premarket.yml`, `intraday_scan.yml`,
  and `intraday.yml` (watchdog). Merge branch → main (trading-C deploys from main).
- OFF (rollback): unset / set false. Instantly reverts to the old defer+chase path. No redeploy of
  logic needed.

## Where to monitor

1. **Provy tenant dashboard** (workflow_id `c4d90fe7-...`): premarket session + OPG traces; fills →
   `c_positions`; EOD P&L → Outcome Ledger / Results; Command Center trust score; Diagnosis.
2. **EOD alert email** (amit.thirdeyetrading@gmail.com): daily P&L + the **"Entry basis vs open"**
   line (the proof metric — should trend to ~0 vs the old ~+1.6%).
3. **GitHub Actions premarket run log**: confirm OPG orders submit BEFORE the ~09:28 ET cutoff.
4. **Alpaca dashboard + `c_positions`**: actual opening fills; `pending_open → open` reconcile.

## What to watch in the first sessions (the unverified-in-prod bits)

- [ ] Premarket completes and submits OPG **before 09:28 ET** (check the cron fire time + funnel
      duration). If it misses the cutoff, OPG orders reject — move the premarket cron earlier.
- [ ] The pre-open funnel actually yields a shortlist (research handles `available=false` via scanner
      conviction). If yield is thin, add the **scanner top-N fallback** (needs target/stop synthesis).
- [ ] OPG orders fill at the open and the watchdog reconciles `pending_open → open` with a trail.
- [ ] Entry basis vs open lands near **0** in the EOD email.

## Revisit / open items (do after the paper run proves out)

- Delete the chase gate + staleness gate + `max_entry_premium` from `core/alpaca.py` / `core/params.py`,
  and the old defer/chase paths; make the flag default ON (or remove the flag).
- Scanner top-N fallback if pre-open research yield is low.
- Consider LOO (limit-on-open) + a data-derived gap filter ONLY if SIP evidence shows large-gap opens
  are negative expectancy. Do not add a threshold speculatively.
- Re-run `entry_backtest*.py` on SIP periodically; add a research-selection (not scanner) variant once
  the premarket shortlist is logged.

## Branch / commits

Branch `feature/premarket-open-entry`:
- `188e22e` primitive + backtests + design doc
- `bf3ef7c` `_execute_opening_orders`
- `d6d25d1` #1–#3 flag-gated (premarket wiring, watchdog reconcile, intraday management-only)
- (a) entry-basis monitoring (eod report + watchdog basis log)

Suite: 899 tests pass. Flag default OFF in code; turned ON via workflow env at rollout.
