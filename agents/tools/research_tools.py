from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _is_premarket() -> bool:
    """True before 9:25 AM ET — intraday bar data is not yet available."""
    import pytz
    from datetime import datetime
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    return (now_et.hour, now_et.minute) < (9, 25)


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
        import yfinance as yf
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
    """Fetch latest ask/bid price from Alpaca quote stream."""
    try:
        from core.alpaca import get_live_price as _alpaca_price
        price = _alpaca_price(ticker)
        if price is None:
            return {"error": "no price data"}
        return {"price": price, "source": "alpaca", "stale_minutes": 0}
    except Exception as e:
        return {"error": str(e)}


def get_intraday_signals(ticker: str) -> dict[str, Any]:
    """
    Compute VWAP, relative strength vs SPY, and today's % change from Alpaca 1-min bars.
    Returns available: false before market open (9:25 AM ET) — no bars exist yet.
    """
    if _is_premarket():
        return {
            "available":        False,
            "reason":           "pre-market",
            "above_vwap":       None,
            "vwap":             None,
            "rs_vs_spy":        None,
            "today_pct_change": None,
        }
    try:
        from core.alpaca import _dclient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timezone
        import pytz

        et = pytz.timezone("America/New_York")
        market_open = datetime.now(et).replace(hour=9, minute=30, second=0, microsecond=0)

        req = StockBarsRequest(
            symbol_or_symbols=[ticker, "SPY"],
            timeframe=TimeFrame.Minute,
            start=market_open.astimezone(timezone.utc),
        )
        bars_by_symbol = _dclient().get_stock_bars(req).data

        stock_bars = bars_by_symbol.get(ticker, [])
        spy_bars   = bars_by_symbol.get("SPY", [])

        if not stock_bars:
            return {"error": "no intraday data"}

        # Cumulative VWAP = sum((H+L+C)/3 * vol) / sum(vol)
        total_pv  = sum((b.high + b.low + b.close) / 3 * b.volume for b in stock_bars)
        total_vol = sum(b.volume for b in stock_bars)
        vwap = round(total_pv / total_vol, 2) if total_vol > 0 else 0.0

        curr_price       = float(stock_bars[-1].close)
        open_price       = float(stock_bars[0].open)
        above_vwap       = curr_price > vwap
        today_pct_change = round((curr_price - open_price) / open_price * 100, 2) if open_price else 0.0

        rs_vs_spy: float | None = None
        if spy_bars:
            spy_open = float(spy_bars[0].open)
            spy_curr = float(spy_bars[-1].close)
            if spy_open and spy_curr != spy_open:
                spy_chg   = (spy_curr - spy_open) / spy_open
                stock_chg = (curr_price - open_price) / open_price if open_price else 0.0
                rs_vs_spy = round(stock_chg / spy_chg, 2) if spy_chg != 0 else None

        return {
            "above_vwap":       above_vwap,
            "vwap":             vwap,
            "rs_vs_spy":        rs_vs_spy,
            "today_pct_change": today_pct_change,
        }
    except Exception as e:
        return {"error": str(e)}


def get_atr(ticker: str) -> dict[str, Any]:
    """
    Compute 14-day ATR as % of price, and opening range breakout % (first 30 min).
    Uses Alpaca daily + 1-min bars.
    """
    try:
        from core.alpaca import _dclient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta, timezone
        import pytz

        start = (datetime.now(timezone.utc) - timedelta(days=45)).date()
        req = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Day,
            start=start,
        )
        bars = _dclient().get_stock_bars(req).data.get(ticker, [])

        if len(bars) < 2:
            return {"error": "insufficient history"}

        tr_values = []
        for i in range(1, len(bars)):
            h      = float(bars[i].high)
            lo     = float(bars[i].low)
            prev_c = float(bars[i - 1].close)
            tr_values.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))

        atr     = sum(tr_values[-14:]) / min(14, len(tr_values))
        price   = float(bars[-1].close)
        atr_pct = round(atr / price * 100, 2) if price else 0.0

        # ORB: first 30 min of today's session
        orb_pct: float | None = None
        try:
            et = pytz.timezone("America/New_York")
            market_open = datetime.now(et).replace(hour=9, minute=30, second=0, microsecond=0)
            intra_req = StockBarsRequest(
                symbol_or_symbols=[ticker],
                timeframe=TimeFrame.Minute,
                start=market_open.astimezone(timezone.utc),
                limit=30,
            )
            intra_bars = _dclient().get_stock_bars(intra_req).data.get(ticker, [])
            if len(intra_bars) >= 30:
                orb_high = max(float(b.high) for b in intra_bars[:30])
                orb_low  = min(float(b.low)  for b in intra_bars[:30])
                orb_open = float(intra_bars[0].open)
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
