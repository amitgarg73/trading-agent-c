# Wiring the settled outcome to Provy properly

Status: DESIGNED, not built. Diagnosed 29 July 2026 against production.

## What happens today

Two separate paths, and only one of them reaches Provy.

**Path A — the ledger push. Works.** `push_trade_outcomes` (`evals/outcomes.py`) POSTs one row per
closed trade to `ARGUS_URL/api/ingest/outcome`:

```json
{ "entity_id": "<ticker>", "value": <realized_pnl>,
  "source": "confirmed", "occurred_at": "<close_time>", "session_id": "..." }
```

That is what produces the whole right-hand panel on Command Center: 82 runs reconciled, Proved 6%,
$691 in real losses, 13 confident-and-wrong catches. It needs no contract and no signal names, only a
number per entity, and it is correct.

**Path B — the risk signals. Never arrives.** `write_eod_outcome_metrics` computes
`max_drawdown_pct`, `within_limits` and `max_single_trade_loss_pct` (plus `win_rate`,
`trades_total`) and writes them into `SUPABASE_URL`, which is **fpuyabfxtrzwciehfetk, the pre-prod
database**. `ARGUS_URL` is production. So the contract's own evidence is written where production
cannot see it.

## What that costs

The outcome POST carries **no `signals` bag**, so no contract condition can be graded from the
settled result. On production:

| Condition | Signal | Graded from |
|---|---|---|
| s1, f1 | `realized_pnl` | the agents' own **trace payloads** |
| s2 | `max_drawdown_pct` | nothing |
| s3 | `within_limits` | nothing |
| f2 | `max_single_trade_loss_pct` | nothing |
| r1 | none (compound) | never gradeable |

The uncomfortable half is the first row. The two conditions that do grade are grading from what the
agents said about themselves, not from what settled. That is the estimated-versus-confirmed line the
whole product exists to hold, and this fleet is on the wrong side of it while looking fine.

## The three changes

**1. Send the risk signals in a session-level outcome post.** The numbers already exist; only the
delivery is missing. Grain matters: the per-trade push is per TICKER and the risk metrics are per
SESSION, so they must not be duplicated onto each trade. One post at EOD:

```json
{ "entity_id": "<session-level id>", "session_id": "...", "source": "confirmed",
  "signals": { "realized_pnl": …, "max_drawdown_pct": …,
               "within_limits": …, "max_single_trade_loss_pct": … } }
```

Mixing the two grains is the mistake already recorded against the ledger, whose key is
(entity_id, business_date). Keep them separate.

**2. Leave the per-trade `value` pushes exactly as they are.** They feed the ledger, they are the
grain its key expects, and nothing above changes them.

**3. Point `write_eod_outcome_metrics` at production, or retire it.** Writing the contract's evidence
into a database Provy production cannot read is either the wrong target or, once (1) ships,
duplicated work. Decide which; do not leave both.

## What changes on the Provy side once this lands

`realized_pnl` starts grading from the settled outcome rather than the trace, and the three risk
conditions start grading at all. That also makes them confirmable, which is what takes the fleet off
the "Not counted" label that gating now applies to an unchecked contract.

## Constraint that does not move

Provy is never in the trade critical path. All of this is post-trade reporting, on the EOD path, and
a failure to deliver must stay a logged warning rather than anything that can affect a decision.
