# Outcome ledger settlement gap: 228 of 288 per-ticker predictions never settle

> **CORRECTION 2026-07-27. The central conclusion below is WRONG and is kept only for the record.**
>
> This file concluded that 213 of 228 unresolved rows "had no position opened that day", and called
> that a grain mismatch rather than a bug: Provy predicting on every ticker while the fleet trades a
> handful.
>
> That is not what happened. **Provy dated every prediction with the day the JUDGE ran, not the day
> the work happened** (`writeLedgerPredictions` stamped `businessDate(now())`). The upsert key is
> `(tenant, workflow, entity, business_date)`, so every re-run of `/api/compute/judge` over an old
> session minted a NEW prediction dated today for work that only ever happened once. This fleet calls
> `backfill_server_judge`, so it fired constantly.
>
> On production **233 of 288 ledger rows are artifacts of that**, and **188 of the 228 "unresolved"
> rows are among them**. One premarket session that ran once on 9 July carried nine predictions.
> Of course no position was opened on those days: no work happened. **The fleet was never the
> problem here.**
>
> Fixed in argus #438 (predictions now dated from `ag_sessions.started_at`). The real work-item count
> on production is 84, not 288.
>
> What survives from this document: the delivery-verification defect (the transport could not report
> a failed push) was real and is fixed. The "93% is a product decision, not a bug" framing built on
> top of the grain-mismatch conclusion is **not** to be relied on until re-measured against a clean
> ledger.


Filed 2026-07-26. Status: open, not yet actioned.

GitHub issues are disabled on this repo, so this file plus its commit is the change record.

## What happens today

`sessions/eod.py` calls `push_trade_outcomes(get_today_trades(session_id))` at end of day.
`evals/outcomes.py:215` posts one `/api/ingest/outcome` per trade, keyed on ticker:

```python
_ingest_post("/api/ingest/outcome", {
    "entity_id":   ticker,
    "value":       float(pnl),
    "source":      "confirmed",
    "occurred_at": t.get("close_time"),
})
```

It skips anything whose `exit_reason` is in `_NO_TRADE_EXITS = {"unfilled", "test_cleanup"}`,
and anything with a null `realized_pnl`. So an outcome is reported only for a trade that
actually filled and closed with a money result.

Provy, for its part, writes a ledger row for **every ticker the agents made a call on**, not
only the ones that traded. Rows that never receive an outcome age from `pending` to
`unresolved` after the outcome window.

## The measurement

Production, `ag_outcome_ledger`, 2026-07-26:

| reconciliation | rows | with a settled `actual_label` |
|---|---|---|
| unresolved | 228 | 0 |
| pending | 31 | 0 |
| diverged | 21 | 21 |
| matched | 8 | 8 |
| **total** | **288** | **29** |

29 of 288 predictions ever settled. Cross-tabbed against the agents' own call for that ticker:

| reconciliation | PROPOSE | SKIP |
|---|---|---|
| unresolved | 137 | 91 |
| pending | 18 | 13 |
| diverged | 14 | 7 |
| matched | 3 | 5 |

## What this is not

The obvious reading is "a SKIP never trades, so it has no P&L, so it cannot settle." That is
true and it is not the main story. **137 of the 228 unresolved rows were PROPOSE**, so the
larger share is tickers the agents actively proposed and which still produced no reported
outcome.

## What the investigation established (2026-07-26)

The proposals were counted against `c_positions`. Ledger rows live in Provy production
(`eckthcvacrkfjihluubt`); `c_positions` lives in `fpuyabfxtrzwciehfetk`, so this was a join
done outside the database.

**93% of the gap is not a bug. It is a grain mismatch.** Of the 228 unresolved rows, **213 had
no position opened that day at all**. Provy writes a ledger prediction for every ticker the
agents evaluate; the fleet trades a handful. From 2026-07-09 onward the watchlist grew to
22 to 29 predictions a day against 0 to 5 closed trades. Before that it was 6 to 10 predictions
against 1 to 7 trades, which is why the early days settled and the later ones did not. Nothing
broke. The denominator moved.

**The remaining 15 are a real defect.** Those had a genuinely closed position with a realized
P&L, and their ledger row still never settled. Two causes, both now fixed:

