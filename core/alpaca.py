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


# ── Orders ─────────────────────────────────────────────────────────────────────

def submit_bracket_order(
    ticker:       str,
    shares:       int,
    entry_price:  float,
    target_price: float,
    stop_price:   float,
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
                if ask > entry_price * 1.025:
                    print(f"  [alpaca] {ticker} stale: ask ${ask:.2f} is "
                          f"{(ask-entry_price)/entry_price:.1%} above proposal ${entry_price:.2f} — skip")
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

    for _ in range(60):   # 120s total (was 30s — premarket bracket orders need up to 2 min)
        time.sleep(2)
        try:
            o      = _client().get_order_by_id(str(order.id))
            status = str(o.status).lower()
            if status in ("filled", "partially_filled"):
                fill_price = float(o.filled_avg_price) if o.filled_avg_price else None
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
        except Exception:
            pass

    # Still pending after 120s — record for reconciliation
    print(f"  [alpaca] {ticker} fill pending after 120s — recording for reconcile")
    return str(order.id), None


def get_bracket_status(order_id: str) -> dict:
    """
    Return the fill state of a bracket order: entry fill and exit leg fill.
    Used by reconcile_positions to backfill actual entry prices and sync exits.
    """
    try:
        order        = _client().get_order_by_id(order_id)
        order_status = str(order.status).lower()
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
                status = str(o.status).lower()
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
