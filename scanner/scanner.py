from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date
from typing import Any

import yfinance as yf
import pandas as pd

from scanner.universe import get_tickers, get_sector

_MIN_PRICE        = 10.0          # no upper cap — positions are dollar-sized
_MAX_ATR_PCT      = 10.0          # raised from 5% — semis / high-beta leaders need room
_MIN_AVG_VOL      = 500_000
_DOWNLOAD_TIMEOUT = 90
_HISTORY_DAYS     = "90d"         # 90d for reliable SMA50 + 52W proximity


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
    ema12  = closes.ewm(span=12, adjust=False).mean()
    ema26  = closes.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal
    return round(float(hist.iloc[-1]), 4) if not hist.empty else 0.0


def _compute_atr_pct(hist: pd.DataFrame, period: int = 14) -> float:
    if len(hist) < period + 1:
        return 0.0
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    prev_c  = c.shift(1)
    tr = (h - l).abs().combine((h - prev_c).abs(), max).combine((l - prev_c).abs(), max)
    atr   = float(tr.iloc[-period:].mean())
    price = float(c.iloc[-1])
    return round(atr / price * 100, 2) if price else 0.0


def _score_ticker(hist: pd.DataFrame) -> dict[str, Any]:
    """
    Score a ticker from its daily OHLCV history.

    Scoring (max 11):
      RSI          0-2   momentum zone 45-75, extended zone 40-45/75-82
      MACD         0-2   positive+rising=2, positive=1, inflecting=1
      Volume ratio 0-3   2x=3, 1.5x=2, 1.2x=1  (was max 2)
      Trend (SMA)  0-2   above both=2, above SMA20=1  (soft — no hard gate)
      52W proximity 0-2  within 5% of 52W high=2, within 15%=1

    Hard gates removed:
      - No $500 price cap (was eliminating AMAT, CRWD, GS, MU, CAT, etc.)
      - No ATR% = 5% cap (was eliminating LRCX, AVGO, KLAC, QCOM, PLTR)
      - SMA20 is now a scoring signal not a rejection gate
      - MACD is now a scoring signal not a rejection gate

    Raises ValueError on truly disqualifying conditions:
      price < $10, avg volume < 500K, ATR > 10%, insufficient history
    """
    if len(hist) < 55:
        raise ValueError("insufficient history")

    closes  = hist["Close"]
    volumes = hist["Volume"]

    price = round(float(closes.iloc[-1]), 2)
    if price < _MIN_PRICE:
        raise ValueError(f"price {price} below minimum")

    avg_vol = int(volumes.iloc[-20:].mean())
    if avg_vol < _MIN_AVG_VOL:
        raise ValueError(f"avg volume {avg_vol} too low")

    atr_pct = _compute_atr_pct(hist)
    if atr_pct > _MAX_ATR_PCT:
        raise ValueError(f"ATR% {atr_pct:.1f} too high (>{_MAX_ATR_PCT})")

    rsi       = _compute_rsi(closes)
    macd      = _compute_macd_hist(closes)
    prev_macd = _compute_macd_hist(closes.iloc[:-1])

    sma20 = float(closes.iloc[-20:].mean())
    sma50 = float(closes.iloc[-50:].mean())

    today_vol    = float(volumes.iloc[-1])
    avg_vol_20d  = float(volumes.iloc[-21:-1].mean()) if len(volumes) > 21 else avg_vol
    volume_ratio = round(today_vol / avg_vol_20d, 2) if avg_vol_20d > 0 else 1.0

    above_sma20 = price > sma20
    above_sma50 = price > sma50

    # 52-week high proximity — leaders near 52W high on strong relative strength
    lookback = min(252, len(hist))
    high_52w  = float(hist["High"].iloc[-lookback:].max())
    pct_from_52w_high = round((high_52w - price) / high_52w * 100, 1) if high_52w > 0 else 100.0

    # ── Scoring ───────────────────────────────────────────────────────────────
    score = 0

    # RSI: momentum zone — extended to 82 for bull market leaders
    if 45 <= rsi <= 75:
        score += 2
    elif 40 <= rsi < 45 or 75 < rsi <= 82:
        score += 1

    # MACD: soft signal — reward both acceleration and inflection
    if macd > 0 and macd > prev_macd:
        score += 2
    elif macd > 0:
        score += 1
    elif macd > prev_macd:
        score += 1  # turning up even if still negative = early setup

    # Volume surge — primary confirmation signal
    if volume_ratio >= 2.0:
        score += 3
    elif volume_ratio >= 1.5:
        score += 2
    elif volume_ratio >= 1.2:
        score += 1

    # Trend — soft: below SMA20 is acceptable for gap-up / breakout setups
    if above_sma20 and above_sma50:
        score += 2
    elif above_sma20:
        score += 1

    # 52-week proximity — near highs on volume = accumulation, not distribution
    if pct_from_52w_high <= 5.0:
        score += 2
    elif pct_from_52w_high <= 15.0:
        score += 1

    return {
        "technical_score":   score,
        "current_price":     price,
        "avg_volume":        avg_vol,
        "rsi":               rsi,
        "volume_ratio":      volume_ratio,
        "atr_pct":           atr_pct,
        "above_sma20":       above_sma20,
        "above_sma50":       above_sma50,
        "macd_rising":       macd > 0 and macd > prev_macd,
        "macd_inflecting":   macd > prev_macd,
        "pct_from_52w_high": pct_from_52w_high,
    }


