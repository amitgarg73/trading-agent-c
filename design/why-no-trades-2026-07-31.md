# Why no trades, 30-31 July 2026

**Not a strategy problem. A market-data problem.** The agent is running correctly, risk is approving
picks, and every approved pick is being killed at the entry gate by an **ask price that is wrong**.

## What the evidence says

**The agent ran.** Premarket plus two intraday scans on both days, all completing.

**Risk approved picks.** 3 on the 14:31 scan, 2 on the 16:31 scan (31 Jul).

**No buy order ever reached Alpaca.** Order history for 31 Jul contains one order: the 08:00 EOD sell
of BLK. The gate fires before `submit_order` is called.

**The gate that fired was the staleness gate**, from the run logs:

```
14:31  TGT  ask $149.61 is 4.3% above proposal $143.41
14:31  MRK  ask $135.00 is 4.4% above proposal $129.37
14:31  ABNB ask $159.12 is 4.9% above proposal $151.71
16:31  TGT  ask $149.61 is 4.1% above proposal $143.71
16:31  DDOG ask $280.75 is 4.0% above proposal $269.90
```

**The proposals were right. The ask was wrong.** Actual traded prices at 14:31 were TGT $143.24,
MRK $129.74, ABNB $151.72 — within ~1% of the proposals and ~4-5% below the ask.

**TGT's ask was $149.61 at 14:31, at 16:31, and still $149.61 at 18:59.** Frozen for four and a half
hours while the stock traded between $143 and $145.

**Live spreads on the same feed, 18:59:** ABNB bid $142.12 / ask $151.81 (**6.4%**), DDOG bid $256.60 /
ask $271.27 (**5.4%**). Those are not NBBO spreads on liquid large caps.

## Root cause

`submit_bracket_order` takes its ask from `get_stock_latest_quote`, which on this account returns the
**IEX-only quote, not the consolidated NBBO**. The account is on the free data plan — a SIP query
returns `subscription does not permit querying recent SIP data`. IEX carries a small share of volume,
so when nothing is resting at the top of its book the quote goes wide and stale.

**The 4% staleness gate is behaving correctly on bad input.** It is comparing a good proposal price
against a fictional ask.

## Effect

| Date | Approved | Filled |
|---|---|---|
| 27 Jul | 2 | 1 |
| 28 Jul | 8 | 3 |
| 29 Jul | 6 | 4 |
| 30 Jul | 8 | 1 |
| 31 Jul | 5 | **0** |
| **Total** | **29** | **9 (31%)** |

## Fixes, in order of cost

1. **Sanity-check the ask before trusting it.** If `ask > last_trade * 1.01`, or the bid/ask spread is
   wider than ~1%, treat the quote as unusable and fall back to the last trade or the current minute
   bar close. Free, small, and makes the gate robust to any bad feed.
2. **Buy the SIP feed** (Alpaca Algo Trader Plus, ~$99/mo) for a real NBBO. The proper fix, and it also
   removes the premarket-bar limitation that has bitten before.
3. Consider whether the staleness gate should reference the last trade rather than the ask at all. The
   gate exists to avoid chasing a stock that has already run; a traded price answers that better than a
   single-venue offer.

## Second, separate bug: the terminal reason lies

`_entry_outcome(0)` returns `intraday_all_rejected` whenever zero trades are placed, **including when
risk approved everything and the entry gate skipped it**. The daily email therefore reads
`terminal=intraday_all_rejected`, which says risk rejected the picks. Risk did not. The
`result_summary` says the truth ("All 3 approved pick(s) skipped at entry gate") but the terminal
reason contradicts it, and the terminal reason is what the report and Provy both key on.

Needs a distinct outcome, e.g. `intraday_entry_gate_skipped`.

## Worth noticing

This is the exact failure Provy exists to catch. Every run completed, reported success, and closed
cleanly. The agent did nothing for two days and nothing in the reporting said so plainly.
