# Why we are not making money (plain English)

Date: 2026-06-29. Based on the actual trades in the system, not theory. Covers Strategy B and Strategy C. Written without jargon.

## The short answer (corrected after the full investigation, June 30)

An earlier read in this document was that the system cannot pick winners. The deeper, holistic review reversed that, and this is the corrected conclusion: **the picking is actually good. We lose the money in execution.**

When we measure the stocks the agents chose by their honest potential (buy at the open, sell at the close), they average about **+1.0% with a 78% win rate** — the scanner and the research agent are choosing winners. But the **actual** result on the same trades is **-0.08% with a 38% win rate.** The gap, about **-1.1% per trade**, is lost between picking the stock and managing the trade: we enter late (about a third of the way into the day's move, after it has already started) and the trailing stop plus forced end-of-day exits sell winners before they play out.

So the problem is not the scanner and not the research agent. It is entry timing and the exit/stop logic. This is still paper money, which is the good news.

The sections below are the investigation that led here. Where an earlier section says "the picker is the problem," read this section instead.

## What the numbers actually say

**Strategy B, last 5 trading days:**
- Made about **+$164 total**, green on 3 of the 5 days.
- But only **3 out of 10 trades actually made money** (a 30% win rate).
- It stayed positive only because the winners were big. One trade (UBER, +$119) carried almost the whole week. Take that one trade away and the week is roughly flat.
- It is also nowhere near the goal of $500 a day. It made about $33 a day.

**Strategy C, last ~6 trading days (Jun 15 to 22):**
- Roughly **break-even, slightly down (about -$8 total)**.
- Made money on only **1 of 6 days**. So 5 days out of 6 were red. That is why it feels like losing every day.
- Here is the worrying part: the agents graded their own reasoning **high** (about 0.86 out of 1.0), but that grade had **almost no connection to whether the trade made money.** The agents were confident and wrong.

So you are right to be worried, and you are right about where the problem is: the **research and selection** step. We pick 120-plus stocks in the morning, narrow to a handful, and the handful are close to a coin flip, sometimes worse.

## The test that settles it: are the agents better than a dumb rule?

We ran exactly that, over 14 trading days, same money each day, comparing Strategy C's actual picks to three brainless no-AI rules:

| What | Result over 14 days |
|---|---|
| Our agents (what we actually did) | lost $158 |
| Just hold the market (SPY) | lost $373 |
| Average stock in our morning 120-pool | lost $257 |
| Dumb momentum: buy yesterday's biggest gainers, no AI | made $152 |

What it means, honestly:
- The agents are NOT worthless. They beat holding the market, and they beat picking at random from our own morning list, so selection adds a little.
- But a one-line momentum rule with no AI made money while the agents lost, a swing of about $310 on the same days and the same capital. We do a lot of expensive reasoning to land below a rule you could write on a napkin. That is the heart of the overconfidence problem.
- Caveat: 14 days is a small, jumpy sample. The momentum rule is driven by a few big days, so its lead could shrink or flip with more data. Directional, not proof.
- Side note on the Alpaca subscription: the test found the fuller market-data feed (SIP) is blocked on the current plan, so a subscription would unlock more data. But the dumb rule beat the agents on the SAME basic feed the agents already use, so the feed is not why we lose. This confirms a subscription will not fix the P&L.

## The three real reasons

**1. The picker adds only a sliver of edge, and loses to a dumb rule.**
Most individual trades lose. The agents do slightly better than picking at random from our own morning list, and better than just holding the market, so selection is not worthless. But a brainless momentum rule (buy yesterday's biggest gainers, no AI) made money over the same 14 days while the agents lost. We are doing a lot of expensive reasoning to land below a one-line rule. The full scoreboard is below.

**2. The agents are confident but wrong.**
Strategy C's agents rate their own work highly, but those high grades do not line up with making money. The system thinks it is doing great while losing. So we cannot trust the agents' own confidence score as a sign a trade will work. The market is the only grader that counts, and it disagrees.

**3. Strategy C also sells its winners too early.**
Even when C picks a stock that goes up, it often still sells at a loss. The automatic "trailing stop" is set wider (1.5%) than the size of the moves we actually get on most days (often under 1%). So a stock can go green, never rise enough to lock in a profit, then drift back and get sold red. Strategy B does not have this problem, and that is a big reason B is positive and C is not. B lets its winners run; C cuts them off.

## Will an Alpaca subscription help?

Honest answer: **a little, but it is not the cause, and it will not fix the losses.** You are right not to believe in it as the fix.

- A paid Alpaca data plan gives fresher, more complete market data. That would help at the edges: fewer stale prices (we have already had to hand-patch a stale price once), and slightly better timing on entries.
- But better data does not turn a coin-flip picker into a winner. If the research agent cannot tell winners from losers, feeding it cleaner numbers faster does not change that. The problem is the **decision**, not the **data feed**.
- My recommendation: do not spend on it expecting the P&L to turn around. Fix the picking first. Revisit a data plan later only if timing and stale prices are proven to be costing real money.

## The honest caveat you should hear

We only have a handful of real trading days (about 5 for B, 6 for C). That is a very small sample. It is not enough to *prove* the agents have zero skill, and it is not enough to *prove* they have skill either. The flywheel has barely started turning. So the truthful position is: **we have not yet shown the system can make money, and the little evidence we have points at the picker as the weak link.** Anyone who tells you they are sure either way, in either direction, is guessing.

## What I would actually do, in order

1. **Fix the picker before anything else.** This is where the money is lost. Concretely: test whether the research agent beats a dumb baseline (for example, "buy the strongest-trending stocks, no agent"). If it cannot beat that, the agents are not earning their keep and the selection logic needs to change. Trade fewer, higher-conviction names rather than a handful of so-so ones.
2. **Stop trusting the agents' own confidence score as a money signal.** Keep it for spotting reasoning problems, but it does not predict profit, so do not size or gate trades on it.
3. **Fix Strategy C's early-exit problem** so its winners can run like B's. This is a smaller, faster fix than the picker and would stop C from turning green trades into red ones.
4. **Get more days of data before any big decision or any real money.** With this few trades, we are partly looking at noise.
5. **Do not buy the Alpaca subscription as a fix.** It is not the root cause.

## Update: correction on the trace pipeline, and the momentum-backbone re-run

A first attempt at the agent-veto test looked like it hit a wall, because the local `c_traces` and `c_sessions` tables stop on June 4. That was a false alarm. Those local tables were **deprecated on June 4** when trace writing moved to the Argus observability tables (`ag_*`). The pipeline is healthy: as of late June, Provy has current trading-C data in `ag_sessions`, `ag_traces` (2,000+ traces since June 10), `ag_evals`, and `ag_outcomes`, all running through June 29. The agents are running and fully instrumented. Nothing is "trading blind," and Provy is receiving everything.

The momentum-backbone test, re-run against `ag_traces` (covering the losing days), over the 14-day window:

| Strategy | Result |
|---|---|
| Pure momentum | +$152 |
| Momentum with the agents' veto layered on | -$31 |
| The agents' actual trades | -$158 |

Using the agents as a filter on momentum made it WORSE, not better. And on the few momentum picks the agents actually evaluated, their judgment ran backwards: every stock they rejected went up, and the only loser was one they approved.

Honest caveats: this rests on a tiny overlap. The agents had an opinion on only 8 of 45 momentum picks (about 18%), and the skill check is 5 rejected vs 3 approved, far too small to be statistically real. So it is directionally negative, not proof. The deeper reason for the low overlap is that the agents and momentum fish in different ponds: the agents research their own scanner shortlist (about 6 to 16 names a day), which rarely includes the prior-day top gainers momentum chases. So this is really two different strategies, and over this window momentum was the better one.

Bottom-line update: neither "agents pick from scratch" nor "agents veto momentum" is justified by the data we have. On what we can measure, the agents add no value over plain momentum and slightly subtract. Before removing them, get more days of data, but there is no evidence of positive selection skill, and the early evidence is negative.

## The execution fix: it is the late entries, not the trail

We split the -1.1%/trade drag into its parts. It is almost entirely entry timing:
- We buy on average **+1.4% ABOVE the day's open**, because we enter 30 to 180 minutes after the open (most fills cluster 10 to 11am and 12 to 1pm), after the move we correctly spotted has already run. That late entry is about **136% of the drag.**
- The exit/trail, in aggregate, is NOT the problem. It is actually helping slightly (it cut losers on down days). It just never lets a winner run.

The before/after on the same trades (about $100K notional):
- Current: **-$346**.
- Enter at the open, keep the same exits: **+$1,054.** This single change flips the book from a loss to a clear profit.
- Hold to the close instead: -$661 (worse, because the trail was saving money on the choppy down days).

So fixing the entry is the whole game. Conservatively, even capturing half the entry gap takes the book from about -0.35%/trade to about +0.35%/trade, i.e. profitable.

Recommended change:
1. **Entry (the fix):** enter at or near the open, or add a guard that rejects an entry once the stock is already more than about 0.5% above the open (stop chasing). There is no entry-timing guard in the params today; this is a new gate.
2. **Trail (a small tweak, not a fix):** keep the 1.5% trail, but do not start trailing until the position is up about 1%, so it stops firing on open noise and lets winners run. Do not widen it.

One data gap to fix while we are here: about a quarter of the trades (the forced end-of-day closes) store only the realized P&L, not an exit price, which blocks clean analysis. Log the exit price on every close.

## Bottom line (corrected)

We are not losing big money, and it is paper money by design. The honest, corrected read after the full investigation: **the system CAN pick winners** (the chosen stocks average about +1% open-to-close, 78% win). We lose it in execution, and almost entirely because **we enter late** (buying about 1.4% above the open, after the move has run). The trail and exits are fine; they are not the leak. So the fix is entry timing: enter at or near the open, or stop chasing names that have already moved. On the trades we have, that one change flips the book from -$346 to about +$1,054. Fix the entry and the same good picks turn from a loss into a gain.
