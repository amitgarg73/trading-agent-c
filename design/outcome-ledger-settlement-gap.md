# Outcome ledger settlement gap: 228 of 288 per-ticker predictions never settle

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

## What is not yet known

The measurement above is solid. The cause is not. A PROPOSE can fail to become a closed trade
through at least these paths, and this document does not claim which dominates:

- vetoed downstream by risk or the orchestrator before an order was placed
- an order was placed and never filled (`exit_reason = "unfilled"`, explicitly excluded)
- blocked by the chase gate (loosened 0.5% to 2% recently, so the mix may have shifted)
- still open at end of day, so no `realized_pnl` yet

Working that out means counting proposals against `c_positions` per session. It has not been
done. Do not act on a guess here: the whole point of the Provy work this feeds is that a
plausible cause is not a verified one.

## Why it matters beyond the ledger looking untidy

Provy's Behavioral Attribution engine correlates agent decision patterns against per-work-item
outcomes. It has 463 decisions on production and produces zero findings. Restricted to work
items whose outcome actually settled, it has **28 analysable items across 12 sessions**. That
is too thin for the engine's `minSample` of 8 and `minLift` of 1.3 to say much.

The settlement rate is the binding constraint on that feature. Raising it is worth more than
any change on the Provy side. (Provy has a separate, smaller defect where the engine reads the
wrong outcome table entirely: amitgarg73/argus#433.)

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
