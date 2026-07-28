"""
READ-ONLY: Did the AI agents pick better than a brainless rule?

Compares, over all trading days with real filled agent trades:
  AGENTS      -- realized P&L from c_positions (what we actually did)
  Baseline A  -- SPY buy-and-hold (open->close) on the same notional
  Baseline B  -- average same-day return of the full morning candidate pool
  Baseline C  -- momentum top-N from the pool (rank by prior-day gain), open->close

All baselines are intraday open->close, matching the agents' premarket-entry /
EOD-exit style, applied to the SAME dollars the agents deployed that day.

NO WRITES. Reads c_positions + c_scan_results (Supabase) and Alpaca daily bars only.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from core.db import get_client
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# ---------- load DB rows (read only) ----------
c = get_client()

def all_rows(table, cols="*"):
    out=[]; step=1000; start=0
    while True:
        r=c.table(table).select(cols).range(start,start+step-1).execute()
        out.extend(r.data)
        if len(r.data)<step: break
        start+=step
    return out

positions = all_rows("c_positions")
scan = all_rows("c_scan_results","date,ticker,score,price,sector")

# Real filled agent trades: exclude test cleanup and unfilled (never got exposure).
def is_real_fill(p):
    er = (p.get("exit_reason") or "")
    if er == "test_cleanup": return False
    if er == "unfilled": return False
    if p.get("status") == "cancelled": return False
    return True

real = [p for p in positions if is_real_fill(p)]

# Agent trades grouped by trading day (open_date).
agent_by_day = defaultdict(list)
for p in real:
    agent_by_day[p["open_date"]].append(p)

agent_days = sorted(agent_by_day.keys())

# Pool grouped by date.
pool_by_day = defaultdict(list)
for s in scan:
    pool_by_day[s["date"]].append(s)

# ---------- pull Alpaca daily bars ----------
key = os.environ["ALPACA_API_KEY_ID_C"]
sec = os.environ["ALPACA_API_SECRET_KEY_C"]
adata = StockHistoricalDataClient(key, sec)

# Collect every ticker we need bars for, across the full window (+ a few prior days for momentum).
all_dates = sorted(set(agent_days) | set(pool_by_day.keys()))
start_date = datetime.strptime(min(all_dates), "%Y-%m-%d") - timedelta(days=8)
end_date   = datetime.strptime(max(all_dates), "%Y-%m-%d") + timedelta(days=2)

tickers = set(["SPY"])
for s in scan: tickers.add(s["ticker"])
for p in real: tickers.add(p["ticker"])
tickers = sorted(tickers)

# bars[symbol][date_str] = {open, close}
bars = defaultdict(dict)
B = 200
for i in range(0, len(tickers), B):
    chunk = tickers[i:i+B]
    req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                           start=start_date, end=end_date, feed=DataFeed.IEX)
    try:
        df = adata.get_stock_bars(req).df
    except Exception as e:
        print(f"WARN: bar fetch failed for chunk {i}: {e}")
        continue
    if df is None or len(df)==0: continue
    for (sym, ts), row in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        bars[sym][d] = {"open": float(row["open"]), "close": float(row["close"])}

def oc_return(sym, day):
    """open->close fractional return for sym on day, or None."""
    b = bars.get(sym, {}).get(day)
    if not b or not b["open"]: return None
    return b["close"]/b["open"] - 1.0

def prior_day_gain(sym, day):
    """close[t-1]/close[t-2]-1 using the two trading days before `day`."""
    days = sorted(bars.get(sym, {}).keys())
    prior = [d for d in days if d < day]
    if len(prior) < 2: return None
    d1, d2 = prior[-1], prior[-2]
    c1 = bars[sym][d1]["close"]; c2 = bars[sym][d2]["close"]
    if not c2: return None
    return c1/c2 - 1.0

# ---------- per-day computation ----------
rows = []
missing_notes = []
for day in agent_days:
    trades = agent_by_day[day]
    n = len(trades)
    notional = sum((t.get("position_size") or 0) for t in trades)
    agent_pnl = sum((t.get("realized_pnl") or 0) for t in trades)
    agent_wins = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)

    # Baseline A: SPY open->close on same notional
    spy_r = oc_return("SPY", day)
    spy_pnl = spy_r * notional if spy_r is not None else None

    # Baseline B: average pool open->close return on same notional
    pool = pool_by_day.get(day, [])
    pool_rets = [oc_return(s["ticker"], day) for s in pool]
    pool_rets = [r for r in pool_rets if r is not None]
    pool_avg_r = sum(pool_rets)/len(pool_rets) if pool_rets else None
    pool_pnl = pool_avg_r * notional if pool_avg_r is not None else None
    pool_cov = (len(pool_rets), len(pool))

    # Baseline C: momentum top-N (N = n filled agent trades), prior-day gain ranking
    ranked = []
    for s in pool:
        g = prior_day_gain(s["ticker"], day)
        r = oc_return(s["ticker"], day)
        if g is not None and r is not None:
            ranked.append((g, r, s["ticker"]))
    ranked.sort(key=lambda x: -x[0])
    topn = ranked[:n] if n>0 else []
    if topn:
        slice_notional = notional / len(topn)
        mom_pnl = sum(r*slice_notional for (_,r,_) in topn)
        mom_wins = sum(1 for (_,r,_) in topn if r>0)
    else:
        mom_pnl = None; mom_wins = None

    rows.append(dict(day=day, n=n, notional=notional,
                     agent_pnl=agent_pnl, agent_wins=agent_wins,
                     spy_r=spy_r, spy_pnl=spy_pnl,
                     pool_avg_r=pool_avg_r, pool_pnl=pool_pnl, pool_cov=pool_cov,
                     mom_pnl=mom_pnl, mom_wins=mom_wins, mom_n=len(topn)))

# ---------- print per-day detail ----------
print("="*118)
print("PER-DAY DETAIL  (notional = sum of agent filled position sizes that day; baselines applied to same notional)")
print("="*118)
hdr = f"{'date':<11}{'n':>3}{'notional':>11}{'AGENT$':>10}{'SPY$':>9}{'POOL$':>9}{'MOM$':>9}{'poolAvg%':>9}{'spy%':>8}{'poolcov':>9}"
print(hdr); print("-"*118)
for r in rows:
    def f(x,d=2): return f"{x:.{d}f}" if x is not None else "  n/a"
    print(f"{r['day']:<11}{r['n']:>3}{r['notional']:>11.0f}{r['agent_pnl']:>10.2f}"
          f"{f(r['spy_pnl']):>9}{f(r['pool_pnl']):>9}{f(r['mom_pnl']):>9}"
          f"{f((r['pool_avg_r'] or 0)*100) if r['pool_avg_r'] is not None else '  n/a':>9}"
          f"{f((r['spy_r'] or 0)*100) if r['spy_r'] is not None else '  n/a':>8}"
          f"{str(r['pool_cov'][0])+'/'+str(r['pool_cov'][1]):>9}")

# ---------- scoreboard totals (only over days where each baseline is computable) ----------
def agg(metric_pnl, metric_winfn=None, require=None):
    tot=0.0; wins=0; windays=0; ndays=0
    for r in rows:
        val = r[metric_pnl]
        if val is None: continue
        ndays+=1
        tot+=val
        if val>0: windays+=1
    return tot, windays, ndays

# Agent totals
A_tot = sum(r["agent_pnl"] for r in rows)
A_windays = sum(1 for r in rows if r["agent_pnl"]>0)
A_trade_total = sum(r["n"] for r in rows)
A_trade_wins = sum(r["agent_wins"] for r in rows)
A_ndays = len(rows)

def line(name, total, windays, ndays, trade_wr=None):
    wr = f"{trade_wr*100:5.1f}%" if trade_wr is not None else "   -- "
    print(f"{name:<26}{total:>12.2f}{wr:>10}{windays:>10}/{ndays:<3}")

print("\n"+"="*70)
print("SCOREBOARD  (totals over the days each row could be computed)")
print("="*70)
print(f"{'strategy':<26}{'total P&L $':>12}{'trade WR':>10}{'win-days':>13}")
print("-"*70)
line("AGENTS (actual)", A_tot, A_windays, A_ndays,
     trade_wr=(A_trade_wins/A_trade_total if A_trade_total else None))

spy_tot, spy_wd, spy_nd = agg("spy_pnl")
line("A: SPY hold", spy_tot, spy_wd, spy_nd)

pool_tot, pool_wd, pool_nd = agg("pool_pnl")
line("B: Pool average", pool_tot, pool_wd, pool_nd)

mom_tot = sum(r["mom_pnl"] for r in rows if r["mom_pnl"] is not None)
mom_wd  = sum(1 for r in rows if r["mom_pnl"] is not None and r["mom_pnl"]>0)
mom_nd  = sum(1 for r in rows if r["mom_pnl"] is not None)
mom_twins = sum(r["mom_wins"] for r in rows if r["mom_wins"] is not None)
mom_tn = sum(r["mom_n"] for r in rows if r["mom_n"])
line("C: Momentum top-N", mom_tot, mom_wd, mom_nd,
     trade_wr=(mom_twins/mom_tn if mom_tn else None))

# Apples-to-apples: restrict ALL to the intersection of days where every baseline computed.
common = [r for r in rows if r["spy_pnl"] is not None and r["pool_pnl"] is not None and r["mom_pnl"] is not None]
print("\n"+"="*70)
print(f"APPLES-TO-APPLES  (same {len(common)} days where ALL baselines computable)")
print("="*70)
print(f"{'strategy':<26}{'total P&L $':>12}{'win-days':>13}")
print("-"*70)
ca = sum(r["agent_pnl"] for r in common); caw=sum(1 for r in common if r["agent_pnl"]>0)
cs = sum(r["spy_pnl"] for r in common);  csw=sum(1 for r in common if r["spy_pnl"]>0)
cp = sum(r["pool_pnl"] for r in common); cpw=sum(1 for r in common if r["pool_pnl"]>0)
cm = sum(r["mom_pnl"] for r in common);  cmw=sum(1 for r in common if r["mom_pnl"]>0)
nd=len(common)
for nm,t,w in [("AGENTS (actual)",ca,caw),("A: SPY hold",cs,csw),
               ("B: Pool average",cp,cpw),("C: Momentum top-N",cm,cmw)]:
    print(f"{nm:<26}{t:>12.2f}{str(w)+'/'+str(nd):>13}")

print("\nNotes:")
print(f"  Real filled agent trades: {len(real)} across {len(agent_days)} days "
      f"(excluded {sum(1 for p in positions if (p.get('exit_reason')=='unfilled'))} unfilled, "
      f"{sum(1 for p in positions if p.get('exit_reason')=='test_cleanup')} test_cleanup).")
print(f"  Scan-pool dates available: {len(pool_by_day)}; days with agent fills: {len(agent_days)}.")
