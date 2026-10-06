# EOD resilience: open questions for the founder (argus#1566, argus#1567)

Written 5 Oct 2026. The additive parts are built on branch `fix/eod-resilience` (explicit EOD date,
dry run, watchdog advice, read-only broker report). Everything below changes how positions are
closed or how fills, brackets and quantities are recorded, so none of it is built. Each question has
a recommendation.

## (a) What should `close_all_strategy_positions` select?

Today (`core/alpaca.py`): it takes every broker holding, then keeps only symbols that have an order
whose client id starts `stratc_` placed in the last 2 days. Anything else is left alone. If the order
lookup itself fails, it falls back to closing every holding.

Consequence, proven from the code and the 5 Oct finding: CL, JPM and PSX had no recent tagged order
(the entries were in August) and their database rows had been marked closed from a snapshot on 14 Aug,
so nothing selected them for seven weeks. The 2-day window also means a position held longer than two
days (an entry that never reached EOD, or a missed EOD followed by another) silently falls out.

Two more things to know. The fallback "close all" fires on an API hiccup, which is the opposite
failure (it would close anything in the account). And `cancel_all_orders()` runs first and cancels
every open order, not only strategy orders.

Recommendation: do not widen the closer. The account is dedicated to Strategy C, so any holding with
no open database row is a finding, not an instruction. Report it every EOD (built: the broker
reconciliation line in the EOD alert and the `broker_reconciliation` trace decision) and let a human
decide to close it. Separately, make the fallback fail safe: if the tag lookup fails, close nothing
beyond rows the database says are open, and alert. Awaiting your decision on both.

## (b) Why do database share counts differ from broker quantities?

What the code proves: `c_positions.shares` is the quantity REQUESTED, never the quantity filled.
`premarket.py` and `intraday.py` compute `shares` from the sizing, submit the bracket, and insert
that same number. `submit_bracket_order` returns only an order id and a price, and treats
`partially_filled` as filled (it stops polling at the first partial and cancels the bracket legs).
`get_bracket_status` returns an entry price but no filled quantity. Nothing afterwards reads
`filled_qty`. So a partial fill, or an entry that was accepted and then expired or was cancelled
while the row stayed open, leaves the row saying more shares than the broker holds. Realized P&L is
then `(exit - entry) * shares` with the requested number, so the error carries into P&L, scoring and
the daily performance row. The trailing stop is also submitted for the requested quantity, which the
broker can reject when it exceeds what is held.

What the code does not prove: that this explains CL (32), JPM (8) and PSX (3). Those rows were closed
from a snapshot on 14 Aug, so the quantity at that moment is not in the code path I can read.

What to measure (read-only, safe): for every `c_positions` row from the last 60 days, fetch its
`alpaca_order_id` from the broker and compare `filled_qty` with `shares`; count rows where they
differ and sum the P&L impact. For the three, pull the August orders and the 14 Aug snapshot.

Recommendation: record `filled_qty` next to `shares` (new column, additive), take realized P&L from
`filled_qty`, and size the trailing stop from it. This is recording logic, so it waits for your
approval of the design. Measure first; if the differ-count is zero, the cause is elsewhere (the
14 Aug snapshot) and the change is not needed.

## (c) What protection should exist overnight if EOD is missed?

Today: `cancel_all_orders()` removes every bracket leg at EOD, then the market closes are submitted.
EOD fires at 16:05 ET (cron `5 20 * * 1-5`, UTC, so 15:05 ET in winter), so in summer the market is
already closed and the closes queue for the next open. Between the cancel and the open, the holdings
have no stop and no target. If EOD is missed entirely the brackets survive (DAY legs expire at the
close, so the position is unprotected anyway), and the holding rides until a human acts. The watchdog
only alerts after 16:30 ET and cannot act.

Recommendations, in order of size:
1. Cheapest: do not cancel brackets until a close order can actually fill. After the close, cancel
   only the legs for a symbol when its close is accepted, and skip the cancel when the market is
   closed (the close then queues and the bracket stays as protection until the open). Needs your
   approval because it changes the order of broker writes.
2. Run EOD at 15:55 ET year-round as the design intended (3:55 PM per CLAUDE.md), so closes fill in
   the session. The cron time is outside the repo (cron-job.org), so this is a settings change, not a
   code change, but it is the largest single risk reducer.
3. Keep the watchdog alert (now with the re-run command), and treat the reconciliation line in the
   EOD alert as the nightly check that nothing is unaccounted for.
