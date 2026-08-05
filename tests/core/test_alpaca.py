from __future__ import annotations

from types import SimpleNamespace
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
        with patch("core.alpaca._dclient") as mc, \
             patch("core.alpaca.get_last_trade", return_value=185.45):
            mc.return_value.get_stock_latest_quote.return_value = {"AAPL": quote}
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result == pytest.approx(185.50)

    def test_stale_ask_falls_back_to_the_traded_price(self):
        # The 2026-07-31 condition: a quote nothing has traded near.
        quote = MagicMock()
        quote.ask_price = "149.61"
        quote.bid_price = "0"
        with patch("core.alpaca._dclient") as mc, \
             patch("core.alpaca.get_last_trade", return_value=143.24):
            mc.return_value.get_stock_latest_quote.return_value = {"TGT": quote}
            from core.alpaca import get_live_price
            result = get_live_price("TGT")
        assert result == pytest.approx(143.24)

    def test_falls_back_to_bid_when_ask_zero(self):
        quote = MagicMock()
        quote.ask_price = "0"
        quote.bid_price = "185.40"
        with patch("core.alpaca._dclient") as mc:
            mc.return_value.get_stock_latest_quote.return_value = {"AAPL": quote}
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result == pytest.approx(185.40)

    def test_quote_failure_falls_back_to_the_last_trade(self):
        # Behaviour change 2026-07-31: a failed quote used to mean no price at all.
        # A trade that actually happened is better than nothing.
        with patch("core.alpaca._dclient") as mc, \
             patch("core.alpaca.get_last_trade", return_value=185.10):
            mc.return_value.get_stock_latest_quote.side_effect = Exception("rate limit")
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result == pytest.approx(185.10)

    def test_returns_none_when_both_sources_fail(self):
        with patch("core.alpaca._dclient") as mc, \
             patch("core.alpaca.get_last_trade", return_value=None):
            mc.return_value.get_stock_latest_quote.side_effect = Exception("rate limit")
            from core.alpaca import get_live_price
            result = get_live_price("AAPL")
        assert result is None


# ── submit_bracket_order ───────────────────────────────────────────────────────

_MARKET_OPEN   = patch("core.alpaca._is_market_open", return_value=True)
_MARKET_CLOSED = patch("core.alpaca._is_market_open", return_value=False)


def _mock_dclient(ticker: str, ask: float, last: float | None = None):
    """Return a patched _dclient reporting the given ask, and a last trade that
    corroborates it.

    `last` defaults to the ask, i.e. a healthy quote. Pass it explicitly to simulate
    the stale-quote condition from 2026-07-31, where the ask sat several percent above
    anything that had actually traded. Without a last trade the guard in
    _credible_ask cannot tell a good quote from a fictional one, and a bare MagicMock
    reports a last trade of $1.00, which would make every ask look implausible.
    """
    quote = MagicMock()
    quote.ask_price = str(ask)
    quote.bid_price = "0"
    trade = MagicMock()
    trade.price = str(ask if last is None else last)
    mc = MagicMock()
    mc.return_value.get_stock_latest_quote.return_value = {ticker: quote}
    mc.return_value.get_stock_latest_trade.return_value = {ticker: trade}
    return patch("core.alpaca._dclient", mc)


