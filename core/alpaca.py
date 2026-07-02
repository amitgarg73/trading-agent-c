from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytz
from dotenv import load_dotenv

load_dotenv()

_ET = pytz.timezone("America/New_York")

_API_KEY = os.getenv("ALPACA_API_KEY_ID_C", "")
_SECRET  = os.getenv("ALPACA_API_SECRET_KEY_C", "")
_PAPER   = os.getenv("ALPACA_PAPER", "true").lower() != "false"

_ORDER_PREFIX = "stratc_"

_trading_client = None
_data_client    = None
_news_client    = None


def _client():
    global _trading_client
    if _trading_client is None:
        from alpaca.trading.client import TradingClient
        _trading_client = TradingClient(_API_KEY, _SECRET, paper=_PAPER)
    return _trading_client


def _dclient():
    global _data_client
    if _data_client is None:
        from alpaca.data import StockHistoricalDataClient
        _data_client = StockHistoricalDataClient(_API_KEY, _SECRET)
    return _data_client


def _nclient():
    global _news_client
    if _news_client is None:
        from alpaca.data import NewsClient
        _news_client = NewsClient(_API_KEY, _SECRET)
    return _news_client


def _is_market_open() -> bool:
    """True between 9:25 AM and 4:00 PM ET — safe window to poll for fills."""
    now_et = datetime.now(timezone.utc).astimezone(_ET)
    t = (now_et.hour, now_et.minute)
    return (9, 25) <= t < (16, 1)


def _order_id(ticker: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{_ORDER_PREFIX}{ticker}_{ts}"


# ── Account ────────────────────────────────────────────────────────────────────

def get_account() -> dict:
    """Return equity and buying_power from Alpaca account."""
    try:
        acct = _client().get_account()
        return {
            "equity":        round(float(acct.equity), 2),
            "buying_power":  round(float(acct.buying_power), 2),
            "cash":          round(float(acct.cash), 2),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Prices ─────────────────────────────────────────────────────────────────────

def get_live_price(ticker: str) -> Optional[float]:
    """Return latest ask price for a ticker, or None on failure."""
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        req    = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
        quotes = _dclient().get_stock_latest_quote(req)
        q = quotes.get(ticker)
        if q:
            ask = getattr(q, "ask_price", None)
            bid = getattr(q, "bid_price", None)
            if ask and float(ask) > 0:
                return round(float(ask), 4)
            if bid and float(bid) > 0:
                return round(float(bid), 4)
    except Exception as e:
        print(f"  [alpaca] get_live_price({ticker}): {e}")
    return None


def get_day_open(ticker: str) -> Optional[float]:
    """
    Return today's regular-session OPEN price for a ticker, or None on failure.
    Used by the entry-chase guard to skip entries that have already run up off the
    open. Returns None (guard fails safe) before the open or on any data error.
    """
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snap = _dclient().get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=[ticker])).get(ticker)
        if snap and getattr(snap, "daily_bar", None) and float(snap.daily_bar.open) > 0:
            return round(float(snap.daily_bar.open), 4)
    except Exception as e:
        print(f"  [alpaca] get_day_open({ticker}): {e}")
    return None


# ── Orders ─────────────────────────────────────────────────────────────────────

