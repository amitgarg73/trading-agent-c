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


def get_sector_rotation() -> list[dict[str, Any]]:
    """Fetch 1-day % change for all 11 sector ETFs, sorted best to worst."""
    try:
        rows = []
        for etf in _SECTOR_ETFS:
            hist = yf.Ticker(etf).history(period="2d")
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                curr = float(hist["Close"].iloc[-1])
                chg  = round((curr - prev) / prev * 100, 2) if prev else 0.0
            else:
                chg = 0.0
            rows.append({"etf": etf, "change_pct": chg})
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        return rows
    except Exception as e:
        return [{"error": str(e)}]
