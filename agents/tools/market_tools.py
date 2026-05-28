from __future__ import annotations

from typing import Any

import yfinance as yf


_VIX_LEVELS = [
    (15,  "LOW"),
    (20,  "ELEVATED"),
    (25,  "HIGH"),
    (30,  "CRISIS"),
]

_SECTOR_ETFS = ["XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE", "XLC"]

_FUTURES = {
    "S&P500": "ES=F",
    "Nasdaq":  "NQ=F",
    "Dow":     "YM=F",
}


def _vix_level(value: float) -> str:
    for threshold, label in _VIX_LEVELS:
        if value < threshold:
            return label
    return "EXTREME"


def get_vix() -> dict[str, Any]:
    """Fetch current VIX index value."""
    try:
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="1d")
        if hist.empty:
            return {"error": "no VIX data"}
        value = round(float(hist["Close"].iloc[-1]), 2)
        return {"value": value, "level": _vix_level(value)}
    except Exception as e:
        return {"error": str(e)}


def get_futures() -> dict[str, Any]:
    """Fetch current futures % change for S&P500, Nasdaq, Dow."""
    try:
        results: dict[str, Any] = {}
        changes = []
        for name, symbol in _FUTURES.items():
            hist = yf.Ticker(symbol).history(period="2d")
            if len(hist) >= 2:
                prev  = float(hist["Close"].iloc[-2])
                curr  = float(hist["Close"].iloc[-1])
                chg   = round((curr - prev) / prev * 100, 2) if prev else 0.0
            else:
                chg = 0.0
            results[name] = {"change_pct": chg}
            changes.append(chg)

        avg = round(sum(changes) / len(changes), 2) if changes else 0.0
        if avg > 0.2:
            bias = "BULLISH"
        elif avg < -0.2:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        results["avg_change_pct"] = avg
        results["bias"] = bias
        return results
    except Exception as e:
        return {"error": str(e)}


def get_fear_greed() -> dict[str, Any]:
    """Fetch CNN Fear & Greed index from alternative.me API."""
    import urllib.request, json
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        entry = data["data"][0]
        value = int(entry["value"])
        return {"value": value, "classification": entry["value_classification"]}
    except Exception as e:
        return {"error": str(e)}


def get_economic_calendar() -> dict[str, Any]:
    """
    Fetch today's US economic events from ForexFactory.
    High-impact events (Fed, CPI, NFP, GDP) fundamentally change session risk.
    """
    import urllib.request, json
    from datetime import date
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            events = json.loads(resp.read())

        today_str = date.today().isoformat()
        usd_today = []
        for ev in events:
            if ev.get("country") != "USD":
                continue
            # date format: "2026-05-27T08:30:00-0400" — first 10 chars are the local date
            if (ev.get("date") or "")[:10] == today_str:
                usd_today.append({
                    "title":    ev.get("title"),
                    "time":     (ev.get("date") or "")[ 11:16],  # HH:MM
                    "impact":   ev.get("impact"),
                    "forecast": ev.get("forecast"),
                    "previous": ev.get("previous"),
                })

        high = [e for e in usd_today if e["impact"] == "High"]
        return {
            "has_high_impact_events": len(high) > 0,
            "high_impact_count":      len(high),
            "high_impact_events":     [f"{e['title']} at {e['time']} ET" for e in high],
            "all_events_today":       usd_today,
        }
    except Exception as e:
        return {"error": str(e)}


def get_treasury_yields() -> dict[str, Any]:
    """
    Fetch 10-year Treasury yield (^TNX) direction and 1-day change in basis points.
    Rising yields on high-VIX days compound headwinds for momentum stocks.
    """
    try:
        hist = yf.Ticker("^TNX").history(period="2d")
        if len(hist) < 2:
            return {"error": "insufficient yield data"}
        prev_yield = round(float(hist["Close"].iloc[-2]), 3)
        curr_yield = round(float(hist["Close"].iloc[-1]), 3)
        change_bp  = round((curr_yield - prev_yield) * 100, 1)
        direction  = "rising" if change_bp > 5 else ("falling" if change_bp < -5 else "flat")
        return {"yield_10y": curr_yield, "change_bp": change_bp, "direction": direction}
    except Exception as e:
        return {"error": str(e)}


def get_sector_rotation() -> list[dict[str, Any]]:
    """Fetch 1-day % change for all 11 sector ETFs, sorted best to worst."""
    try:
        from core.alpaca import _dclient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta, timezone

        start = (datetime.now(timezone.utc) - timedelta(days=7)).date()
        req = StockBarsRequest(
            symbol_or_symbols=_SECTOR_ETFS,
            timeframe=TimeFrame.Day,
            start=start,
        )
        bars_by_symbol = _dclient().get_stock_bars(req).data

        rows = []
        for etf in _SECTOR_ETFS:
            bars = bars_by_symbol.get(etf, [])
            if len(bars) >= 2:
                prev = float(bars[-2].close)
                curr = float(bars[-1].close)
                chg  = round((curr - prev) / prev * 100, 2) if prev else 0.0
            else:
                chg = 0.0
            rows.append({"etf": etf, "change_pct": chg})
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        return rows
    except Exception as e:
        return [{"error": str(e)}]
