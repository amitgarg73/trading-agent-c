"""
READ-ONLY: test the PROPOSED design — decide premarket, buy at the OPEN — on a
premarket-decidable pick set, vs deciding late and chasing.

The pick set is the scanner pool (c_scan_results), which is scored BEFORE the open
— the only pre-open signal we have (premarket currently defers, so there are no
stored premarket proposals). Take the top-N by score each day, then compare:

  OPEN  (proposed) -- buy at the day's open (premarket decision + opening order)
  CHASE (status quo proxy) -- buy CHASE_DELAY_MIN minutes after the open (late entry)

Same trailing exit for both (trail_pct + EOD close), so only the entry differs.
SPY open->close is the market benchmark.

Honesty:
- This is OUT OF SAMPLE from the 46 traded names (scan pool is 5/27-6/22).
- Selection here is top-N SCANNER score, which is WEAKER than the LLM research
  ("scanner score weakly predicts; research is the best funnel step"). So this
  tests the ENTRY design on a weaker selection than the real pipeline — a
  conservative floor. Real research picks + open entry should do better.
- No lookahead (score is pre-open; exits use only bars after entry).
- IEX minute bars; treat P&L as directional, confirm on SIP before shipping.

NO WRITES. Reads c_scan_results (Supabase) + Alpaca minute bars only.
"""
import os
from collections import defaultdict
from datetime import datetime, timezone
import pytz
from dotenv import load_dotenv
load_dotenv()

from core.db import get_client
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

_FEED = DataFeed.SIP if os.environ.get('BACKTEST_FEED', 'iex').lower() == 'sip' else DataFeed.IEX

N_VALUES        = [5]                     # focus one basket size for the delay sweep
DELAY_SWEEP_MIN = [0, 2, 5, 10, 20, 45]   # minutes after the open the entry happens (0 = the open)
CHASE_DELAY_MIN = 45                       # kept for the old open/chase print path
TRAIL_PCT       = 0.015                    # matches core/params.py
ET = pytz.timezone("America/New_York")

# ---- premarket pick set (read only) ----
c = get_client()
scan = []
start = 0
while True:
    r = c.table("c_scan_results").select("date,ticker,score,price").range(start, start + 999).execute()
    scan.extend(r.data)
    if len(r.data) < 1000:
        break
    start += 1000

by_day = defaultdict(list)
for s in scan:
    if s.get("ticker") and s.get("score") is not None:
        by_day[s["date"]].append(s)
days = sorted(by_day)
print(f"Scan pool: {len(scan)} rows over {len(days)} days ({days[0]} -> {days[-1]}).\n")

# ---- minute bars ----
adata = StockHistoricalDataClient(os.environ["ALPACA_API_KEY_ID_C"], os.environ["ALPACA_API_SECRET_KEY_C"])

def fetch_day_bars(tickers, day_str):
    day = datetime.strptime(day_str, "%Y-%m-%d")
    start_utc = ET.localize(day.replace(hour=9, minute=30)).astimezone(timezone.utc)
    end_utc   = ET.localize(day.replace(hour=16, minute=0)).astimezone(timezone.utc)
    out = defaultdict(list)
    try:
        req = StockBarsRequest(symbol_or_symbols=sorted(set(tickers)), timeframe=TimeFrame.Minute,
                               start=start_utc, end=end_utc, feed=_FEED)
        df = adata.get_stock_bars(req).df
    except Exception as e:
        print(f"  WARN: bars failed {day_str}: {e}")
        return out
    if df is None or len(df) == 0:
        return out
    for (sym, ts), row in df.iterrows():
        out[sym].append({"ts": ts.tz_convert(ET), "open": float(row["open"]), "high": float(row["high"]),
                         "low": float(row["low"]), "close": float(row["close"])})
    for sym in out:
        out[sym].sort(key=lambda b: b["ts"])
    return out

# take top-N once at the largest N, fetch those bars per day
topN_by_day = {d: [s["ticker"] for s in sorted(by_day[d], key=lambda x: x["score"], reverse=True)[:max(N_VALUES)]]
               for d in days}
bars_by_day = {d: fetch_day_bars(topN_by_day[d] + ["SPY"], d) for d in days}

def simulate_exit(bars_after, entry_px):
    """Trailing stop (trail_pct) + EOD close. No fixed target (scanner picks have none)."""
    high = entry_px
    for b in bars_after:
        high = max(high, b["high"])
        if b["low"] <= high * (1 - TRAIL_PCT):
            return high * (1 - TRAIL_PCT)
    return bars_after[-1]["close"] if bars_after else entry_px

def trade(bars, delay_min):
    """Enter delay_min minutes after the open (0 = the open). Returns (basis_vs_open, pnl_pct) or None."""
    if len(bars) < 3:
        return None
    open_px = bars[0]["open"]
    if not open_px:
        return None
    if delay_min <= 0:
        idx, entry_px = 0, open_px
    else:
        idx = next((i for i, b in enumerate(bars) if (b["ts"] - bars[0]["ts"]).total_seconds() >= delay_min * 60), None)
        if idx is None or idx >= len(bars) - 1:
            return None
        entry_px = bars[idx]["open"]
    exit_px = simulate_exit(bars[idx + 1:], entry_px)
    return entry_px / open_px - 1.0, exit_px / entry_px - 1.0

def spy_oc(day):
    b = bars_by_day.get(day, {}).get("SPY")
    return (b[-1]["close"] / b[0]["open"] - 1.0) if b and b[0]["open"] else None

def summarize(rows):
    if not rows:
        return None
    n = len(rows)
    wins = sum(1 for _, p in rows if p > 0)
    return {"n": n, "win": wins / n, "basis": sum(b for b, _ in rows) / n,
            "avg": sum(p for _, p in rows) / n, "sum": sum(p for _, p in rows)}

# SPY benchmark over the covered days
spy = [spy_oc(d) for d in days]
spy = [x for x in spy if x is not None]
print(f"SPY open->close over {len(spy)} days: avg {sum(spy)/len(spy):+.2%}/day, cumulative {sum(spy):+.2%}\n")

N = N_VALUES[0]
print(f"Entry-delay sweep (top-{N} scanner picks/day, same trailing exit). Delay 0 = the open.\n")
print(f"{'delay':>6} | {'trades':>6} | {'win':>4} | {'basis':>7} | {'avg/tr':>7} | {'cumulative':>10}")
print("-" * 60)
for delay in DELAY_SWEEP_MIN:
    rows = []
    for d in days:
        picked = [s["ticker"] for s in sorted(by_day[d], key=lambda x: x["score"], reverse=True)[:N]]
        for t in picked:
            bars = bars_by_day.get(d, {}).get(t)
            if not bars:
                continue
            r = trade(bars, delay)
            if r:
                rows.append(r)
    s = summarize(rows)
    if s:
        label = "open" if delay == 0 else f"+{delay}m"
        print(f"{label:>6} | {s['n']:>6} | {s['win']:>3.0%} | {s['basis']:>+6.2%} | "
              f"{s['avg']:>+6.2%} | {s['sum']:>+9.2%}")

print("\nRead: how the edge decays as the entry slips later past the open. 'basis' is how")
print("far above the open you paid; 'cumulative' is the equal-weight sum of per-trade")
print("returns. This sizes the cost of deciding a few minutes after the open (near-open")
print("design) vs an exact opening-auction fill (pre-open decision).")
print("\nCAVEATS: selection here is top-N SCANNER (weaker than the LLM research), out of")
print("sample from the 46 trades, on IEX bars. Directional evidence for the DESIGN, not")
print("a live P&L promise. Confirm with research picks + SIP before shipping.")
