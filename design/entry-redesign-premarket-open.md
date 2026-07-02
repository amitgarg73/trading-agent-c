# Entry Redesign: Decide Premarket, Enter at the Open

Status: proposed. Owner: Amit. Supersedes the entry-chase guard and staleness gate.

## The one-line change

Move the trade decision before the open and enter at the opening auction, instead of deciding mid-morning and chasing the ask. Delete the chase gate and the staleness gate: once you are not chasing, there is nothing to veto.

## Why (the evidence, not a hunch)

The picks are good; the entry is the leak. Two independent backtests, held selection and exit constant so only the entry timing varies:

1. `entry_backtest.py` — the 46 real filled trades. Chasing bought **+1.59% above the open** and returned **+0.01%/trade ($31)**. The same names entered at the open returned **+1.22%/trade ($1,718)**.
2. `entry_backtest_premarket.py` — top-N scanner picks, out of sample (5/27–7/1), in a down market (SPY −2.85%). Open entry was profitable at every N (+0.27% to +0.33%/trade, +20% to +56% cumulative, beating SPY). Chasing flipped every N to a loss (−0.07% to −0.11%/trade). The entry timing alone is the difference between a winning and a losing strategy.

Selection was never the problem. Late entry is. This matches `design/losing-trades-investigation.md`.

## Why gates were the wrong fix

Every gate we added (chase 0.5%, staleness 4%, earlier SPY and trail gates) is a veto bolted onto a chase-based entry. A veto needs a threshold; a threshold that is right in a choppy market is wrong in a trending one; so it needs re-tuning every regime change. On the strong up-days of 6/30 and 7/1 the gates rejected 100% of candidates and we made zero trades. The gates were the alarm, not the fix. The mechanism was still chasing.

Anchoring the entry to the open removes the need for any threshold. The open is a fixed reference every day. You either get the open price (opening auction) or, with a limit variant, you do not fill. Nothing to tune.

## The design

### Decision moves to premarket

Premarket already scans (`scanner.run_scanner`) and already has an execution path (`premarket._execute_trades`). Today it defers to intraday (`deferred_to_open`) because it cannot submit ordinary orders before the open. The change:

1. Premarket runs the full funnel before the open: scanner → research (`run_research_agent`) → risk (`run_risk_agent`) → orchestrator synthesis, producing the ranked shortlist with sizing. Research already runs in the intraday path with the same signature; here it runs pre-open (some intraday signals such as `rs_vs_spy` and `above_vwap` are null, which research already tolerates).
2. Premarket submits an opening-auction order per approved name, before the auction cutoff.

### Entry becomes an opening-auction order

New execution primitive in `core/alpaca.py`, replacing the chase path for entries:

```
submit_opening_order(ticker, shares, limit_price=None)
  -> market-on-open  (MarketOrderRequest, TimeInForce.OPG)         # default
  -> limit-on-open   (LimitOrderRequest,  TimeInForce.OPG, limit)  # optional gap cap
```

Alpaca constraint: `OPG` time-in-force is valid only on simple market or limit orders, not on bracket/OCO/OTO classes, and must be submitted before ~09:28 ET. So the entry is a standalone opening order. Protection is attached after the fill, which is exactly what the code already does post-fill via `submit_trailing_stop`.

### Post-open: confirm, protect, record

At/after 09:30 (a short poll, or the first intraday cycle):
1. Read the opening fills.
2. Attach the trailing stop (`submit_trailing_stop`, `trail_pct`) and, where research set one, the take-profit. Stop and target reproject off the actual fill, as the current code already does.
3. Write `c_positions` with the real open fill.

### Exit is unchanged

Trailing stop (`trail_pct` 1.5%) + take-profit + EOD force-close. No change. The redesign is entry-only, which is what the backtests isolated.

### Intraday becomes position management

Intraday stops opening new chase entries. It keeps position sync, goal gates, unfill checks, and trailing management. New-entry placement moves out of intraday. Keep the old intraday-entry path behind a config flag for one release as a rollback, then remove.

## What gets deleted

- The chase gate (`is_chasing_entry`, `max_entry_premium`) and the staleness gate in `submit_bracket_order`.
- The `max_entry_premium` param from `core/params.py` and its wiring.
- The intraday new-entry path (after the flagged rollback window).

Deleting these removes the tuning treadmill. There is no premium threshold to set because we never chase.

## Key design decisions

