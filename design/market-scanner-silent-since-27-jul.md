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

## The likely cause, unconfirmed

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