def submit_bracket_order(
    ticker:       str,
    shares:       int,
    entry_price:  float,
    target_price: float,
    stop_price:   float,
    max_entry_premium: float = 0.0,
) -> tuple[Optional[str], Optional[float]]:
    """
    Submit a bracket order (limit entry + take-profit + stop-loss).
    Polls up to 30s for fill confirmation.

    Returns (order_id, fill_price):
    - fill_price is None if fill is still pending after 30s (reconcile will backfill)
    - Both None if order is rejected/cancelled
    """
    from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    # Fetch current ask at submission time and use it as the limit price.
    # Using ask guarantees fill on momentum entries — a limit at bid sits below
    # the ask and never fills in a trending market (today's observed root cause).
    # Stop/target are reprojected at the same % distances from ask.
    # Staleness gate: if ask is >1.5% above the research proposal, the stock has
    # already run — skip without submitting.
    # Fallback: get_live_prices() if ask is unavailable (premarket IEX feed).
    limit_px   = round(entry_price, 2)   # fallback: proposal price
    eff_stop   = round(stop_price, 2)
    eff_target = round(target_price, 2)
    _quote_found = False
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        req_q = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
        q = _dclient().get_stock_latest_quote(req_q).get(ticker)
        if q:
            raw_ask = getattr(q, "ask_price", None)
            if raw_ask and float(raw_ask) > 0:
                ask = round(float(raw_ask), 4)
                if ask > entry_price * 1.04:
                    print(f"  [alpaca] {ticker} STALENESS GATE: ask ${ask:.2f} is "
                          f"{(ask-entry_price)/entry_price:.1%} above proposal ${entry_price:.2f} — skipping order")
                    return None, None
                # Entry-chase guard: skip if the stock has already run up off the day's
                # open. Late entries above the open are the dominant execution drag
                # (design/why-we-are-losing-plain-english.md). Inactive when
                # max_entry_premium <= 0, or before the open (day_open unknown).
                if max_entry_premium and max_entry_premium > 0:
                    from core.scoring import is_chasing_entry
                    day_open = get_day_open(ticker)
                    if is_chasing_entry(ask, day_open, max_entry_premium):
                        print(f"  [alpaca] {ticker} CHASE GATE: ask ${ask:.2f} is "
                              f"{(ask-day_open)/day_open:.1%} above day open ${day_open:.2f} "
                              f"(max {max_entry_premium:.1%}) — skipping order")
                        return None, None
                stop_pct   = (entry_price - stop_price)  / entry_price
                target_pct = (target_price - entry_price) / entry_price
                limit_px   = round(ask * 1.001, 2)   # 0.1% buffer ensures fill even on minor ask movement
                eff_stop   = round(ask * (1 - stop_pct),   2)
                eff_target = round(ask * (1 + target_pct), 2)
                _quote_found = True
                if ask < entry_price * 0.95:
                    print(f"  [alpaca] {ticker} market moved down: ask ${ask:.2f} vs proposal ${entry_price:.2f} "
                          f"({(entry_price-ask)/entry_price:.1%} gap) — limit at ask")
    except Exception:
        pass  # keep fallback prices

    if not _quote_found:
        # No ask available (premarket ask=0 on IEX feed).
        # Fall back to get_live_price() which accepts ask-only or bid-only quotes.
        # Re-anchor stop/target to live price so they stay valid relative to fill.
        try:
            live_px = get_live_price(ticker)
            if live_px and entry_price > 0 and abs(live_px - entry_price) / entry_price > 0.005:
                stop_pct   = (entry_price - stop_price)  / entry_price
                target_pct = (target_price - entry_price) / entry_price
                limit_px   = round(live_px, 2)
                eff_stop   = round(live_px * (1 - stop_pct),   2)
                eff_target = round(live_px * (1 + target_pct), 2)
                print(f"  [alpaca] {ticker} no ask available — live ${live_px:.2f} used (proposal ${entry_price:.2f})")
        except Exception:
            pass  # keep fallback prices

    req = LimitOrderRequest(
        symbol=ticker,
        qty=shares,
        side=OrderSide.BUY,
        limit_price=limit_px,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=eff_target),
        stop_loss=StopLossRequest(stop_price=eff_stop),
        client_order_id=_order_id(ticker),
    )
    order = _client().submit_order(req)
    print(f"  [alpaca] Bracket order: {ticker} {shares}sh @ ${limit_px} → {order.id}")

    if not _is_market_open():
        print(f"  [alpaca] {ticker} queued for market open — skipping fill poll")
        return str(order.id), None

    last_status = "unknown"
    for i in range(90):   # 180s total — paper account bracket status can lag 2-3 min
        time.sleep(2)
        try:
            o      = _client().get_order_by_id(str(order.id))
            status = str(o.status).lower().split(".")[-1]  # normalize "orderstatus.filled" → "filled"
            if status != last_status:
                print(f"  [alpaca] {ticker} order status → {status} (poll {i+1})")
                last_status = status
            if status in ("filled", "partially_filled"):
                fill_price = float(o.filled_avg_price) if o.filled_avg_price else None
                print(f"  [alpaca] {ticker} filled @ avg ${fill_price} — cancelling bracket legs")
                # Cancel both stop-loss AND take-profit legs so trailing stop can be submitted.
                # Alpaca counts all bracket child legs against available qty — leaving either
                # open blocks the trailing stop submission.
                for leg in (o.legs or []):
                    lt = str(leg.order_type).lower()
                    ls = str(leg.status).lower()
                    if "trailing" not in lt and "cancel" not in ls and "fill" not in ls:
                        try:
                            _client().cancel_order_by_id(str(leg.id))
                            print(f"  [alpaca] {ticker} bracket leg cancelled ({lt}) — trail will be submitted")
                        except Exception:
                            pass
                return str(order.id), fill_price
            if status in ("cancelled", "rejected", "expired"):
                print(f"  [alpaca] {ticker} order {status} — skipping DB write")
                return None, None
        except Exception as e:
            print(f"  [alpaca] {ticker} poll error (attempt {i+1}): {e}")

    # Still pending after 180s — record for reconciliation
    print(f"  [alpaca] {ticker} fill still pending after 180s (last status: {last_status}) — recording for reconcile")
    return str(order.id), None


