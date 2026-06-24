# Losing trades investigation

Status: OPEN. Logged 2026-06-24 after EOD closed 1W/4L for -$87.02.

We cannot keep taking losing trades. The Argus Outcome Ledger now flags these as
"predicted success -> confirmed fail," which gives us a way to point at which agent's
decision is failing instead of guessing.

## Evidence (2026-06-24, from c_positions)

| Ticker | Entry | Exit | Exit reason | Max favorable excursion* | P&L | directional_hit | Entry (ET) |
|---|---|---|---|---|---|---|---|
| GE | 364.28 | 363.59 | NATIVE_TRAIL | ~+1.33% | -4.14 | true | 10:32 |
| TER | 427.79 | 422.00 | NATIVE_TRAIL | ~+0.15% | -46.34 | false | 12:32 |
| CAT | 1001.06 | 987.28 | NATIVE_TRAIL | ~+0.13% | -41.34 | false | 12:32 |
| V | 331.67 | 332.23 | eod_forced | ~+0.17% | +5.60 | true | 12:32 |
| JNJ | 241.08 | 241.00 | eod_forced | ~0% | -0.80 | false | 12:32 |

\*Implied from where the 1.5% trailing stop fired (exit / 0.985).

## Three hypotheses to test

1. **Trail width vs typical move.** Trail is 1.5% (`core/params.py`). A trailing stop only
   locks a profit once the position rises more than the trail width above entry. None did today
   (best was GE at +1.33%, still under 1.5%), so the trail exits at a loss even on positions that
   went green. Either the trail is too wide for the moves we actually get, or we need a different
   model (tighten after a partial move, or a time-based exit). Measure trail width against the
   distribution of max favorable excursion across more days.

2. **Late entries.** CAT, TER, V, JNJ all entered ~12:32 PM ET (2nd intraday session); only GE
   was earlier (~10:32). Midday entries leave little runway and the move may already be spent.
   Look at entry time vs same-day forward return.

3. **Wrong-direction picks (the deeper issue).** `directional_hit` was false on 3 of 5 (TER, CAT,
   JNJ). Research/selection predicted up moves that did not happen. This is upstream of the stop:
   we are picking losers, not just managing them poorly.

## How the Outcome Ledger helps

The ledger reconciles each ticker's trace-based prediction against realized P&L. The
"predicted success -> confirmed fail" group is exactly the set of trades where the agents were
confident and wrong. Drilling into those traces (research thesis, risk verdict, entry timing) is
the path to root-causing whether the failure is selection, timing, or stop management.

## Done when

- A short analysis across more than one day of trades on the three hypotheses above.
- A concrete change (trail model, entry-time gate, or selection threshold) with a before/after
  expectation.
