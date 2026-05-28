# Market Agent — Future Tools (Parked)

Status: PARKED — evaluate after shadow eval runs for 2+ weeks.

These tools were debated and deprioritized relative to economic calendar and treasury
yields. They add signal but at the cost of additional API round-trips, latency, and
trace complexity. Revisit once the V1 vs V2 shadow comparison shows whether more inputs
improve decision quality.

---

## 1. International Overnight Markets

**What it is:** 1-day % change for Nikkei (^N225), DAX (^GDAXI), FTSE (^FTSE).
These close before US pre-market and set the tone for Asian/European risk-off flow.

**Signal value:** A Nikkei -2% night is a leading indicator of what's coming into the
US open. Stronger signal than sector rotation for broad market sentiment.

**Why parked:** yfinance ^N225 / ^GDAXI data can be stale pre-market. Alpaca doesn't
carry these. Reliability risk outweighs the benefit for now.

**Implementation:** yfinance `history(period="2d")` on each index. Same pattern as
`get_futures()`. Straightforward to add.

**Suggested threshold for agent:** If all three major international markets are down
more than 1%, that should carry significant weight toward CAUTION or SKIP.

---

## 2. Put/Call Ratio

**What it is:** CBOE total put/call ratio — options market sentiment. High ratio means
heavy hedging (smart money defensive). Different signal from Fear&Greed — positioned
risk, not survey-based sentiment.

**Signal value:** Elevated put/call (> 1.2) often precedes or coincides with volatile
sessions. Depressed put/call (< 0.7) can indicate complacency or short squeeze risk.

**Why parked:** CBOE publishes this but there is no clean free API. yfinance doesn't
reliably provide it. Would require scraping cboe.com or a paid data source.

**Implementation options:**
- Scrape `https://www.cboe.com/us/options/market_statistics/daily/` (fragile)
- Alpha Vantage or FMP premium tier (requires API key + cost)
- Use `^PCALL` ticker in yfinance (not always available)

**Suggested threshold for agent:** Put/call > 1.2 = lean CAUTION; > 1.5 = lean SKIP.

---

## 3. Pre-Market SPY/QQQ Volume

**What it is:** Volume and direction of SPY and QQQ in pre-market session (4 AM - 9:30 AM ET).
High pre-market volume with direction = conviction. Low pre-market volume = choppy open likely.

**Signal value:** Tells the agent whether there is a tradeable trend or just noise at
open. A SPY pre-market volume 3x the daily average with a clear directional move is a
strong signal.

**Why parked:** Alpaca can fetch pre-market bars but the data interpretation logic needs
care — pre-market volume is thin and can mislead. Need to normalize against historical
pre-market averages, not regular session averages.

**Implementation:** `StockBarsRequest` with `extended_hours=True` on SPY and QQQ,
timeframe 15-minute, from 4 AM ET. Compute volume sum and directional % change.

**Suggested output for agent:**
```json
{
  "spy_premarket_pct": 0.4,
  "spy_premarket_vol_ratio": 1.8,
  "qqq_premarket_pct": 0.6,
  "qqq_premarket_vol_ratio": 2.1,
  "conviction": "HIGH | MODERATE | LOW"
}
```

---

## When to Revisit

Run the shadow eval (V1 vs V2) for at least 15 trading days. Then check:

1. On days where V1 and V2 disagree, which was right more often?
2. On days where V2 said SKIP or CAUTION and V1 said GO, what was the actual P&L?
3. Are there patterns in V2's key_factors that suggest international markets or
   put/call ratio would have changed the call?

If the shadow eval shows V2 making materially better calls with 6 tools, the case
for adding more tools gets stronger. If V2 and V1 agree 90%+ of the time, more tools
add noise without improving signal.
