"""
READ-ONLY: does entering at the OPEN beat chasing the ask?

Compares two entry mechanisms on the SAME real picks (c_positions filled trades),
holding selection and the exit rule constant so the only variable is the entry:

  CHASE (what we do)   -- enter at the recorded fill price (mid-morning ask).
  OPEN-ANCHOR (proposed) -- rest a limit at open*(1+buffer) for the first N minutes;
                          fills only if a minute bar trades through it, else skip.

Both then run the identical exit on minute bars: hard stop (from the position),
trailing stop (trail_pct), take-profit (from the position), else EOD close.

Reports, per anchor buffer: fill rate, average entry basis vs the open, and P&L
(chase total vs open-anchor total, plus what the SKIPPED names would have returned).

Honesty guards:
- selection held constant (same names/days for both), so only the entry differs
- no lookahead: exits use only bars strictly after entry
- conservative fills: an open-anchor limit fills only if a bar's LOW trades through
  it, and fills AT the limit (not the bar low); adverse exits (stop/trail) are
  checked before the target within a bar
- IEX minute bars (paper feed, ~2-3% of volume) can miss the true intraday low, so
  open-anchor fill counts are a LOWER bound. Re-run on SIP before trusting live.

NO WRITES. Reads c_positions (Supabase) + Alpaca minute bars only.
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

# ---- config (structural choices, not regime knobs) ----
ANCHOR_BUFFERS   = [0.000, 0.001, 0.002, 0.003]   # limit = open * (1 + buffer)
ENTRY_WINDOW_MIN = 30       # minutes after the open the limit is allowed to rest
TRAIL_PCT        = 0.015    # matches core/params.py PARAM_DEFAULTS["trail_pct"]
ET = pytz.timezone("America/New_York")

# ---- read picks (read only) ----
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
    er = (p.get("exit_reason") or "")
    if er in ("test_cleanup", "unfilled"):
        return False
    if p.get("status") == "cancelled":
        return False
    return True

positions = [p for p in all_rows("c_positions") if is_real_fill(p)]

def num(p, k):
    v = p.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None

# keep only rows with everything the simulation needs
picks = []
for p in positions:
    entry = num(p, "entry_price")
    tgt   = num(p, "target_price")
    stop  = num(p, "stop_loss")
    size  = num(p, "position_size") or (num(p, "shares") or 0) * (entry or 0)
    if p.get("ticker") and p.get("open_date") and entry and tgt and stop and size:
        picks.append({"ticker": p["ticker"], "day": p["open_date"], "entry": entry,
                      "target": tgt, "stop": stop, "size": size, "entry_time": p.get("entry_time")})

print(f"Loaded {len(picks)} filled picks across {len(set(p['day'] for p in picks))} days.\n")

# ---- Alpaca minute bars ----
adata = StockHistoricalDataClient(os.environ["ALPACA_API_KEY_ID_C"], os.environ["ALPACA_API_SECRET_KEY_C"])

def fetch_day_bars(tickers, day_str):
    """Return {ticker: [ {ts(ET), open, high, low, close} ... ]} for the regular session."""
    day = datetime.strptime(day_str, "%Y-%m-%d")
    start_utc = ET.localize(day.replace(hour=9, minute=30)).astimezone(timezone.utc)
    end_utc   = ET.localize(day.replace(hour=16, minute=0)).astimezone(timezone.utc)
    out = defaultdict(list)
    try:
        req = StockBarsRequest(symbol_or_symbols=sorted(set(tickers)), timeframe=TimeFrame.Minute,
                               start=start_utc, end=end_utc, feed=_FEED)
        df = adata.get_stock_bars(req).df
    except Exception as e:
        print(f"  WARN: bar fetch failed for {day_str}: {e}")
        return out
    if df is None or len(df) == 0:
        return out
    for (sym, ts), row in df.iterrows():
        ts_et = ts.tz_convert(ET)
        out[sym].append({"ts": ts_et, "open": float(row["open"]), "high": float(row["high"]),
                         "low": float(row["low"]), "close": float(row["close"])})
    for sym in out:
        out[sym].sort(key=lambda b: b["ts"])
    return out

# fetch once per day
tickers_by_day = defaultdict(set)
for p in picks:
    tickers_by_day[p["day"]].add(p["ticker"])
bars_by_day = {d: fetch_day_bars(list(ts), d) for d, ts in tickers_by_day.items()}

# ---- exit simulation (identical for both mechanisms) ----
def simulate_exit(bars_after, entry_px, target_px, stop_px):
    """Walk minute bars strictly after entry. Conservative: adverse stops before target."""
    high = entry_px
    for b in bars_after:
        high = max(high, b["high"])
        binding_stop = max(stop_px, high * (1 - TRAIL_PCT))
        if b["low"] <= binding_stop:
            return binding_stop
        if b["high"] >= target_px:
            return target_px
    return bars_after[-1]["close"] if bars_after else entry_px

def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(ET)
    except ValueError:
        return None

# ---- run: chase (once) + open-anchor (per buffer) ----
def run(buffer):
    chase_rows, oa_filled, oa_missed = [], [], []
    for p in picks:
        bars = bars_by_day.get(p["day"], {}).get(p["ticker"])
        if not bars:
            continue
        open_px = bars[0]["open"]
        if not open_px:
            continue
        oc_ret = bars[-1]["close"] / open_px - 1.0     # what the name did, open->close
        tgt_pct  = p["target"] / p["entry"] - 1.0      # same %-distances the position used
        stop_pct = 1.0 - p["stop"] / p["entry"]

        # CHASE: enter at the recorded fill, exit-sim from the fill minute forward
        et_entry = parse_ts(p["entry_time"])
        idx = next((i for i, b in enumerate(bars) if b["ts"] >= et_entry), None) if et_entry else None
        if idx is None:
            idx = min(range(len(bars)), key=lambda i: abs((bars[i]["open"] - p["entry"]) / p["entry"]))
        c_exit = simulate_exit(bars[idx + 1:], p["entry"], p["target"], p["stop"])
        chase_rows.append({"t": p["ticker"], "day": p["day"], "basis": p["entry"] / open_px - 1.0,
                           "pnl": c_exit / p["entry"] - 1.0, "size": p["size"], "oc": oc_ret})

        # OPEN-ANCHOR: rest a limit at open*(1+buffer) for the first ENTRY_WINDOW_MIN
        limit = open_px * (1 + buffer)
        window = [b for b in bars if (b["ts"] - bars[0]["ts"]).total_seconds() <= ENTRY_WINDOW_MIN * 60]
        fill_i = next((i for i, b in enumerate(window) if b["low"] <= limit), None)
        if fill_i is None:
            oa_missed.append({"t": p["ticker"], "size": p["size"], "oc": oc_ret})
            continue
        oa_target = limit * (1 + tgt_pct)
        oa_stop   = limit * (1 - stop_pct)
        o_exit = simulate_exit(bars[fill_i + 1:], limit, oa_target, oa_stop)
        oa_filled.append({"t": p["ticker"], "basis": limit / open_px - 1.0,
                          "pnl": o_exit / limit - 1.0, "size": p["size"], "oc": oc_ret})
    return chase_rows, oa_filled, oa_missed

def agg(rows):
    if not rows:
        return 0, 0.0, 0.0, 0.0
    n = len(rows)
    basis = sum(r["basis"] for r in rows) / n
    pnl_pct = sum(r["pnl"] for r in rows) / n
    pnl_usd = sum(r["pnl"] * r["size"] for r in rows)
    return n, basis, pnl_pct, pnl_usd

# chase is identical across buffers; compute once from buffer 0's run
chase_rows, _, _ = run(0.0)
cn, cbasis, cpnl, cusd = agg(chase_rows)
covered = len(chase_rows)

print("=" * 78)
print(f"ENTRY-MECHANISM BACKTEST  |  {covered} picks with minute bars  |  trail {TRAIL_PCT:.1%}  |  {os.environ.get('BACKTEST_FEED','iex').upper()} feed")
print("=" * 78)
print(f"\nCHASE (recorded fills):")
print(f"  fills:      {cn}/{covered} (100%)")
print(f"  entry basis vs open:  {cbasis:+.2%}   (positive = bought above the open)")
print(f"  avg P&L/trade:        {cpnl:+.2%}")
print(f"  total P&L:            ${cusd:,.0f}")

print(f"\nOPEN-ANCHOR (limit at open + buffer, {ENTRY_WINDOW_MIN}-min window):")
print(f"  {'buffer':>7} | {'fills':>9} | {'basis':>8} | {'P&L/tr':>8} | {'total P&L':>11} | {'skipped: their open->close':>28}")
for buf in ANCHOR_BUFFERS:
    _, oaf, oam = run(buf)
    n, basis, pnl, usd = agg(oaf)
    miss_n = len(oam)
    miss_oc = sum(m["oc"] for m in oam) / miss_n if miss_n else 0.0
    fill_rate = n / covered if covered else 0
    print(f"  {buf:>6.1%} | {n:>3}/{covered:<3} {fill_rate:>3.0%} | {basis:>+7.2%} | {pnl:>+7.2%} | ${usd:>9,.0f} | "
          f"{miss_n:>3} skipped, avg {miss_oc:+.2%}")

print("\nRead: CHASE buys above the open and takes every name; OPEN-ANCHOR buys at the")
print("open (better basis) but skips names that gapped away. The question is whether the")
print("better basis on the fills beats the P&L given up on the skipped names.")
print("\nCAVEAT: IEX minute bars miss part of the tape, so open-anchor fills are a LOWER")
print("bound (real fill rate is higher). Treat P&L as directional; confirm on SIP before")
print("shipping. Selection and exits are held constant — only the entry differs.")