1. **The transport could not say no.** `trace/logger.py:_ingest_post_raw` ended in
   `except Exception: pass` and returned nothing. `push_trade_outcomes` counted its own loop
   iterations, so it reported deliveries it had not made and `tracer.log_decision(
   "ledger_outcomes_pushed")` recorded a number that proved nothing. A day where every push
   was dropped logged identically to a day where every push landed.

   This is not hypothetical. The retired `argusobs.vercel.app` still answers 200 and still
   accepts the fleet's ingest key: probed on 2026-07-26, it returned the same
   `400 entity_id is required` as production. A stale `ARGUS_URL` therefore fails in exactly
   this shape. The CI secret was write-only and unverifiable until it was reset on 2026-07-26.

2. **The outcome is not pinned to its own prediction.** The push omits `session_id`, so
   `reconcileOutcome` falls back to "the most recent unanswered row for this entity", ordered by
   `predicted_at`. On a fleet that sees the same ticker on many days, an outcome can answer the
   wrong day's prediction.

Defect 1 is fixed. The transport now returns whether the POST was accepted, prints a line when
it was not, and the EOD trace records `reportable` and `dropped` alongside `count`, so a
shortfall shows up in the run rather than in the ledger weeks later.

**Defect 2 is NOT fixed, and the obvious fix is wrong.** Pinning EOD's `session_id` was tried
and reverted before it ever ran. Argus writes the ledger prediction from whichever session
judged the ticker: **45 of 288 rows on production belong to intraday sessions, and 21 of the 29
rows that have ever settled are among them.** `get_today_session_id()` returns the PREMARKET
session, so pinning it would have missed exactly the rows that currently reconcile. The loose
fallback is load-bearing today.

The correct fix is on the Argus side: match the outcome on entity plus business date rather
than "most recent unanswered row". The fleet cannot pin what it does not know, and it has no
way to learn which session Argus attributed a prediction to.

## Why it matters beyond the ledger looking untidy

Provy's Behavioral Attribution engine correlates agent decision patterns against per-work-item
outcomes. It has 463 decisions on production, covering 395 work items, and produces zero
findings. Of those 395, **51 have an outcome to correlate against** once the settled ledger rows
are counted (23 were already analysable through session-grained evaluations; the settled ledger
adds 28). That is still thin for the engine's `minSample` of 8 and `minLift` of 1.3.

The settlement rate is the binding constraint on that feature. Raising it is worth more than
any change on the Provy side. (Provy has a separate, smaller defect where the engine reads the
wrong outcome table entirely: amitgarg73/argus#433.)

## Status

**Fixed 2026-07-26:** the two defects above, covering the 15. Delivery is now verified rather
than assumed, and outcomes are pinned to their own session.

**Still open, and the whole 93%:** what a work item that never traded should report. That is
option 1 below and it needs a product decision, not a code change.

**Not done:** backfilling the 15 already-missed outcomes. The P&L for those trades is known and
sitting in `c_positions`, so it can be replayed through `/api/ingest/outcome`. It writes real
business outcomes into the production ledger and moves the trust score, so it needs an explicit
go-ahead rather than being folded into a fix.

## Options, none chosen

1. **Report "no trade" as an outcome rather than as silence.** A SKIP that avoided a loss and a
   PROPOSE that was vetoed are both real results the pipeline produced. Today they are
   indistinguishable from an outcome that got lost. This is the largest change and the one that
   would move the number most.
2. **Push outcomes for positions still open at EOD** as a provisional value, reconciled on close.
   Narrower, and it only helps the "still open" slice.
3. **Stop writing ledger predictions for tickers that cannot settle**, so the ledger stops
   carrying rows nothing will ever answer. This makes the ledger honest without producing any
   new signal, and it would shrink the denominator rather than grow the numerator.

Option 1 is the only one that increases what Behavioral Attribution can analyse. It is also the
one that needs a decision about what a SKIP's correct outcome label is, which is a product
question, not a code question.

## Where to look

- `evals/outcomes.py:215` `push_trade_outcomes`, and `_NO_TRADE_EXITS` above it
- `sessions/eod.py:413` the only call site
- `scripts/reconcile_per_trade.py` is a manual analysis script, not wired into the pipeline
- Provy side: `web/lib/ledger.ts` `reconcileOutcome` and the pending-to-unresolved ageing