def get_bracket_status(order_id: str) -> dict:
    """
    Return the fill state of a bracket order: entry fill and exit leg fill.
    Used by reconcile_positions to backfill actual entry prices and sync exits.
    """
    try:
        order        = _client().get_order_by_id(order_id)
        order_status = str(order.status).lower().split(".")[-1]  # normalize enum prefix
        entry_filled = order_status in ("filled", "partially_filled")
        entry_price  = round(float(order.filled_avg_price), 4) if order.filled_avg_price else None

        exit_price = None
        exit_reason = None
        exit_filled = False
        for leg in (order.legs or []):
            if "filled" in str(leg.status).lower() and leg.filled_avg_price:
                type_str    = str(leg.order_type).lower()
                if "trailing" in type_str:
                    exit_reason = "NATIVE_TRAIL"
                elif "stop" in type_str:
                    exit_reason = "STOP"
                else:
                    exit_reason = "TARGET"
                exit_price  = round(float(leg.filled_avg_price), 4)
                exit_filled = True
                break

        return {
            "entry_filled": entry_filled,
            "entry_price":  entry_price,
            "exit_filled":  exit_filled,
            "exit_price":   exit_price,
            "exit_reason":  exit_reason,
            "order_status": order_status,
        }
    except Exception as e:
        return {"error": str(e)}


def get_order_fill(order_id: str) -> tuple[Optional[float], Optional[str]]:
    """
    For a completed bracket or trailing stop, return (close_price, exit_reason).
    exit_reason: TARGET | NATIVE_TRAIL | STOP
    """
    try:
        order    = _client().get_order_by_id(order_id)
        type_str = str(order.order_type).lower()
        status   = str(order.status).lower()

        # Standalone trailing stop order (not a bracket leg)
        if "trailing" in type_str and "filled" in status and order.filled_avg_price:
            return float(order.filled_avg_price), "NATIVE_TRAIL"

        # Bracket legs
        for leg in (order.legs or []):
            if "filled" in str(leg.status).lower() and leg.filled_avg_price:
                leg_type = str(leg.order_type).lower()
                if "trailing" in leg_type:
                    reason = "NATIVE_TRAIL"
                elif "stop" in leg_type:
                    reason = "STOP"
                else:
                    reason = "TARGET"
                return float(leg.filled_avg_price), reason
    except Exception as e:
        print(f"  [alpaca] get_order_fill({order_id[:8]}): {e}")
    return None, None