def run_scanner(
    scan_date: date | None = None,
    tickers: list[str] | None = None,
    min_score: int = 1,
    tracer: Any | None = None,
) -> int:
    """
    Score all universe tickers and write results to c_scan_results.
    Returns count of rows written (including already-scored rows from today).
    Idempotent: skips tickers already scored for scan_date.

    `tracer` is optional and off by default, so backtests and ad-hoc runs are unaffected.
    When a session passes one, the scan reports itself as the `scanner` agent.

    WHY: this module replaced agents/scanner_agent.py, which emitted spans, and did not carry the
    tracing across. The scan kept running (126 candidates a morning) and Provy saw nothing, so
    Scanner Agent showed as a normal row with a stale grade and an empty tool strip for nine days.
    An agent that stops reporting must not look like one that had a quiet week.
    """
    from core.db import get_client

    if tracer:
        tracer.start_agent_span("scanner")

    today     = scan_date or date.today()
    today_iso = today.isoformat()
    symbols   = tickers or get_tickers()
    db        = get_client()

    existing = (
        db.table("c_scan_results")
        .select("ticker")
        .eq("date", today_iso)
        .execute()
        .data
    ) or []
    already_scored = {r["ticker"] for r in existing}
    symbols = [s for s in symbols if s not in already_scored]

    if not symbols:
        if tracer:
            tracer.log_decision("scanner", "already_scored",
                                detail={"tickers": len(already_scored), "scan_date": today_iso})
        return len(already_scored)

    try:
        with ThreadPoolExecutor(max_workers=1) as _pool:
            _future = _pool.submit(
                yf.download,
                symbols,
                period=_HISTORY_DAYS,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            raw = _future.result(timeout=_DOWNLOAD_TIMEOUT)
    except FuturesTimeoutError:
        print(f"[scanner] yfinance download timed out after {_DOWNLOAD_TIMEOUT}s, aborting scan")
        if tracer:
            tracer.log_error("scanner", f"price download timed out after {_DOWNLOAD_TIMEOUT}s")
        return 0
    except Exception as e:
        print(f"[scanner] yfinance download failed: {e}")
        if tracer:
            tracer.log_error("scanner", f"price download failed: {e}")
        return 0

    rows_written     = 0
    filtered_price   = 0
    filtered_atr     = 0
    filtered_volume  = 0
    filtered_score   = 0

    for ticker in symbols:
        try:
            if len(symbols) == 1:
                hist = raw
            else:
                hist = raw[ticker] if ticker in raw.columns.get_level_values(0) else pd.DataFrame()

            if hist.empty or "Close" not in hist.columns:
                continue
            hist   = hist.dropna(subset=["Close"])
            fields = _score_ticker(hist)

            if fields["technical_score"] < min_score:
                filtered_score += 1
                continue

            db.table("c_scan_results").insert({
                "date":   today_iso,
                "ticker": ticker,
                "score":  fields["technical_score"],
                "price":  fields["current_price"],
                "sector": get_sector(ticker),
            }).execute()
            rows_written += 1

        except ValueError as e:
            msg = str(e)
            if "price" in msg:
                filtered_price += 1
            elif "ATR" in msg:
                filtered_atr += 1
            elif "volume" in msg:
                filtered_volume += 1
        except Exception as e:
            print(f"[scanner] {ticker}: {e}")

    print(
        f"[scanner] {rows_written} tickers written for {today_iso} "
        f"(price={filtered_price}, atr={filtered_atr}, "
        f"volume={filtered_volume}, score={filtered_score})"
    )
    total = rows_written + len(already_scored)
    if tracer:
        tracer.log_decision("scanner", "candidates_scored", detail={
            "candidates": total, "written": rows_written, "considered": len(symbols),
            "filtered_price": filtered_price, "filtered_atr": filtered_atr,
            "filtered_volume": filtered_volume, "filtered_score": filtered_score,
        })
    return total
