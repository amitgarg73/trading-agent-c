from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import yfinance as yf


def get_candidates(min_score: int = 5) -> list[dict[str, Any]]:
    """
    Return tickers from c_scan_results for today with score >= min_score.
    Returns ticker, technical_score, current_price, avg_volume only.
    The Research Agent decides which to investigate further.
    """
    try:
        from core.db import get_client
        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_scan_results")
            .select("ticker,score,price,sector")
            .eq("date", today)
            .gte("score", min_score)
            .order("score", desc=True)
            .limit(100)
            .execute()
            .data
        )
        # Normalize to field names the Research Agent expects
        return [
            {
                "ticker":          r["ticker"],
                "technical_score": r["score"],
                "current_price":   r["price"],
                "sector":          r.get("sector"),
            }
            for r in (rows or [])
        ]
    except Exception as e:
        return [{"error": str(e)}]


def get_news(ticker: str) -> dict[str, Any]:
    """
    Check earnings blackout and fetch recent headlines for a ticker.
    Blackout = earnings today or tomorrow.
    """
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar

        blackout = False
        reason: str | None = None

        if cal is not None and not cal.empty:
            if "Earnings Date" in cal.index:
                earn_dates = cal.loc["Earnings Date"]
                if hasattr(earn_dates, "__iter__"):
                    earn_dates = list(earn_dates)
                else:
                    earn_dates = [earn_dates]

                today     = date.today()
                tomorrow  = today + timedelta(days=1)
                for ed in earn_dates:
                    try:
                        ed_date = ed.date() if hasattr(ed, "date") else date.fromisoformat(str(ed))
                        if ed_date in (today, tomorrow):
                            blackout = True
                            reason   = f"earnings {ed_date.isoformat()}"
                            break
                    except Exception:
                        continue

        news_items = t.news or []
        headlines  = [n.get("title", "") for n in news_items[:3] if n.get("title")]

        return {"blackout": blackout, "reason": reason, "headlines": headlines}
    except Exception as e:
        return {"error": str(e)}


def get_live_price(ticker: str) -> dict[str, Any]:
    """
    Fetch best available current price. Phase 1 uses yfinance 1-min close.
    """
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m")
        if hist.empty:
            return {"error": "no price data"}
        price = round(float(hist["Close"].iloc[-1]), 2)
        stale = 1
        return {"price": price, "source": "yfinance", "stale_minutes": stale}
    except Exception as e:
        return {"error": str(e)}


def get_intraday_signals(ticker: str) -> dict[str, Any]:
    """
    Compute VWAP, relative strength vs SPY, and today's % change from intraday bars.
    """
    try:
        bars = yf.Ticker(ticker).history(period="1d", interval="1m")
        spy  = yf.Ticker("SPY").history(period="1d", interval="1m")

        if bars.empty:
            return {"error": "no intraday data"}

        # VWAP = sum(price * volume) / sum(volume)
        tp      = (bars["High"] + bars["Low"] + bars["Close"]) / 3
        vwap    = float((tp * bars["Volume"]).sum() / bars["Volume"].sum()) if bars["Volume"].sum() > 0 else 0.0
        vwap    = round(vwap, 2)

        curr_price       = float(bars["Close"].iloc[-1])
        open_price       = float(bars["Open"].iloc[0])
        above_vwap       = curr_price > vwap
        today_pct_change = round((curr_price - open_price) / open_price * 100, 2) if open_price else 0.0

        rs_vs_spy: float | None = None
        if not spy.empty:
            spy_open = float(spy["Open"].iloc[0])
            spy_curr = float(spy["Close"].iloc[-1])
            if spy_open and spy_curr != spy_open:
                spy_chg   = (spy_curr - spy_open) / spy_open
                stock_chg = (curr_price - open_price) / open_price if open_price else 0.0
                rs_vs_spy = round(stock_chg / spy_chg, 2) if spy_chg != 0 else None

        return {
            "above_vwap":        above_vwap,
            "vwap":              vwap,
            "rs_vs_spy":         rs_vs_spy,
            "today_pct_change":  today_pct_change,
        }
    except Exception as e:
        return {"error": str(e)}


def get_atr(ticker: str) -> dict[str, Any]:
    """
    Compute 14-day ATR as % of price, and opening range breakout % (first 30 min).
    """
    try:
        daily = yf.Ticker(ticker).history(period="30d")
        if len(daily) < 2:
            return {"error": "insufficient history"}

        # True range per bar
        highs  = daily["High"]
        lows   = daily["Low"]
        closes = daily["Close"]
        prev_c = closes.shift(1)

        tr = (
            (highs - lows).abs()
            .combine((highs - prev_c).abs(), max)
            .combine((lows  - prev_c).abs(), max)
        )
        atr      = float(tr.iloc[-14:].mean())
        price    = float(closes.iloc[-1])
        atr_pct  = round(atr / price * 100, 2) if price else 0.0

        # ORB: first 30 min of today's session
        orb_pct: float | None = None
        try:
            intra = yf.Ticker(ticker).history(period="1d", interval="1m")
            if len(intra) >= 30:
                orb_high = float(intra["High"].iloc[:30].max())
                orb_low  = float(intra["Low"].iloc[:30].min())
                orb_open = float(intra["Open"].iloc[0])
                if orb_open:
                    orb_pct = round((orb_high - orb_low) / orb_open * 100, 2)
        except Exception:
            pass

        return {"atr_pct": atr_pct, "orb_pct": orb_pct}
    except Exception as e:
        return {"error": str(e)}


def get_position_history(ticker: str, days: int = 30) -> dict[str, Any]:
    """Fetch recent trade history for a ticker from c_positions."""
    try:
        from core.db import get_client
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select("ticker,realized_pnl,exit_reason,status")
            .eq("ticker", ticker)
            .eq("status", "closed")
            .gte("close_date", cutoff)
            .execute()
            .data
        ) or []

        trades   = len(rows)
        wins     = sum(1 for r in rows if (r.get("realized_pnl") or 0) > 0)
        win_rate = round(wins / trades * 100, 1) if trades else 0.0
        avg_pnl  = round(sum(r.get("realized_pnl") or 0 for r in rows) / trades, 2) if trades else 0.0
        last_row = rows[-1] if rows else None
        last_exit = last_row.get("exit_reason") if last_row else None

        return {
            "trades":       trades,
            "wins":         wins,
            "win_rate_pct": win_rate,
            "avg_pnl":      avg_pnl,
            "last_exit":    last_exit,
        }
    except Exception as e:
        return {"error": str(e)}