def submit_trailing_stop(ticker: str, shares: int, trail_pct: float) -> Optional[str]:
    """
    Submit a standalone trailing stop sell order for an open position.
    Alpaca tracks the high-watermark server-side and fires on reversal.
    Returns order ID or None on failure.
    """
    from alpaca.trading.requests import TrailingStopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    try:
        req = TrailingStopOrderRequest(
            symbol=ticker,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            trail_percent=round(trail_pct * 100, 2),
            client_order_id=_order_id(ticker),
        )
        order = _client().submit_order(req)
        print(f"  [alpaca] Trailing stop: {ticker} {shares}sh {trail_pct*100:.1f}% → {order.id}")
        return str(order.id)
    except Exception as e:
        print(f"  [alpaca] submit_trailing_stop({ticker}): {e}")
        return None


def submit_opening_order(ticker: str, shares: int, limit_price: Optional[float] = None,
                         on_open: bool = True) -> Optional[str]:
    """
    Submit an entry BUY. Default (on_open=True) uses TimeInForce.OPG so the fill is the regular-session
    open — market-on-open, or limit-on-open if limit_price is given. OPG must be submitted before the
    ~09:28 ET auction cutoff and cannot be a bracket, so protection attaches AFTER the fill via
    submit_trailing_stop, as the chase path already does post-fill.

    on_open=False submits a same-day (DAY) order instead — the near-open FALLBACK used only when
    premarket missed the auction window, so a premarket miss does not cost the whole day. Same code,
    no chase/staleness gate; entering near the open is still the discipline. Returns the order id or None.
    """
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    tif = TimeInForce.OPG if on_open else TimeInForce.DAY
    try:
        if limit_price is not None:
            req = LimitOrderRequest(
                symbol=ticker, qty=shares, side=OrderSide.BUY,
                time_in_force=tif, limit_price=round(limit_price, 2),
                client_order_id=_order_id(ticker),
            )
        else:
            req = MarketOrderRequest(
                symbol=ticker, qty=shares, side=OrderSide.BUY,
                time_in_force=tif, client_order_id=_order_id(ticker),
            )
        order = _client().submit_order(req)
        kind = ("LOO" if limit_price is not None else "MOO") if on_open else \
               ("limit" if limit_price is not None else "market")
        print(f"  [alpaca] {'Opening' if on_open else 'Near-open fallback'} order ({kind}): "
              f"{ticker} {shares}sh → {order.id}")
        return str(order.id)
    except Exception as e:
        print(f"  [alpaca] submit_opening_order({ticker}): {e}")
        return None


# ── Positions ──────────────────────────────────────────────────────────────────

def get_position_data(ticker: str) -> Optional[dict]:
    """Return current_price and unrealized_pnl for an open Alpaca position."""
    try:
        p = _client().get_open_position(ticker)
        return {
            "current_price":  round(float(p.current_price), 4),
            "unrealized_pnl": round(float(p.unrealized_pl), 2),
        }
    except Exception:
        return None


def get_open_alpaca_tickers() -> set[str]:
    """Return set of ticker symbols currently held in Alpaca."""
    try:
        return {p.symbol for p in _client().get_all_positions()}
    except Exception:
        return set()


def close_position(ticker: str) -> tuple[bool, Optional[float]]:
    """Market-close a single position. Returns (success, fill_price).

    Polls up to 30s for a fill when market is open, matching the entry-order
    pattern. Returns (True, None) when fill is still pending after the poll.
    """
    try:
        order = _client().close_position(ticker)
        if order.filled_avg_price:
            return True, float(order.filled_avg_price)

        if not _is_market_open():
            print(f"  [alpaca] {ticker} close queued — market closed, fill pending")
            return True, None

        for _ in range(15):
            time.sleep(2)
            try:
                o      = _client().get_order_by_id(str(order.id))
                status = str(o.status).lower().split(".")[-1]  # normalize "orderstatus.filled" → "filled"
                if status in ("filled", "partially_filled") and o.filled_avg_price:
                    return True, float(o.filled_avg_price)
                if status in ("cancelled", "rejected", "expired"):
                    return True, None
            except Exception:
                pass

        print(f"  [alpaca] {ticker} close fill pending after 30s")
        return True, None
    except Exception as e:
        print(f"  [alpaca] close_position({ticker}): {e}")
        return False, None


