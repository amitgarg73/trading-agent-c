"""
READ-ONLY: does research's HIGH-confidence rubric sort names BACKWARDS?

Background: on the reconciled trades, research's own confidence is inverted -- its
HIGH-confidence names hit direction 27% vs LOW's 56%, and it sizes UP on HIGH. The
rubric (agents/research_agent.py) gates HIGH on `today_pct_change <= 2` (a momentum
FADE) and rewards very high `rs_vs_spy`. This tests whether those criteria predict
FORWARD return backwards.

The stored signals only exist for ~14 of 45 filled trades (traces began ~6/4), so we
RECOMPUTE the signals from Alpaca bars, which covers all filled trades. For each
trade, at the recorded entry_time (~decision time) we compute:
  today_pct_change = ref_price / prior_close - 1        (the rubric's gate variable)
  rs_proxy         = name_move_since_open - SPY_move_since_open   (same-day RS proxy)
then the FORWARD return from that moment to the close. We bucket by the rubric's own
today_pct_change boundaries and report forward return + hit rate per bucket.

Hypothesis to confirm or kill: the <=2% bucket (which the rubric reserves for HIGH
confidence) has LOWER forward return than the 2-4% bucket -> the gate is inverted.

Honesty guards:
- No lookahead: signals at entry_time; forward return uses only bars strictly after.
- rs_proxy is same-day (not the tool's exact multi-window rs_vs_spy) -> RS is
  DIRECTIONAL only, do not over-read it.
- IEX minute bars by default (paper feed, thin); set BACKTEST_FEED=sip before trusting.
- Small n (~45 filled trades). Directional, NOT statistical proof.
NO WRITES. Reads c_positions (Supabase) + Alpaca bars only.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import pytz
from dotenv import load_dotenv
load_dotenv()

from core.db import get_client
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

_FEED = DataFeed.SIP if os.environ.get("BACKTEST_FEED", "iex").lower() == "sip" else DataFeed.IEX
ET = pytz.timezone("America/New_York")

c = get_client()

def all_rows(table, cols="*"):
    out, step, start = [], 1000, 0
    while True:
        r = c.table(table).select(cols).range(start, start + step - 1).execute()
        out.extend(r.data)
        if len(r.data) < step:
            break
        start += step
    return out

def is_real_fill(p):
    if (p.get("exit_reason") or "") in ("test_cleanup", "unfilled", "stale_midnight_catchup"):
        return False
    if p.get("status") == "cancelled":
        return False
    return p.get("realized_pnl") is not None

def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(ET)
    except ValueError:
        return None

picks = [{"ticker": p["ticker"], "day": p["open_date"], "entry_time": p.get("entry_time"),
          "confidence": p.get("confidence")}
         for p in all_rows("c_positions")
         if is_real_fill(p) and p.get("ticker") and p.get("open_date") and p.get("entry_time")]
print(f"Loaded {len(picks)} filled trades with an entry_time across "
      f"{len(set(p['day'] for p in picks))} days.  feed={_FEED}\n")

adata = StockHistoricalDataClient(os.environ["ALPACA_API_KEY_ID_C"], os.environ["ALPACA_API_SECRET_KEY_C"])

def fetch_minute(tickers, day_str):
    day = datetime.strptime(day_str, "%Y-%m-%d")
    start = ET.localize(day.replace(hour=9, minute=30)).astimezone(timezone.utc)
    end   = ET.localize(day.replace(hour=16, minute=0)).astimezone(timezone.utc)
    out = defaultdict(list)
    try:
        req = StockBarsRequest(symbol_or_symbols=sorted(set(tickers)), timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=_FEED)
        df = adata.get_stock_bars(req).df
    except Exception as e:
        print(f"  WARN: minute fetch failed {day_str}: {e}")
        return out
    if df is None or len(df) == 0:
        return out
    for (sym, ts), row in df.iterrows():
        out[sym].append({"ts": ts.tz_convert(ET), "close": float(row["close"]), "open": float(row["open"])})
    for sym in out:
        out[sym].sort(key=lambda b: b["ts"])
    return out

def fetch_prior_closes(tickers, day_min, day_max):
    """{(ticker, day_str): prior trading-day close} using daily bars."""
    start = (datetime.strptime(day_min, "%Y-%m-%d") - timedelta(days=8))
    end   = (datetime.strptime(day_max, "%Y-%m-%d") + timedelta(days=1))
    hist = defaultdict(list)  # ticker -> [(date, close)]
    try:
        req = StockBarsRequest(symbol_or_symbols=sorted(set(tickers)), timeframe=TimeFrame.Day,
                               start=start, end=end, feed=_FEED)
        df = adata.get_stock_bars(req).df
    except Exception as e:
        print(f"  WARN: daily fetch failed: {e}")
        return {}
    if df is None or len(df) == 0:
        return {}
    for (sym, ts), row in df.iterrows():
        hist[sym].append((ts.tz_convert(ET).date(), float(row["close"])))
    for sym in hist:
        hist[sym].sort()
    out = {}
    for sym, series in hist.items():
        for day_str in {p["day"] for p in picks if p["ticker"] == sym}:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
            prior = [close for (dt, close) in series if dt < d]
            if prior:
                out[(sym, day_str)] = prior[-1]
    return out

days = sorted({p["day"] for p in picks})
all_syms = {p["ticker"] for p in picks} | {"SPY"}
minute_by_day = {d: fetch_minute([p["ticker"] for p in picks if p["day"] == d] + ["SPY"], d) for d in days}
prior_close = fetch_prior_closes(all_syms, days[0], days[-1])

def price_at(bars, ref_dt):
    idx = next((i for i, b in enumerate(bars) if b["ts"] >= ref_dt), None)
    return (idx, bars[idx]["close"]) if idx is not None else (None, None)

rows = []
for p in picks:
    day_bars = minute_by_day.get(p["day"], {})
    bars = day_bars.get(p["ticker"]); spy = day_bars.get("SPY")
    ref = parse_ts(p["entry_time"])
    pc = prior_close.get((p["ticker"], p["day"]))
    if not bars or not spy or not ref or not pc:
        continue
    ridx, ref_px = price_at(bars, ref)
    _, spy_ref = price_at(spy, ref)
    if ridx is None or ref_px is None or spy_ref is None or ridx >= len(bars) - 1:
        continue
    open_px, spy_open = bars[0]["open"], spy[0]["open"]
    today_pct = (ref_px / pc - 1.0) * 100.0
    rs_proxy  = ((ref_px / open_px - 1.0) - (spy_ref / spy_open - 1.0)) * 100.0
    fwd_ret   = (bars[-1]["close"] / ref_px - 1.0) * 100.0   # ref -> close, no lookahead
    rows.append({"t": p["ticker"], "conf": p["confidence"], "today_pct": today_pct,
                 "rs_proxy": rs_proxy, "fwd": fwd_ret})

print(f"Recomputed signals for {len(rows)} of {len(picks)} trades "
      f"({len(picks) - len(rows)} missing bars/prior-close).\n")

def bucket_today(x):
    if x <= 2:  return "<=2%  (HIGH gate)"
    if x <= 4:  return "2-4%  (pushed to LOW/MED)"
    return ">4%   (skip zone)"

def report(name, keyfn, order):
    agg = defaultdict(list)
    for r in rows:
        agg[keyfn(r)].append(r["fwd"])
    print(f"== {name} ==")
    print(f"  {'bucket':<26}{'n':>4}{'avg fwd ret':>13}{'hit %':>8}")
    for k in order:
        v = agg.get(k, [])
        if not v:
            continue
        hit = 100.0 * sum(1 for x in v if x > 0) / len(v)
        print(f"  {k:<26}{len(v):>4}{sum(v)/len(v):>12.2f}%{hit:>7.0f}%")
    print()

report("Forward return by today_pct_change bucket (the gate test)",
       lambda r: bucket_today(r["today_pct"]),
       ["<=2%  (HIGH gate)", "2-4%  (pushed to LOW/MED)", ">4%   (skip zone)"])

report("Forward return by same-day RS proxy (directional only)",
       lambda r: "RS>=1.0" if r["rs_proxy"] >= 1.0 else ("RS 0-1" if r["rs_proxy"] >= 0 else "RS<0"),
       ["RS>=1.0", "RS 0-1", "RS<0"])

report("Cross-check: forward return by stored research confidence",
       lambda r: r["conf"] or "(none)", ["HIGH", "MEDIUM", "LOW", "(none)"])

print("Read the gate test: if <=2% has LOWER avg fwd return than 2-4%, the HIGH gate is inverted.")
