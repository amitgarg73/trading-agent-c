from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock
import pytest


def _mock_client():
    return MagicMock()


def _mock_order(order_id="ord-001", status="filled", filled_avg_price=185.0, legs=None):
    o = MagicMock()
    o.id = order_id
    o.status = status
    o.filled_avg_price = filled_avg_price
    o.client_order_id = f"stratc_AAPL_20260527120000"
    o.legs = legs or []
    o.symbol = "AAPL"
    return o


def _mock_position(ticker="AAPL", current_price=186.0, unrealized_pl=100.0):
    p = MagicMock()
    p.symbol = ticker
    p.current_price = str(current_price)
    p.unrealized_pl = str(unrealized_pl)
    return p


# ── get_account ────────────────────────────────────────────────────────────────

class TestGetAccount:
    def test_returns_equity_and_buying_power(self):
        acct = MagicMock()
        acct.equity       = "51234.56"
        acct.buying_power = "25000.00"
        acct.cash         = "10000.00"
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_account.return_value = acct
            from core.alpaca import get_account
            result = get_account()
        assert result["equity"]       == 51234.56
        assert result["buying_power"] == 25000.00
        assert result["cash"]         == 10000.00

    def test_returns_error_on_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_account.side_effect = Exception("auth failed")
            from core.alpaca import get_account
            result = get_account()
        assert "error" in result


# ── get_live_price ─────────────────────────────────────────────────────────────

class TestGetLivePrice:
    def test_returns_ask_price(self):
        quote = MagicMock()
        quote.ask_price = "185.50"
        quote.bid_price = "185.40"
        with patch("core.alpaca._dclient") as mc:
            mc.return_value.get_stock_latest_quote.return_value = {"AAPL": quote}
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result == pytest.approx(185.50)

    def test_falls_back_to_bid_when_ask_zero(self):
        quote = MagicMock()
        quote.ask_price = "0"
        quote.bid_price = "185.40"
        with patch("core.alpaca._dclient") as mc:
            mc.return_value.get_stock_latest_quote.return_value = {"AAPL": quote}
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result == pytest.approx(185.40)

    def test_returns_none_on_exception(self):
        with patch("core.alpaca._dclient") as mc:
            mc.return_value.get_stock_latest_quote.side_effect = Exception("rate limit")
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result is None


# ── submit_bracket_order ───────────────────────────────────────────────────────

class TestSubmitBracketOrder:
    def test_returns_order_id_and_fill_price_on_fill(self):
        order = _mock_order(status="filled", filled_avg_price=185.20)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        assert fill == pytest.approx(185.20)

    def test_returns_none_none_on_rejection(self):
        submitted = _mock_order(status="new")
        rejected  = _mock_order(status="rejected", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = submitted
            mc.return_value.get_order_by_id.return_value = rejected
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid is None
        assert fill is None

    def test_returns_order_id_with_none_fill_when_pending(self):
        pending = _mock_order(status="new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = pending
            mc.return_value.get_order_by_id.return_value = pending
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        assert fill is None

    def test_limit_price_has_buffer(self):
        order = _mock_order(status="filled", filled_avg_price=185.20)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.limit_price == pytest.approx(185.0 * 1.001, rel=1e-3)


# ── get_order_fill ─────────────────────────────────────────────────────────────

class TestGetOrderFill:
    def _leg(self, order_type, status, filled_avg_price):
        leg = MagicMock()
        leg.order_type      = order_type
        leg.status          = status
        leg.filled_avg_price = filled_avg_price
        return leg

    def test_target_exit(self):
        leg   = self._leg("limit", "filled", 192.0)
        order = _mock_order(legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("ord-001")
        assert price  == pytest.approx(192.0)
        assert reason == "TARGET"

    def test_stop_exit(self):
        leg   = self._leg("stop_limit", "filled", 183.0)
        order = _mock_order(legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("ord-001")
        assert price  == pytest.approx(183.0)
        assert reason == "STOP"

    def test_returns_none_when_no_filled_leg(self):
        leg   = self._leg("limit", "new", None)
        order = _mock_order(legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("ord-001")
        assert price  is None
        assert reason is None


# ── get_position_data ──────────────────────────────────────────────────────────

class TestGetPositionData:
    def test_returns_price_and_pnl(self):
        pos = _mock_position("AAPL", 186.0, 100.0)
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_open_position.return_value = pos
            from core.alpaca import get_position_data
            result = get_position_data("AAPL")
        assert result["current_price"]  == pytest.approx(186.0)
        assert result["unrealized_pnl"] == pytest.approx(100.0)

    def test_returns_none_when_position_not_found(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_open_position.side_effect = Exception("not found")
            from core.alpaca import get_position_data
            result = get_position_data("AAPL")
        assert result is None


# ── close_position ─────────────────────────────────────────────────────────────

class TestClosePosition:
    def test_returns_success_and_fill(self):
        order = _mock_order(status="filled", filled_avg_price=186.0)
        with patch("core.alpaca._client") as mc:
            mc.return_value.close_position.return_value = order
            from core.alpaca import close_position
            ok, fill = close_position("AAPL")
        assert ok   is True
        assert fill == pytest.approx(186.0)

    def test_returns_false_on_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.close_position.side_effect = Exception("no position")
            from core.alpaca import close_position
            ok, fill = close_position("AAPL")
        assert ok   is False
        assert fill is None


# ── cancel_all_orders ──────────────────────────────────────────────────────────

class TestCancelAllOrders:
    def test_calls_cancel(self):
        with patch("core.alpaca._client") as mc:
            from core.alpaca import cancel_all_orders
            cancel_all_orders()
        mc.return_value.cancel_orders.assert_called_once()

    def test_swallows_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.cancel_orders.side_effect = Exception("network")
            from core.alpaca import cancel_all_orders
            cancel_all_orders()  # must not raise


# ── order prefix ──────────────────────────────────────────────────────────────

class TestOrderPrefix:
    def test_client_order_id_starts_with_stratc(self):
        order = _mock_order(status="filled", filled_avg_price=185.0)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("MSFT", 5, 420.0, 435.0, 415.0)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.client_order_id.startswith("stratc_")
