from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.market_agent_v1 import check_circuit_breakers
from agents.market_agent import run_market_agent
from core.params import StrategyParams
from tests.conftest import make_api_response, make_query, text_block, tool_block


# ── check_circuit_breakers ─────────────────────────────────────────────────────

class TestCheckCircuitBreakers:
    def _vix(self, value):
        return {"value": value}

    def _futures(self, avg, sp=0.0, nq=0.0, dw=0.0):
        return {
            "avg_change_pct": avg,
            "S&P500": {"change_pct": sp},
            "Nasdaq":  {"change_pct": nq},
            "Dow":     {"change_pct": dw},
        }

    def test_no_breaker_on_normal_day(self):
        triggered, reason = check_circuit_breakers(self._vix(18), self._futures(0.3))
        assert triggered is False
        assert reason is None

    def test_vix_extreme_triggers(self):
        triggered, reason = check_circuit_breakers(self._vix(36), self._futures(0.1))
        assert triggered is True
        assert "vix_extreme" in reason

    def test_vix_at_35_does_not_trigger(self):
        triggered, _ = check_circuit_breakers(self._vix(35), self._futures(0.1))
        assert triggered is False

    def test_futures_crash_triggers(self):
        triggered, reason = check_circuit_breakers(self._vix(22), self._futures(-2.1))
        assert triggered is True
        assert "futures_crash" in reason

    def test_futures_at_minus_2_does_not_trigger(self):
        triggered, _ = check_circuit_breakers(self._vix(22), self._futures(-2.0))
        assert triggered is False

    def test_coordinated_selloff_triggers(self):
        triggered, reason = check_circuit_breakers(
            self._vix(28),
            self._futures(-1.5, sp=-1.2, nq=-1.5, dw=-1.1),
        )
        assert triggered is True
        assert "coordinated_selloff" in reason

    def test_coordinated_selloff_needs_all_three(self):
        triggered, _ = check_circuit_breakers(
            self._vix(28),
            self._futures(-0.8, sp=-1.2, nq=-1.5, dw=-0.5),
        )
        assert triggered is False

    def test_vix_checked_before_futures(self):
        triggered, reason = check_circuit_breakers(self._vix(40), self._futures(-3.0))
        assert triggered is True
        assert "vix_extreme" in reason


# ── run_market_agent ───────────────────────────────────────────────────────────

_GO_REPORT = {
    "decision": "GO", "max_positions": 10, "bias": "BULLISH",
    "skip_reason": None, "confidence": "HIGH",
    "key_factors": ["VIX at 17 — low volatility", "Strong futures", "No high-impact events"],
    "summary": "Clean setup.",
}

_NORMAL_VIX     = {"value": 17.0, "level": "LOW"}
_NORMAL_FUTURES = {
    "avg_change_pct": 0.4,
    "bias": "BULLISH",
    "S&P500": {"change_pct": 0.4},
    "Nasdaq":  {"change_pct": 0.5},
    "Dow":     {"change_pct": 0.3},
}

_CB_VIX     = {"value": 38.0, "level": "EXTREME"}
_CB_FUTURES = {
    "avg_change_pct": -2.5,
    "bias": "BEARISH",
    "S&P500": {"change_pct": -2.5},
    "Nasdaq":  {"change_pct": -2.6},
    "Dow":     {"change_pct": -2.4},
}


class TestRunMarketAgent:
    def _params(self):
        return StrategyParams()

    def _tracer(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        from trace.logger import TraceLogger
        return TraceLogger("test-market-session")

    def test_returns_skip_when_circuit_breaker_fires(self, mock_supabase):
        tracer = self._tracer(mock_supabase)
        mock_supabase.table.return_value = make_query([])
        with patch("agents.market_agent.get_vix",     return_value=_CB_VIX), \
             patch("agents.market_agent.get_futures", return_value=_CB_FUTURES):
            result = run_market_agent(tracer, self._params())
        assert result["decision"] == "SKIP"
        assert result["circuit_breaker"] is not None
        assert "vix_extreme" in result["circuit_breaker"]

    def test_cb_skip_does_not_call_claude(self, mock_supabase):
        tracer = self._tracer(mock_supabase)
        mock_supabase.table.return_value = make_query([])
        with patch("agents.market_agent.get_vix",     return_value=_CB_VIX), \
             patch("agents.market_agent.get_futures", return_value=_CB_FUTURES), \
             patch("anthropic.Anthropic") as mock_client:
            run_market_agent(tracer, self._params())
        mock_client.assert_not_called()

    def test_calls_claude_when_no_circuit_breaker(self, mock_supabase):
        import json
        tracer = self._tracer(mock_supabase)
        mock_supabase.table.return_value = make_query([])

        tool_calls = [
            tool_block("get_vix",                {}, "t1"),
            tool_block("get_futures",             {}, "t2"),
            tool_block("get_fear_greed",          {}, "t3"),
            tool_block("get_sector_rotation",     {}, "t4"),
            tool_block("get_economic_calendar",   {}, "t5"),
            tool_block("get_treasury_yields",     {}, "t6"),
        ]
        final = make_api_response("end_turn", [text_block(json.dumps(_GO_REPORT))])
        tool_resp = make_api_response("tool_use", tool_calls)

        with patch("agents.market_agent.get_vix",     return_value=_NORMAL_VIX), \
             patch("agents.market_agent.get_futures", return_value=_NORMAL_FUTURES), \
             patch("anthropic.Anthropic") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.side_effect = [tool_resp, final]
            with patch("agents.market_agent.get_fear_greed",        return_value={"value": 60}), \
                 patch("agents.market_agent.get_sector_rotation",   return_value=[]), \
                 patch("agents.market_agent.get_economic_calendar", return_value={"has_high_impact_events": False}), \
                 patch("agents.market_agent.get_treasury_yields",   return_value={"yield_10y": 4.5, "change_bp": 3, "direction": "flat"}):
                result = run_market_agent(tracer, self._params())

        assert result["decision"] == "GO"
        assert result["confidence"] == "HIGH"
        assert len(result["key_factors"]) == 3

    def test_result_has_circuit_breaker_none_on_normal_run(self, mock_supabase):
        import json
        tracer = self._tracer(mock_supabase)
        mock_supabase.table.return_value = make_query([])

        tool_calls = [tool_block(t, {}, f"t{i}") for i, t in enumerate(
            ["get_vix", "get_futures", "get_fear_greed",
             "get_sector_rotation", "get_economic_calendar", "get_treasury_yields"]
        )]
        final = make_api_response("end_turn", [text_block(json.dumps(_GO_REPORT))])
        tool_resp = make_api_response("tool_use", tool_calls)

        with patch("agents.market_agent.get_vix",     return_value=_NORMAL_VIX), \
             patch("agents.market_agent.get_futures", return_value=_NORMAL_FUTURES), \
             patch("anthropic.Anthropic") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.side_effect = [tool_resp, final]
            with patch("agents.market_agent.get_fear_greed",        return_value={}), \
                 patch("agents.market_agent.get_sector_rotation",   return_value=[]), \
                 patch("agents.market_agent.get_economic_calendar", return_value={}), \
                 patch("agents.market_agent.get_treasury_yields",   return_value={}):
                result = run_market_agent(tracer, self._params())

        assert result.get("circuit_breaker") is None