def close_all_strategy_positions() -> list[dict]:
    """
    Close every open position tagged with stratc_ from the last 2 days.
    Filters by client_order_id prefix so we never touch other strategies' positions.
    """
    results = []
    try:
        positions = _client().get_all_positions()
    except Exception as e:
        print(f"  [alpaca] get_all_positions failed: {e}")
        return results

    # Filter to only stratc_-tagged positions
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        cutoff = (datetime.utcnow() - timedelta(days=2)).replace(tzinfo=timezone.utc)
        orders = _client().get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500, after=cutoff
        ))
        owned = {str(o.symbol) for o in orders
                 if str(o.client_order_id or "").startswith(_ORDER_PREFIX)}
        positions = [p for p in positions if p.symbol in owned]
    except Exception as e:
        print(f"  [alpaca] tag filter failed ({e}) — closing all")

    for pos in positions:
        ok, fill = close_position(pos.symbol)
        results.append({"ticker": pos.symbol, "success": ok, "fill_price": fill})
        print(f"  [alpaca] {'OK' if ok else 'FAIL'} close {pos.symbol} @ "
              f"{'$' + str(fill) if fill else 'pending'}")
    return results


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cancel_all_orders() -> None:
    """Cancel all open orders — call before EOD close to clear pending bracket legs."""
    try:
        _client().cancel_orders()
        print("  [alpaca] All open orders cancelled")
    except Exception as e:
        print(f"  [alpaca] cancel_all_orders: {e}")


def cancel_order(order_id: str) -> bool:
    """Cancel a single order by ID."""
    try:
        _client().cancel_order_by_id(order_id)
        return True
    except Exception:
        return False


def _cancel_bracket_stop_leg(order_id: str) -> None:
    """
    Cancel all open bracket legs so a trailing stop can be submitted without conflict.
    Alpaca counts both the stop-loss and take-profit legs against available qty, so
    both must be cancelled before a standalone trailing stop can be accepted.
    """
    try:
        o = _client().get_order_by_id(order_id)
        for leg in (o.legs or []):
            leg_type   = str(getattr(leg.order_type, "value", str(leg.order_type))).lower()
            leg_status = str(getattr(leg.status,     "value", str(leg.status))).lower()
            if "trailing" not in leg_type and "cancel" not in leg_status and "fill" not in leg_status:
                try:
                    _client().cancel_order_by_id(str(leg.id))
                except Exception:
                    pass
        print(f"  [alpaca] bracket legs cleared — trailing stop will replace them")
    except Exception as e:
        print(f"  [alpaca] _cancel_bracket_stop_leg: {e}")


def get_gap_up_tickers(min_gap_pct: float = 2.0, top_n: int = 20) -> list[dict]:
    """
    Return today's top gainers with percent_change >= min_gap_pct.
    Each dict: {ticker, gap_pct, price}.
    Returns [] on any error so callers can treat it as an optional signal.
    """
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest
        from alpaca.data.enums import MarketType
        api_key    = os.environ.get("ALPACA_API_KEY_ID_C", "")
        secret_key = os.environ.get("ALPACA_API_SECRET_KEY_C", "")
        client     = ScreenerClient(api_key, secret_key)
        req        = MarketMoversRequest(market_type=MarketType.STOCKS, top=top_n)
        movers     = client.get_market_movers(req)
        return [
            {"ticker": m.symbol, "gap_pct": m.percent_change, "price": m.price}
            for m in (movers.gainers or [])
            if m.percent_change >= min_gap_pct
        ]
    except Exception as e:
        print(f"  [alpaca] get_gap_up_tickers: {e}")
        return []


