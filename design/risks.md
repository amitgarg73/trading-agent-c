# Risks and Mitigations — Trading Agent C

---

## R1: Loop runaway — agents spin without converging

**Likelihood:** Medium  
**Impact:** High — burns API budget, misses premarket window, unpredictable behavior  
**Mitigation:** Hard per-agent tool call cap + absolute time limit (see Q1). Orchestrator
has a "decide with what I have" instruction that fires unconditionally at limit.

---

## R2: Research Agent investigates wrong tickers

**Likelihood:** Medium  
**Impact:** Medium — selects lower-quality trades than Strategy A  
**Mitigation:** Initial candidate list is still scanner-filtered and scored. Research
Agent chooses WHICH to deep-dive, not WHICH universe to start from. Floor is the
same as Strategy A.

---

## R3: Agent disagreement loops (Risk rejects, Research reproposed, Risk rejects again)

**Likelihood:** Low-Medium  
**Impact:** Medium — wastes time, may miss window  
**Mitigation:** One retry max (Q4). Second rejection is final. Orchestrator logs
the disagreement in the trace for post-session analysis.

---

## R4: Latency — agents don't finish before market opens fully

**Likelihood:** Medium  
**Impact:** High — late entries are negative EV (current 3PM cutoff; morning
volatility window closes by 10:30 AM)  
**Mitigation:** Time-based hard stop. If session not complete by 10:20 AM ET,
Orchestrator takes current best state and proceeds. Benchmark latency in shadow
mode before going live.

---

## R5: Tool call results are stale (price moved between Research Agent fetch and execution)

**Likelihood:** High — always true in markets  
**Impact:** Medium — same risk as Strategy A; guardrails price sanity check catches
large deviations  
**Mitigation:** Price sanity guardrail (already in A) still runs as final gate.
Research Agent fetches are inputs to reasoning, not execution prices. Alpaca
fills at market/limit at execution time.

---

## R6: Emergent agent behavior that's hard to predict or test

**Likelihood:** Medium — inherent to multi-agent systems  
**Impact:** High in a financial system  
**Mitigation:** Shadow mode first (Q7). Full trace logging from day one. Each
agent has a constrained tool set — they cannot call tools outside their role.
Orchestrator prompt explicitly prohibits it from expanding agent scope mid-session.

---

## R7: Cost blowup if Research Agent fetches signals for all candidates

**Likelihood:** Low — if Q3 is resolved correctly  
**Impact:** Medium — $0.15/day becomes $0.50+ if unconstrained  
**Mitigation:** Tiered approach (Q3, Option C): one `get_candidates()` call,
then at most 5 deep-dives. Cap enforced in tool implementation, not just prompt.

---

## R8: Trace table becomes the bottleneck (Supabase writes per tool call)

**Likelihood:** Low  
**Impact:** Low — inserts are async-safe; worst case a trace write fails and
the session continues  
**Mitigation:** Trace writes are best-effort (same pattern as _log_run in A).
Failure to write a trace does NOT block trade execution.

---

## R9: Shadow mode comparison is misleading

**Likelihood:** Medium  
**Impact:** High — could validate a bad system  
**Mitigation:** Shadow comparison must run for minimum 10 trading days before
any paper capital decision. Compare not just trade selection but: signal quality
used, tool calls made, agent disagreements, trace depth. Not just "did C pick
the same tickers as A."
