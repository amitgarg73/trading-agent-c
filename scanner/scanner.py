from __future__ import annotations

import math
from datetime import date
from typing import Any

import yfinance as yf
import pandas as pd

from scanner.universe import get_tickers, get_sector

_MIN_PRICE     = 10.0
_MAX_PRICE     = 500.0
_MAX_ATR_PCT   = 5.0
_MIN_AVG_VOL   = 500_000   # filter illiquid tickers
_HISTORY_DAYS  = "60d"     # enough for SMA50 + ATR14 + MACD


def _compute_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    if gain.empty:
        return 50.0
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_macd_hist(closes: pd.Series) -> float:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist  = macd - signal
    return round(float(hist.iloc[-1]), 4) if not hist.empty else 0.0


def _compute_atr_pct(hist: pd.DataFrame, period: int = 14) -> float:
    if len(hist) < period + 1:
        return 0.0
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    prev_c  = c.shift(1)
    tr = (h - l).abs().combine((h - prev_c).abs(), max).combine((l - prev_c).abs(), max)
    atr = float(tr.iloc[-period:].mean())
    price = float(c.iloc[-1])
    return round(atr / price * 100, 2) if price else 0.0


def _score_ticker(hist: pd.DataFrame) -> dict[str, Any]:
    """
    Score a ticker from its daily OHLCV history.
    Returns scoring fields or raises on insufficient data.
    """
    if len(hist) < 55:
        raise ValueError("insufficient history")

    closes  = hist["Close"]
    volumes = hist["Volume"]

    price = round(float(closes.iloc[-1]), 2)

    if not (_MIN_PRICE <= price <= _MAX_PRICE):
        raise ValueError(f"price {price} out of range")

    avg_vol = int(volumes.iloc[-20:].mean())
    if avg_vol < _MIN_AVG_VOL:
        raise ValueError(f"avg volume {avg_vol} too low")

    atr_pct = _compute_atr_pct(hist)
    if atr_pct > _MAX_ATR_PCT:
        raise ValueError(f"ATR% {atr_pct} too high")

    rsi  = _compute_rsi(closes)
    macd = _compute_macd_hist(closes)
    prev_macd = _compute_macd_hist(closes.iloc[:-1])

    sma20 = float(closes.iloc[-20:].mean())
    sma50 = float(closes.iloc[-50:].mean())

    today_vol    = float(volumes.iloc[-1])
    avg_vol_20d  = float(volumes.iloc[-21:-1].mean()) if len(volumes) > 21 else avg_vol
    volume_ratio = round(today_vol / avg_vol_20d, 2) if avg_vol_20d > 0 else 1.0

    above_sma20 = price > sma20
    above_sma50 = price > sma50

    # ── Score ─────────────────────────────────────────────────────────────────
    score = 0

    # RSI: momentum zone
    if 50 <= rsi <= 70:
        score += 2
    elif 45 <= rsi < 50 or 70 < rsi <= 75:
        score += 1

    # MACD histogram: positive and increasing
    if macd > 0 and macd > prev_macd:
        score += 2
    elif macd > 0:
        score += 1

    # Volume surge
    if volume_ratio >= 1.5:
        score += 2
    elif volume_ratio >= 1.2:
        score += 1

    # Trend
    if above_sma20 and above_sma50:
        score += 2
    elif above_sma20:
        score += 1

    return {
        "technical_score": score,
        "current_price":   price,
        "avg_volume":      avg_vol,
        "rsi":             rsi,
        "volume_ratio":    volume_ratio,
        "atr_pct":         atr_pct,
        "above_sma20":     above_sma20,
        "above_sma50":     above_sma50,
    }


def run_scanner(
    scan_date: date | None = None,
    tickers: list[str] | None = None,
    min_score: int = 1,
) -> int:
    """
    Score all universe tickers and write results to c_scan_results.
    Returns count of rows written.

    Skips tickers already scored for scan_date (idempotent).
    """
    from core.db import get_client

    today     = scan_date or date.today()
    today_iso = today.isoformat()
    symbols   = tickers or get_tickers()
    db        = get_client()

    # Check if already ran today (idempotent)
    existing = (
        db.table("c_scan_results")
        .select("ticker")
        .eq("scan_date", today_iso)
        .execute()
        .data
    ) or []
    already_scored = {r["ticker"] for r in existing}
    symbols = [s for s in symbols if s not in already_scored]

    if not symbols:
        return len(existing)

    # Bulk-fetch daily history for all tickers at once
    try:
        raw = yf.download(
            symbols,
            period=_HISTORY_DAYS,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"[scanner] yfinance download failed: {e}")
        return 0

    rows_written = 0
    for ticker in symbols:
        try:
            # Extract per-ticker DataFrame from multi-level columns
            if len(symbols) == 1:
                hist = raw
            else:
                hist = raw[ticker] if ticker in raw.columns.get_level_values(0) else pd.DataFrame()

            hist = hist.dropna(subset=["Close"])
            fields = _score_ticker(hist)

            if fields["technical_score"] < min_score:
                continue

            db.table("c_scan_results").insert({
                "scan_date":       today_iso,
                "ticker":          ticker,
                "technical_score": fields["technical_score"],
                "current_price":   fields["current_price"],
                "avg_volume":      fields["avg_volume"],
                "sector":          get_sector(ticker),
                "rsi":             fields["rsi"],
                "volume_ratio":    fields["volume_ratio"],
                "atr_pct":         fields["atr_pct"],
                "above_sma20":     fields["above_sma20"],
                "above_sma50":     fields["above_sma50"],
            }).execute()
            rows_written += 1

        except ValueError:
            pass  # filtered out — price range, ATR, volume
        except Exception as e:
            print(f"[scanner] {ticker}: {e}")

    print(f"[scanner] {rows_written} tickers written for {today_iso} "
          f"(skipped {len(symbols) - rows_written} below threshold or errored)")
    return rows_written + len(already_scored)