0. **Exact open vs near-open (RESOLVED 2026-07-01, exact open).** An entry-delay sweep on SIP (top-5 scanner picks) showed the edge lives in the first minute: open +0.33%/trade (+40.9% cum), +2m +0.11% (+14%), +5m +0.02% (+2%), +45m negative. A decide-at-9:30 / buy-at-9:32 "near-open" design gives up two-thirds of the edge, so it is rejected. We hit the EXACT open via an OPG order submitted before the ~09:28 cutoff. Unlock: the +40.9% used SCANNER selection, and the scanner already runs premarket with no real-time SIP needed — so scanner picks at the open work today with no data-subscription dependency; LLM research raises the ceiling (+1.24%/trade on the traded set) and only needs its pre-open gate relaxed.
1. **Market-on-open vs limit-on-open.** Default MOO: it fills at the open with near-certainty and reproduces the backtest exactly (buy at the open). It accepts whatever the auction clears, including a gap. LOO (limit at open + a small buffer) caps gap risk but may not fill. Recommendation: ship MOO first because that is what the evidence tested. Do not add a gap threshold speculatively.
1b. **Selection pre-open.** Run the funnel pre-open (relax research's intraday-signal gate to use scanner conviction), with scanner top-N as the fallback when research yields nothing. Both feed submit_opening_order. Scanner-only already beats the current chase; research is the ceiling.
2. **Gap handling.** The staleness gate existed to avoid names that already ran. At the open there is no "already ran" relative to the open. The remaining question is an overnight gap (open far above prior close, e.g. an earnings pop). Do not add a gap filter until the SIP backtest shows large-gap opens are negative expectancy. If it does, add exactly one structural filter (skip open-vs-prior-close gaps beyond a data-derived cutoff), chosen out of sample, not hand-tuned.
3. **Sizing before the fill price is known.** Size on shares from the premarket reference price; the actual basis is the open. Stop and target are set as percentages and reprojected off the fill, as today.
4. **Timing.** Research must finish and orders must be submitted before the ~09:28 ET auction cutoff. The premarket run must start early enough (well before 09:20) for the LLM funnel to complete. Confirm the cron time and add a hard deadline: if the funnel is not done by the cutoff, skip the day rather than fall back to chasing.
5. **Number of names.** The backtest was robust across N = 3/5/8. Keep the current sizing/pool rules; N is not a new knob introduced here.

## Implementation plan

Each phase has its own tests (new and modified files get tests written, run, and passing before commit) and a validation gate. No live cutover until Phase 5 passes.

**Phase 0 — Confirm the evidence (gate before building). PASSED 2026-07-01.**
- Re-ran both backtests on the SIP feed (available on this account; SIP returns the full session, e.g. 391 vs IEX 280 bars on a thin name). Results held: traded set (research-selected) open entry +1.24%/trade ($1,760) vs chase +0.02%/trade ($52), 100% fill; scanner floor open entry +0.23% to +0.33%/trade (+17% to +47% cumulative) vs chase -0.09% to -0.13%/trade, over a down market (SPY -3.05%). Robust to the feed (IEX approx SIP).
- Research-selection ceiling is the traded set (+1.24%/trade at the open). The FULL research shortlist (including chase-skipped names) is not testable yet because proposals are not persisted anywhere except c_positions (only traded names). Recommendation: Phase 1 logs the premarket shortlist, so the shadow phase measures the complete research-selection result on real data.
- Gate: open entry beats chase AND beats SPY on SIP — MET at every configuration in both samples. Proceed to Phase 1.

**Phase 1 — Premarket funnel produces the shortlist.**
- Enable scanner → research → risk → orchestrator in the premarket session, before the open, producing the ranked, sized shortlist.
- No order submission yet; log the shortlist and close the session.
- Tests: premarket produces a non-empty shortlist on a fixture day; tolerates null intraday signals.

**Phase 2 — Opening-order execution primitive.**
- Add `submit_opening_order` (MOO default) in `core/alpaca.py`. Pure of the chase/staleness gates.
- Tests: builds a correct `OPG` market request; limit variant builds an `OPG` limit; rejects submission after the cutoff.

**Phase 3 — Wire premarket to submit opening orders + post-open protect/record.**
- Premarket submits one opening order per approved name before the cutoff.
- Post-open step confirms fills, attaches trailing stop/target, writes `c_positions` with the open fill.
- Tests: fill confirmation path; trailing stop attaches off the real fill; `c_positions` reflects the open basis.

**Phase 4 — Remove the gates and the intraday entry path.**
- Delete the chase and staleness gates and `max_entry_premium`; move intraday to management-only behind a config flag.
- Tests: no code path references the removed gates; intraday no longer opens new positions when the flag is off.

**Phase 5 — Shadow / paper validation.**
- Run the new premarket-open path in the paper account alongside the recorded current behavior for at least two weeks (honors the real-money eval gate: `eval.py --days 14` must pass before any real capital).
- Compare: entry basis vs open (target ~0), fill rate at the open, win rate, P&L/trade, vs SPY and vs the prior chase behavior.
- Gate: entry basis near zero and P&L/trade beats the prior chase.

**Phase 6 — Cutover and rollback.**
- Make premarket-open the default. Keep the intraday-chase path behind the flag for one release, then delete.
- Rollback: flip the flag back to intraday-chase if the shadow metrics regress.

## Risks and mitigations

- **Opening-auction fill uncertainty.** MOO fills at the open with high certainty; LOO may not. Mitigation: MOO default; monitor fill rate in shadow.
- **Auction slippage / thin liquidity at the open.** The open can be volatile. Mitigation: measure realized open-fill vs the printed open in shadow; if slippage is material, switch to LOO at open + a small buffer.
- **Overnight gap risk.** MOO buys the gap. Mitigation: the data decision in Design Decision 2; add a gap filter only if SIP evidence supports it.
- **LLM funnel misses the cutoff.** Mitigation: hard deadline; skip the day rather than chase.
- **Fewer intraday adjustments.** Entry is once, at the open. Mitigation: intraday still manages exits and trailing stops.

## Success metrics

- Entry basis vs the open trends to ~0 (from the current +1.59%).
- P&L/trade beats the prior chase behavior and beats SPY over the shadow window.
- Zero use of the chase/staleness gates because they no longer exist.
- No further entry-threshold tuning: the treadmill ends.

## Files touched

- `sessions/premarket.py` — run the funnel pre-open; submit opening orders; post-open confirm/protect/record.
- `core/alpaca.py` — add `submit_opening_order`; remove the chase and staleness gates from the entry path.
- `core/params.py` — remove `max_entry_premium`.
- `sessions/intraday.py` — management-only; remove new-entry placement (behind a flag first).
- `entry_backtest.py`, `entry_backtest_premarket.py` — the validation harness (already written, read-only).
