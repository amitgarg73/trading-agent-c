# Market and Scanner stopped reporting on 27 July

Observed 4 Aug 2026 from Provy production data, not from this repo's logs.

## What the data says

Spans per base agent for the Strategy C workflow in Provy production
(`ag_traces`, workflow `c4d90fe7-892a-4504-a3a3-3ff7c102e6ea`):

| agent | total spans | last span |
|---|---|---|
| research | 2871 | **4 Aug** |
| risk | 588 | **4 Aug** |
| orchestrator | 708 | **4 Aug** |
| learner | 223 | 3 Aug |
| **scanner** | 248 | **27 Jul** |
| **market** | 855 | **27 Jul** |
| news | 7 | 6 Jul |
| validator | 1 | 14 Jul |

The pipeline itself is healthy: 3 sessions a day, 44 traces today, zeros only on
weekends. So this is not an ingest outage. Two specific agents stopped.

## Root cause, CONFIRMED 4 Aug 2026 — two different causes

**Scanner: the work still happens, untraced.** `scanner/scanner.py` replaced `agents/scanner_agent.py`
and did not carry the tracing across. It scans ~126 candidates every morning and emitted nothing. The
traced module is imported by nothing but tests.

**Market: the work stopped happening.** `run_market_agent` is only called from
`run_premarket_pipeline`. Premarket now returns before that, at either the opening-entry branch or
`deferred_to_open`. Intraday does not call it either: it hand-builds the report the agent used to
produce, with `decision: "GO"`, `bias: "NEUTRAL"` and `skip_reason: None`. **So the macro gate, and
the conviction-scaled `max_positions`, have not run since 27 Jul.** `check_protection_status()` is
account-level (suspension, daily hard stop), not market conditions.

**The gate was doing real work.** Downstream `skip:*` spans, emitted only by the orchestrator's
market-SKIP branch, appear on 19 Jun, 22 Jun, 3 Jul and 6 Jul. An earlier read of this that said
"0 skips since 18 Jun" was counting a terminal_reason label, not the behaviour.

**No relaxation found in code or config.** The prompt and the three circuit breakers (VIX > 35,
futures < -2%, all three indices < -1%) are untouched since 6 Jun, and `c_agent_config` has no market
or VIX key at all.

## Fix applied, 4 Aug 2026

- **Scanner reports itself.** `run_scanner` takes an optional `tracer`, default `None`, so backtests
  and ad-hoc runs are unaffected. Every exit reports, including the two download-failure paths and the
  already-scored short circuit, because a scan that dies silently looks like a scan that never ran.
- **Market restored OBSERVATION ONLY** on the deferral path. It runs, it is traced, its verdict is
  recorded with `would_have_blocked_trading` and `acted_on: false`, and **nothing reads it**. Non-fatal
  by construction. Restoring the signal and restoring the gate are two decisions; this is only the
  second. A test fails if any future edit branches on the verdict.

**STILL OPEN: whether to act on it.** After a fortnight of "what it would have said", decide between
restoring the gate live and retiring the agent in Provy. That is now an evidence question.

## The earlier, unconfirmed reading

`agents/market_agent.py` and `agents/scanner_agent.py` still exist and still call
`tracer.log_tool_call(...)`, but nothing outside `tests/` and
`generate_agent_docs.py` imports them. That is consistent with the premarket
entry redesign (`design/entry-redesign-premarket-open.md`,
`design/entry-redesign-ROLLOUT.md`) dropping both from the live path around the
same date.

**This needs confirming in this repo.** Two readings and they have different fixes:

1. **Intended.** The redesign replaced them, and the fix is to retire them in
   Provy so they stop being listed and graded.
2. **Unintended.** They should still run and silently fell out of the path, in
   which case premarket is running without a market-conditions or candidate-scan
   step.

## Why it was not noticed

Provy still lists both agents, because they are in `ag_pipeline_agents`, and
still grades Market at quality 50 from stored evals. So Workflow Health shows a
normal row with a verdict and an empty tool strip. **An agent that has stopped
reporting looks the same as an agent that simply did not call a tool this week.**
Filed against Provy separately.