def get_sector_etf_changes(etf_symbols: list[str]) -> dict[str, float]:
    """
    Batch-fetch today's % change for a list of ETF symbols.
    Returns {etf_symbol: pct_change_today}.
    Uses snapshot API: (daily_bar.close - previous_daily_bar.close) / previous_daily_bar.close.
    Returns {} on error — callers treat sector context as optional.
    """
    try:
        from alpaca.data.requests import StockSnapshotRequest
        req = StockSnapshotRequest(symbol_or_symbols=etf_symbols)
        snaps = _dclient().get_stock_snapshot(req)
        result = {}
        for symbol, snap in snaps.items():
            try:
                curr  = float(snap.daily_bar.close)
                prev  = float(snap.previous_daily_bar.close)
                result[symbol] = round((curr - prev) / prev * 100, 2) if prev else 0.0
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"  [alpaca] get_sector_etf_changes: {e}")
        return {}


def batch_get_intraday_signals(tickers: list[str]) -> dict[str, dict]:
    """
    Batch-fetch first-session intraday signals for a list of tickers.
    Computes VWAP, RS vs SPY, ORB (first 30 min) status from minute bars since market open.
    Returns {ticker: {available, live_price, vwap, above_vwap, rs_vs_spy, orb_pct, above_orb}}.
    Used by intraday _screen_candidates() to rank before LLM investigation.
    Returns {} on error — callers treat as optional enrichment.
    """
    try:
        import pytz
        from datetime import datetime, timezone
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        et = pytz.timezone("America/New_York")
        now_et      = datetime.now(et)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        all_symbols = list(set(tickers + ["SPY"]))
        req = StockBarsRequest(
            symbol_or_symbols=all_symbols,
            timeframe=TimeFrame.Minute,
            start=market_open.astimezone(timezone.utc),
            end=now_et.astimezone(timezone.utc),
            feed="iex",
        )
        all_bars = _dclient().get_stock_bars(req).data

        spy_bars  = all_bars.get("SPY", [])
        spy_open  = float(spy_bars[0].open)  if spy_bars else None
        spy_curr  = float(spy_bars[-1].close) if spy_bars else None
        spy_pct   = round((spy_curr - spy_open) / spy_open * 100, 3) if spy_open and spy_curr else None

        result: dict[str, dict] = {}
        for ticker in tickers:
            bars = all_bars.get(ticker, [])
            if not bars:
                result[ticker] = {"available": False}
                continue

            # VWAP (dollar-weighted)
            total_pv  = sum(float(getattr(b, "vwap", None) or (b.high + b.low + b.close) / 3) * float(b.volume) for b in bars)
            total_vol = sum(float(b.volume) for b in bars)
            vwap      = round(total_pv / total_vol, 2) if total_vol > 0 else None
            curr      = round(float(bars[-1].close), 2)

            # RS vs SPY
            open_price  = float(bars[0].open)
            ticker_pct  = round((curr - open_price) / open_price * 100, 3) if open_price else None
            rs_vs_spy   = round(ticker_pct - spy_pct, 2) if ticker_pct is not None and spy_pct is not None else None
            rs_vs_spy   = round(max(-20.0, min(20.0, rs_vs_spy)), 2) if rs_vs_spy is not None else None

            # ORB: high of first 30 min bars
            orb_bars = bars[:30]
            orb_high = max(float(b.high) for b in orb_bars) if orb_bars else None
            orb_pct  = round((curr - orb_high) / orb_high * 100, 2) if orb_high else None

            result[ticker] = {
                "available":  True,
                "live_price": curr,
                "vwap":       vwap,
                "above_vwap": curr > vwap if vwap else None,
                "rs_vs_spy":  rs_vs_spy,
                "orb_pct":    orb_pct,
                "above_orb":  curr > orb_high if orb_high else None,
            }

        return result
    except Exception as e:
        print(f"  [alpaca] batch_get_intraday_signals: {e}")
        return {}
