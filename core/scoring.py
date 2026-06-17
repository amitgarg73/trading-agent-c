from __future__ import annotations

"""
Post-session decision quality scoring.

Computes three per-trade signals after market close:
  r_multiple        — realized P&L / initial risk (stop_distance * shares)
                      Positive = win, negative = loss. 2.0 means we made 2x our risk.
  entry_timing_pct  — where in the day's H-L range we entered.
                      100 = bought the exact low. 0 = bought the exact high.
  directional_hit   — did the stock close above our entry price?
                      True even if we were stopped out early; measures prediction quality.
  day_pct_move      — stock's full-day % move (close vs open), for context.

And one daily summary signal:
  opportunity_cost  — top 5 actual movers that day vs what we traded.
                      Persistent gap = scanner or research agent problem.

Called by sessions/eod.py after reconcile + force-close.
Writes to c_positions and c_daily_performance.
Silently skips if DB columns don't yet exist (run supabase/migrations/add_trade_scoring.sql first).
"""

import json
from datetime import date, timedelta
from typing import Any

import yfinance as yf

from core.db import get_client


def _fetch_day_ohlc(tickers: list[str], trade_date: str) -> dict[str, dict]:
    """
    Fetch OHLC for each ticker on trade_date.
    Returns {ticker: {open, high, low, close, pct_move}} or {} on failure.
    """
    if not tickers:
        return {}
    try:
        end_date = (date.fromisoformat(trade_date) + timedelta(days=1)).isoformat()
        raw = yf.download(
            tickers,
            start=trade_date,
            end=end_date,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        result: dict[str, dict] = {}
        for ticker in tickers:
            try:
                if len(tickers) == 1:
                    hist = raw
                else:
                    hist = raw[ticker] if ticker in raw.columns.get_level_values(0) else None
                if hist is None or hist.empty:
                    continue
                row = hist.iloc[0]
                o = float(row["Open"])
                h = float(row["High"])
                l = float(row["Low"])
                c = float(row["Close"])
                result[ticker] = {
                    "open":     round(o, 4),
                    "high":     round(h, 4),
                    "low":      round(l, 4),
                    "close":    round(c, 4),
                    "pct_move": round((c - o) / o * 100, 2) if o else 0.0,
                }
            except Exception:
                continue
        return result
    except Exception as e:
        print(f"  [scoring] yfinance fetch failed: {e}")
        return {}


def _entry_timing_pct(entry: float, day_high: float, day_low: float) -> float | None:
    """
    100 = bought the exact low of the day (perfect timing).
    0   = bought the exact high (worst possible timing).
    Returns None if range is zero.
    """
    rng = day_high - day_low
    if rng < 0.001:
        return None
    return round(max(0.0, min(100.0, (day_high - entry) / rng * 100)), 1)


def _r_multiple(realized_pnl: float, entry: float, stop: float, shares: int) -> float | None:
    """
    R-multiple = realized_pnl / initial_risk.
    Initial risk = (entry - stop) * shares.
    Positive = trade made back more than it risked.
    """
    if not entry or not stop or not shares:
        return None
    risk = abs(entry - stop) * shares
    if risk < 0.01:
        return None
    return round(realized_pnl / risk, 2)


def _fetch_top_movers(trade_date: str, top_n: int = 10) -> list[dict]:
    """
    Return top movers for trade_date using yfinance on the S&P 500 universe.
    Falls back to empty list on failure.
    """
    from scanner.universe import get_tickers
    tickers = get_tickers()
    ohlc = _fetch_day_ohlc(tickers, trade_date)
    movers = sorted(
        [{"ticker": t, "pct_move": v["pct_move"]} for t, v in ohlc.items()],
        key=lambda x: x["pct_move"],
        reverse=True,
    )
    return movers[:top_n]


def score_trades(session_id: str, trade_date: str | None = None) -> dict[str, Any]:
    """
    Score all closed trades for session_id. Writes per-trade scores to c_positions
    and a daily summary to c_daily_performance.

    Returns a summary dict for logging.
    """
    today = trade_date or date.today().isoformat()
    db    = get_client()

    # Fetch all closed positions for this session
    rows = (
        db.table("c_positions")
        .select("id,ticker,entry_price,stop_loss,target_price,shares,realized_pnl,exit_reason,confidence")
        .eq("session_id", session_id)
        .eq("status", "closed")
        .execute()
        .data
    ) or []

    if not rows:
        print("  [scoring] No closed trades to score.")
        return {"trades_scored": 0}

    tickers = list({r["ticker"] for r in rows})
    ohlc    = _fetch_day_ohlc(tickers, today)

    directional_hits = []
    timing_scores    = []
    r_multiples      = []

    for row in rows:
        ticker   = row["ticker"]
        entry    = float(row.get("entry_price") or 0)
        stop     = float(row.get("stop_loss")   or 0)
        shares   = int(row.get("shares")        or 0)
        pnl      = float(row.get("realized_pnl") or 0)
        day      = ohlc.get(ticker)

        updates: dict[str, Any] = {}

        if day:
            timing = _entry_timing_pct(entry, day["high"], day["low"])
            d_hit  = day["close"] > entry if entry else None
            d_move = day["pct_move"]

            updates["day_pct_move"]     = d_move
            updates["directional_hit"]  = d_hit
            if timing is not None:
                updates["entry_timing_pct"] = timing
                timing_scores.append(timing)
            if d_hit is not None:
                directional_hits.append(d_hit)

        r_mul = _r_multiple(pnl, entry, stop, shares)
        if r_mul is not None:
            updates["r_multiple"] = r_mul
            r_multiples.append(r_mul)

        if updates:
            try:
                db.table("c_positions").update(updates).eq("id", row["id"]).execute()
            except Exception as e:
                # Columns may not exist yet — log and continue
                if "does not exist" in str(e):
                    print(f"  [scoring] Schema not migrated yet — run add_trade_scoring.sql. ({e})")
                    return {"trades_scored": 0, "error": "migration_needed"}
                print(f"  [scoring] {ticker} update failed: {e}")

    # Daily summary
    dir_acc    = round(sum(directional_hits) / len(directional_hits), 4) if directional_hits else None
    avg_timing = round(sum(timing_scores)   / len(timing_scores),    1)  if timing_scores    else None
    avg_r      = round(sum(r_multiples)     / len(r_multiples),      2)  if r_multiples      else None

    # Opportunity cost — top movers vs what we traded
    top_movers  = _fetch_top_movers(today)
    our_tickers = set(tickers)
    opp_cost    = {
        "top_movers": top_movers,
        "we_traded":  [
            {"ticker": t, "pct_move": ohlc[t]["pct_move"]}
            for t in tickers if t in ohlc
        ],
        "overlap": [m for m in top_movers if m["ticker"] in our_tickers],
    }

    daily_updates: dict[str, Any] = {"opportunity_cost": json.dumps(opp_cost)}
    if dir_acc    is not None: daily_updates["directional_accuracy"]  = dir_acc
    if avg_timing is not None: daily_updates["avg_entry_timing_pct"]  = avg_timing
    if avg_r      is not None: daily_updates["avg_r_multiple"]        = avg_r

    try:
        db.table("c_daily_performance").update(daily_updates).eq("session_id", session_id).execute()
    except Exception as e:
        if "does not exist" in str(e):
            print(f"  [scoring] Schema not migrated yet — run add_trade_scoring.sql.")
            return {"trades_scored": 0, "error": "migration_needed"}
        print(f"  [scoring] daily_performance update failed: {e}")

    # Print summary
    print(f"  [scoring] {len(rows)} trade(s) scored:")
    print(f"    Directional accuracy : {dir_acc*100:.0f}%" if dir_acc is not None else "    Directional accuracy : n/a")
    print(f"    Avg entry timing     : {avg_timing:.0f}/100" if avg_timing is not None else "    Avg entry timing     : n/a")
    print(f"    Avg R-multiple       : {avg_r:+.2f}R" if avg_r is not None else "    Avg R-multiple       : n/a")
    if top_movers:
        top3 = ", ".join(f"{m['ticker']} {m['pct_move']:+.1f}%" for m in top_movers[:3])
        our3 = ", ".join(f"{t} {ohlc[t]['pct_move']:+.1f}%" for t in tickers if t in ohlc)
        print(f"    Top movers           : {top3}")
        print(f"    We traded            : {our3 or 'n/a'}")

    return {
        "trades_scored":       len(rows),
        "directional_accuracy": dir_acc,
        "avg_entry_timing_pct": avg_timing,
        "avg_r_multiple":       avg_r,
        "top_movers":          top_movers[:5],
    }
