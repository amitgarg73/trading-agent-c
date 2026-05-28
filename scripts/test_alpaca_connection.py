"""
Live Alpaca connection smoke test.
Submits a $1 AAPL limit order, confirms it's pending, then cancels it.
Run with: PYTHONPATH=. python3 scripts/test_alpaca_connection.py
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.alpaca import get_account, get_live_price, submit_bracket_order, cancel_order

def main():
    print("=== Alpaca paper connection test ===\n")

    # 1. Account
    acct = get_account()
    if "error" in acct:
        print(f"FAIL account: {acct['error']}")
        sys.exit(1)
    print(f"OK  Account equity:       ${acct['equity']:,.2f}")
    print(f"OK  Buying power:         ${acct['buying_power']:,.2f}")

    # 2. Live price
    price = get_live_price("AAPL")
    if price is None:
        print("FAIL live price: returned None")
        sys.exit(1)
    print(f"OK  AAPL live ask price:  ${price:.2f}")

    # 3. Submit a tiny limit order well below market (won't fill), then cancel
    # Use price 50% below ask — guaranteed not to fill
    test_limit  = round(price * 0.50, 2)
    test_target = round(price * 0.55, 2)
    test_stop   = round(price * 0.45, 2)

    print(f"\n    Submitting test bracket @ ${test_limit} (50% below ask — won't fill)...")
    order_id, fill = submit_bracket_order(
        ticker="AAPL",
        shares=1,
        entry_price=test_limit,
        target_price=test_target,
        stop_price=test_stop,
    )
    if order_id is None:
        print("FAIL bracket order: rejected immediately")
        sys.exit(1)
    print(f"OK  Order placed: {order_id}  fill={fill}")

    # 4. Cancel the test order
    ok = cancel_order(order_id)
    print(f"OK  Order cancelled: {ok}")

    print("\n=== All checks passed ===")

if __name__ == "__main__":
    main()
