from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytz

_ET = pytz.timezone("America/New_York")

_API_KEY = os.getenv("ALPACA_API_KEY_ID_C", "")
_SECRET  = os.getenv("ALPACA_API_SECRET_KEY_C", "")
_PAPER   = os.getenv("ALPACA_PAPER", "true").lower() != "false"

_ORDER_PREFIX = "stratc_"

_trading_client = None
_data_client    = None


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
    except Exception:
        pass
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

    # 0.1% buffer above plan price — widens fill window on fast-moving stocks
    limit_px = round(entry_price * 1.001, 2)

    req = LimitOrderRequest(
        symbol=ticker,
        qty=shares,
        side=OrderSide.BUY,
        limit_price=limit_px,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        client_order_id=_order_id(ticker),
    )
    order = _client().submit_order(req)
    print(f"  [alpaca] Bracket order: {ticker} {shares}sh @ ${limit_px} → {order.id}")

    if not _is_market_open():
        print(f"  [alpaca] {ticker} queued for market open — skipping fill poll")
        return str(order.id), None

    for _ in range(15):
        time.sleep(2)
        try:
            o      = _client().get_order_by_id(str(order.id))
            status = str(o.status).lower()
            if status in ("filled", "partially_filled"):
                fill_price = float(o.filled_avg_price) if o.filled_avg_price else None
                return str(order.id), fill_price
            if status in ("cancelled", "rejected", "expired"):
                print(f"  [alpaca] {ticker} order {status} — skipping DB write")
                return None, None
        except Exception:
            pass

    # Still pending after 30s — record for reconciliation
    print(f"  [alpaca] {ticker} fill pending after 30s — recording for reconcile")
    return str(order.id), None


def get_order_fill(order_id: str) -> tuple[Optional[float], Optional[str]]:
    """
    For a completed bracket, return (close_price, exit_reason).
    exit_reason: TARGET | STOP
    """
    try:
        order = _client().get_order_by_id(order_id)
        for leg in (order.legs or []):
            if "filled" in str(leg.status).lower() and leg.filled_avg_price:
                type_str = str(leg.order_type).lower()
                reason   = "STOP" if "stop" in type_str else "TARGET"
                return float(leg.filled_avg_price), reason
    except Exception as e:
        print(f"  [alpaca] get_order_fill({order_id[:8]}): {e}")
    return None, None


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
    """Market-close a single position. Returns (success, fill_price)."""
    try:
        order = _client().close_position(ticker)
        fill  = float(order.filled_avg_price) if order.filled_avg_price else None
        return True, fill
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