class TestSubmitOpeningOrder:
    def test_market_on_open_by_default(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.return_value = _mock_order(order_id="opg-1")
            from core.alpaca import submit_opening_order
            oid = submit_opening_order("AAPL", 10)
        assert oid == "opg-1"
        from alpaca.trading.enums import TimeInForce
        req = mc.return_value.submit_order.call_args[0][0]
        assert req.time_in_force == TimeInForce.OPG
        assert getattr(req, "limit_price", None) is None  # market-on-open, no limit

    def test_limit_on_open_when_price_given(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.return_value = _mock_order(order_id="opg-2")
            from core.alpaca import submit_opening_order
            oid = submit_opening_order("AAPL", 10, limit_price=185.257)
        assert oid == "opg-2"
        from alpaca.trading.enums import TimeInForce
        req = mc.return_value.submit_order.call_args[0][0]
        assert req.time_in_force == TimeInForce.OPG
        assert float(req.limit_price) == pytest.approx(185.26)  # rounded to 2dp

    def test_returns_none_on_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.side_effect = Exception("rejected")
            from core.alpaca import submit_opening_order
            assert submit_opening_order("AAPL", 10) is None

    def test_no_chase_or_staleness_gate(self):
        # opening orders never consult the day open — there is no gate to trip
        with patch("core.alpaca._client") as mc, patch("core.alpaca.get_day_open") as gdo:
            mc.return_value.submit_order.return_value = _mock_order(order_id="opg-3")
            from core.alpaca import submit_opening_order
            oid = submit_opening_order("AAPL", 10)
        assert oid == "opg-3"
        gdo.assert_not_called()


class TestSubmitBracketOrder:
    def test_returns_order_id_and_fill_price_on_fill(self):
        order = _mock_order(status="filled", filled_avg_price=185.20)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
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
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = submitted
            mc.return_value.get_order_by_id.return_value = rejected
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid is None
        assert fill is None

    def test_returns_order_id_with_none_fill_when_pending(self):
        pending = _mock_order(status="new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = pending
            mc.return_value.get_order_by_id.return_value = pending
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        assert fill is None

    def test_uses_ask_as_limit_price(self):
        """Limit price must be current ask, not the stale proposal price."""
        order = _mock_order(status="filled", filled_avg_price=184.90)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.limit_price == pytest.approx(184.90, rel=1e-3)

    def test_stop_and_target_reprojected_to_ask(self):
        """Stop/target must maintain the same % distance from ask, not from proposal price."""
        # proposal: entry=185, target=192 (+3.78%), stop=183 (-1.08%)
        # ask=184.90 → target should be 184.90*1.0378, stop=184.90*0.9892
        order = _mock_order(status="filled", filled_avg_price=184.90)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        req = mc.return_value.submit_order.call_args[0][0]
        expected_target = round(184.90 * (192.0 / 185.0), 2)
        expected_stop   = round(184.90 * (183.0 / 185.0), 2)
        assert req.take_profit.limit_price == pytest.approx(expected_target, rel=1e-3)
        assert req.stop_loss.stop_price    == pytest.approx(expected_stop,   rel=1e-3)

    def test_falls_back_to_proposal_price_when_ask_unavailable(self):
        """If ask fetch fails, use proposal price as-is (no buffer)."""
        order = _mock_order(status="filled", filled_avg_price=185.0)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, patch("core.alpaca._dclient", side_effect=Exception("no creds")):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.limit_price == pytest.approx(185.0, rel=1e-3)

    def test_premarket_skips_poll_and_returns_order_id(self):
        order = _mock_order(status="new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_CLOSED, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        assert fill is None
        mt.sleep.assert_not_called()

    def test_premarket_does_not_call_get_order_by_id(self):
        order = _mock_order(status="new")
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_CLOSED, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        mc.return_value.get_order_by_id.assert_not_called()

    def test_large_market_gap_uses_ask_limit(self):
        """Large gap between ask and proposal is a real market move — still use ask as limit."""
        order = _mock_order(status="filled", filled_avg_price=457.90)
        # entry=514.50, ask=457.90 → 11% gap → real market drop, limit at ask
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("TMO", 457.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("TMO", 5, 514.50, 555.66, 511.05)
        req = mc.return_value.submit_order.call_args[0][0]
        expected_limit = round(457.90 * 1.001, 2)  # 0.1% buffer applied to ask
        assert req.limit_price == pytest.approx(expected_limit, rel=1e-3)
        # stop/target reprojected at same percentages from ask (before buffer)
        stop_pct   = (514.50 - 511.05) / 514.50
        target_pct = (555.66 - 514.50) / 514.50
        assert req.stop_loss.stop_price    == pytest.approx(round(457.90 * (1 - stop_pct),   2), rel=1e-3)
        assert req.take_profit.limit_price == pytest.approx(round(457.90 * (1 + target_pct), 2), rel=1e-3)

    def test_tight_ask_uses_ask_not_proposal(self):
        """If ask is within 5% of entry_price, use ask as limit (normal market entry)."""
        order = _mock_order(status="filled", filled_avg_price=184.90)
        # entry=185.0, ask=184.90 → 0.05% gap → use ask
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        req = mc.return_value.submit_order.call_args[0][0]
        assert req.limit_price == pytest.approx(184.90, rel=1e-3)

    def test_fill_recognized_when_status_has_enum_prefix(self):
        """Alpaca SDK may return 'orderstatus.filled' — must still be treated as filled."""
        order = _mock_order(status="orderstatus.filled", filled_avg_price=185.20)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        assert fill == pytest.approx(185.20)

    def test_staleness_gate_skips_when_ask_4pct_above_proposal(self):
        """If ask is >4% above proposal entry, the stock ran since the research call — skip."""
        # entry_price=185.0, ask=193.0 → 4.32% above → stale → (None, None)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 193.0):
            mt.sleep = MagicMock()
            from core.alpaca import submit_bracket_order
            oid, fill = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid is None
        assert fill is None
        mc.return_value.submit_order.assert_not_called()

    def test_staleness_gate_allows_ask_within_threshold(self):
        """Ask 1.3% above entry is within the 1.5% threshold — should still submit."""
        # ask = 187.4 → (187.4 - 185.0) / 185.0 = 1.3% < 1.5% → not stale
        order = _mock_order(status="filled", filled_avg_price=187.4)
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 187.4):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            oid, _ = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        mc.return_value.submit_order.assert_called_once()

    def test_staleness_gate_does_not_trigger_on_downside(self):
        """Ask below proposal (stock dropped) is not stale — limit at ask is still valid."""
        order = _mock_order(status="filled", filled_avg_price=182.0)
        # entry=185.0, ask=182.0 → 1.62% BELOW → not stale, proceed with ask
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 182.0):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            oid, _ = submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        assert oid == "ord-001"
        mc.return_value.submit_order.assert_called_once()


# ── get_bracket_status ────────────────────────────────────────────────────────

class TestGetBracketStatus:
    def _leg(self, order_type, status, filled_avg_price):
        leg = MagicMock()
        leg.order_type       = order_type
        leg.status           = status
        leg.filled_avg_price = filled_avg_price
        return leg

    def test_entry_filled_true_when_order_filled(self):
        order = _mock_order(status="filled", filled_avg_price=185.10)
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["entry_filled"] is True
        assert result["entry_price"]  == pytest.approx(185.10)

    def test_entry_filled_false_when_pending(self):
        order = _mock_order(status="pending_new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["entry_filled"] is False
        assert result["entry_price"]  is None

    def test_exit_filled_on_target_leg(self):
        leg   = self._leg("limit", "filled", 192.40)
        order = _mock_order(status="filled", filled_avg_price=185.10, legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["exit_filled"] is True
        assert result["exit_reason"] == "TARGET"
        assert result["exit_price"]  == pytest.approx(192.40)

    def test_exit_reason_stop_on_stop_leg(self):
        leg   = self._leg("stop_limit", "filled", 183.78)
        order = _mock_order(status="filled", filled_avg_price=185.10, legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["exit_reason"] == "STOP"

    def test_no_exit_when_legs_unfilled(self):
        leg   = self._leg("limit", "new", None)
        order = _mock_order(status="filled", filled_avg_price=185.10, legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["exit_filled"] is False
        assert result["exit_price"]  is None

    def test_returns_error_on_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.side_effect = Exception("not found")
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert "error" in result


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

    def test_native_trail_standalone_order(self):
        order = MagicMock()
        order.order_type       = "trailing_stop"
        order.status           = "filled"
        order.filled_avg_price = 188.5
        order.legs             = []
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("trail-001")
        assert price  == pytest.approx(188.5)
        assert reason == "NATIVE_TRAIL"

    def test_native_trail_as_bracket_leg(self):
        leg = self._leg("trailing_stop", "filled", 187.0)
        order = _mock_order(legs=[leg])
        order.order_type = "limit"
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("ord-001")
        assert price  == pytest.approx(187.0)
        assert reason == "NATIVE_TRAIL"

    def test_returns_none_when_no_filled_leg(self):
        leg   = self._leg("limit", "new", None)
        order = _mock_order(legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_order_fill
            price, reason = get_order_fill("ord-001")
        assert price  is None
        assert reason is None


# ── submit_trailing_stop ───────────────────────────────────────────────────────

class TestSubmitTrailingStop:
    def test_returns_order_id_on_success(self):
        order = _mock_order(order_id="trail-001")
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.return_value = order
            from core.alpaca import submit_trailing_stop
            result = submit_trailing_stop("AAPL", 10, 0.008)
        assert result == "trail-001"

    def test_trail_percent_converted_correctly(self):
        order = _mock_order()
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.return_value = order
            from core.alpaca import submit_trailing_stop
            submit_trailing_stop("AAPL", 10, 0.008)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.trail_percent == pytest.approx(0.8, rel=1e-3)

    def test_returns_none_on_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.submit_order.side_effect = Exception("insufficient qty")
            from core.alpaca import submit_trailing_stop
            result = submit_trailing_stop("AAPL", 10, 0.008)
        assert result is None


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

    def test_polls_for_fill_when_not_immediately_filled(self):
        submitted = _mock_order(status="pending_new", filled_avg_price=None)
        filled    = _mock_order(status="filled",      filled_avg_price=187.5)
        with patch("core.alpaca._client") as mc, \
             patch("core.alpaca._is_market_open", return_value=True), \
             patch("core.alpaca.time.sleep"):
            mc.return_value.close_position.return_value      = submitted
            mc.return_value.get_order_by_id.return_value     = filled
            from core.alpaca import close_position
            ok, fill = close_position("AAPL")
        assert ok   is True
        assert fill == pytest.approx(187.5)

    def test_returns_none_fill_when_market_closed(self):
        submitted = _mock_order(status="pending_new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, \
             patch("core.alpaca._is_market_open", return_value=False):
            mc.return_value.close_position.return_value = submitted
            from core.alpaca import close_position
            ok, fill = close_position("AAPL")
        assert ok   is True
        assert fill is None

    def test_returns_none_fill_after_poll_timeout(self):
        submitted = _mock_order(status="pending_new", filled_avg_price=None)
        polled    = _mock_order(status="pending_new", filled_avg_price=None)
        with patch("core.alpaca._client") as mc, \
             patch("core.alpaca._is_market_open", return_value=True), \
             patch("core.alpaca.time.sleep"):
            mc.return_value.close_position.return_value  = submitted
            mc.return_value.get_order_by_id.return_value = polled
            from core.alpaca import close_position
            ok, fill = close_position("AAPL")
        assert ok   is True
        assert fill is None

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


# ── _cancel_bracket_stop_leg ──────────────────────────────────────────────────

class TestCancelBracketStopLeg:
    def _leg(self, order_type, status, leg_id="leg-stop-001"):
        leg = MagicMock()
        leg.order_type = order_type
        leg.status     = status
        leg.id         = leg_id
        return leg

    def test_cancels_open_stop_leg(self):
        stop_leg = self._leg("stop", "new")
        order    = _mock_order(legs=[stop_leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import _cancel_bracket_stop_leg
            _cancel_bracket_stop_leg("ord-001")
        mc.return_value.cancel_order_by_id.assert_called_once_with("leg-stop-001")

    def test_skips_already_cancelled_leg(self):
        stop_leg = self._leg("stop", "canceled")
        order    = _mock_order(legs=[stop_leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import _cancel_bracket_stop_leg
            _cancel_bracket_stop_leg("ord-001")
        mc.return_value.cancel_order_by_id.assert_not_called()

    def test_skips_filled_stop_leg(self):
        stop_leg = self._leg("stop", "filled")
        order    = _mock_order(legs=[stop_leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import _cancel_bracket_stop_leg
            _cancel_bracket_stop_leg("ord-001")
        mc.return_value.cancel_order_by_id.assert_not_called()

    def test_does_not_cancel_trailing_stop_leg(self):
        trail_leg = self._leg("trailing_stop", "new")
        order     = _mock_order(legs=[trail_leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import _cancel_bracket_stop_leg
            _cancel_bracket_stop_leg("ord-001")
        mc.return_value.cancel_order_by_id.assert_not_called()

    def test_swallows_exception(self):
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.side_effect = Exception("not found")
            from core.alpaca import _cancel_bracket_stop_leg
            _cancel_bracket_stop_leg("ord-001")  # must not raise


class TestSubmitBracketOrderCancelsStopLeg:
    def _stop_leg(self, status="new"):
        leg = MagicMock()
        leg.order_type = "stop"
        leg.status     = status
        leg.id         = "leg-stop-001"
        return leg

    def test_cancels_stop_leg_after_fill(self):
        """After bracket fills, the open stop leg must be cancelled so trail can be submitted."""
        stop_leg = self._stop_leg("new")
        order    = _mock_order(status="filled", filled_avg_price=185.0, legs=[stop_leg])
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        mc.return_value.cancel_order_by_id.assert_called_once_with("leg-stop-001")

    def test_skips_stop_leg_cancel_when_already_filled(self):
        """If stop leg already filled (stop-loss triggered before trail), don't cancel."""
        stop_leg = self._stop_leg("filled")
        order    = _mock_order(status="filled", filled_avg_price=185.0, legs=[stop_leg])
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             _MARKET_OPEN, _mock_dclient("AAPL", 184.90):
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import submit_bracket_order
            submit_bracket_order("AAPL", 10, 185.0, 192.0, 183.0)
        mc.return_value.cancel_order_by_id.assert_not_called()


class TestGetBracketStatusNativeTrail:
    def _leg(self, order_type, status, filled_avg_price):
        leg = MagicMock()
        leg.order_type       = order_type
        leg.status           = status
        leg.filled_avg_price = filled_avg_price
        return leg

    def test_trailing_stop_leg_classified_as_native_trail(self):
        leg   = self._leg("trailing_stop", "filled", 186.50)
        order = _mock_order(status="filled", filled_avg_price=185.10, legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["exit_reason"] == "NATIVE_TRAIL"
        assert result["exit_price"]  == pytest.approx(186.50)

    def test_stop_leg_still_classified_as_stop(self):
        leg   = self._leg("stop", "filled", 183.00)
        order = _mock_order(status="filled", filled_avg_price=185.10, legs=[leg])
        with patch("core.alpaca._client") as mc:
            mc.return_value.get_order_by_id.return_value = order
            from core.alpaca import get_bracket_status
            result = get_bracket_status("ord-001")
        assert result["exit_reason"] == "STOP"


# ── order prefix ──────────────────────────────────────────────────────────────

# ── batch_get_intraday_signals ─────────────────────────────────────────────────

class TestBatchGetIntradaySignals:
    def _make_bar(self, open_=100.0, high=105.0, low=99.0, close=103.0, volume=500_000, vwap=101.5):
        b = MagicMock()
        b.open   = open_
        b.high   = high
        b.low    = low
        b.close  = close
        b.volume = volume
        b.vwap   = vwap
        return b

    def _patch_dclient(self, ticker_bars: dict):
        mock_dc = MagicMock()
        mock_dc.return_value.get_stock_bars.return_value.data = ticker_bars
        return patch("core.alpaca._dclient", mock_dc)

    def test_uses_iex_feed(self):
        spy_bar    = self._make_bar(open_=500.0, close=505.0)
        ticker_bar = self._make_bar()
        mock_dc    = MagicMock()
        mock_dc.return_value.get_stock_bars.return_value.data = {
            "SPY": [spy_bar], "NVDA": [ticker_bar]
        }
        with patch("core.alpaca._dclient", mock_dc):
            from core.alpaca import batch_get_intraday_signals
            batch_get_intraday_signals(["NVDA"])
        req = mock_dc.return_value.get_stock_bars.call_args[0][0]
        assert getattr(req, "feed", None) == "iex"

    def test_returns_empty_dict_on_exception(self):
        with patch("core.alpaca._dclient") as mc:
            mc.return_value.get_stock_bars.side_effect = Exception("subscription error")
            from core.alpaca import batch_get_intraday_signals
            result = batch_get_intraday_signals(["NVDA"])
        assert result == {}

    def test_returns_available_false_when_no_bars(self):
        spy_bar = self._make_bar(open_=500.0, close=505.0)
        with self._patch_dclient({"SPY": [spy_bar], "NVDA": []}):
            from core.alpaca import batch_get_intraday_signals
            result = batch_get_intraday_signals(["NVDA"])
        assert result["NVDA"]["available"] is False

    def test_computes_vwap_above_vwap(self):
        spy_bar = self._make_bar(open_=500.0, close=505.0)
        bar     = self._make_bar(open_=100.0, close=103.0, vwap=101.0, volume=1_000_000)
        with self._patch_dclient({"SPY": [spy_bar], "AAPL": [bar]}):
            from core.alpaca import batch_get_intraday_signals
            result = batch_get_intraday_signals(["AAPL"])
        assert result["AAPL"]["available"]  is True
        assert result["AAPL"]["above_vwap"] is True   # close=103 > vwap=101

    def test_rs_vs_spy_capped_at_20(self):
        spy_bar    = self._make_bar(open_=500.0, close=500.0)   # SPY flat
        ticker_bar = self._make_bar(open_=100.0, close=130.0)   # ticker +30%
        with self._patch_dclient({"SPY": [spy_bar], "NVDA": [ticker_bar]}):
            from core.alpaca import batch_get_intraday_signals
            result = batch_get_intraday_signals(["NVDA"])
        assert result["NVDA"]["rs_vs_spy"] == 20.0  # capped


# ── order prefix ──────────────────────────────────────────────────────────────

class TestOrderPrefix:
    def test_client_order_id_starts_with_stratc(self):
        order = _mock_order(status="filled", filled_avg_price=185.0)
        # The staleness gate (a788e28, 31 Jul) skips any order whose ask is >4% above the proposal.
        # This test predates it and used the module default ask of $493 against a $420 proposal, so
        # the order was never submitted and call_args was None. Quote near the proposal so the gate
        # passes and the assertion can reach the order it is actually about.
        with patch("core.alpaca._client") as mc, patch("core.alpaca.time") as mt, \
             patch("core.alpaca._credible_ask", side_effect=lambda _t, ask: ask), \
             patch("core.alpaca._dclient") as mdc:
            mt.sleep = MagicMock()
            mc.return_value.submit_order.return_value = order
            mc.return_value.get_order_by_id.return_value = order
            mdc.return_value.get_stock_latest_quote.return_value = {
                "MSFT": SimpleNamespace(ask_price=420.5)
            }
            from core.alpaca import submit_bracket_order
            submit_bracket_order("MSFT", 5, 420.0, 435.0, 415.0)
        call_args = mc.return_value.submit_order.call_args[0][0]
        assert call_args.client_order_id.startswith("stratc_")


# ── _credible_ask ──────────────────────────────────────────────────────────────
# Regression, 2026-07-31. The free data plan returns the IEX top of book, not the
# NBBO. TGT's ask sat at $149.61 from 14:31 to 18:59 while the stock traded
# $143-145, so the 4% staleness gate skipped every risk-approved pick on both scans
# on both days and every run still reported success.
# See design/why-no-trades-2026-07-31.md.

class TestCredibleAsk:
    def test_ask_close_to_last_trade_is_kept(self):
        from core.alpaca import _credible_ask
        with patch("core.alpaca.get_last_trade", return_value=143.24):
            assert _credible_ask("TGT", 143.60) == 143.60

    def test_stale_ask_is_replaced_by_the_traded_price(self):
        from core.alpaca import _credible_ask
        # The actual numbers from the incident.
        with patch("core.alpaca.get_last_trade", return_value=143.24):
            assert _credible_ask("TGT", 149.61) == 143.24

    def test_ask_below_the_last_trade_is_never_raised(self):
        # The guard exists to stop the gate seeing a price that is too HIGH. A cheap
        # ask is a real opportunity and must pass through untouched.
        from core.alpaca import _credible_ask
        with patch("core.alpaca.get_last_trade", return_value=143.24):
            assert _credible_ask("TGT", 141.00) == 141.00

    def test_no_last_trade_leaves_the_ask_alone(self):
        # Never blind the caller: only replace a quote we can prove wrong.
        from core.alpaca import _credible_ask
        with patch("core.alpaca.get_last_trade", return_value=None):
            assert _credible_ask("TGT", 149.61) == 149.61

    def test_the_incident_would_no_longer_skip_the_order(self):
        # The end-to-end point: with the guard, the corroborated ask sits inside the
        # 4% staleness gate, so the order is submitted instead of silently dropped.
        from core.alpaca import _credible_ask
        proposal = 143.41
        with patch("core.alpaca.get_last_trade", return_value=143.24):
            assert _credible_ask("TGT", 149.61) <= proposal * 1.04
