from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.risk_agent import run_risk_agent
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block, tool_block

_PROPOSALS = {
    "proposals": [
        {"ticker": "AAPL", "entry_price": 185.0, "target_price": 192.4,
         "stop_loss": 183.78, "position_size": 3500, "confidence": "HIGH",
         "evidence": ["above_vwap"]},
    ],
    "skipped": [],
    "summary": "One proposal.",
}

_VERDICTS = {
    "verdicts": [{"ticker": "AAPL", "verdict": "APPROVED", "reason": "all constraints passed"}],
    "portfolio_state": {
        "buying_power": 45000.0, "positions_open": 0,
        "today_pnl": 0.0, "limit_hit": False,
    },
}

_OPEN_POSITIONS  = []
_TODAY_PNL       = {"realized_pnl": 0.0, "trades_closed": 0, "loss_limit": -500.0, "limit_hit": False}
_BUYING_POWER    = {"buying_power": 50000.0, "total_capital": 50000.0, "deployed": 0.0}
_EXPOSURE        = {"positions_open": 0, "total_deployed": 0.0, "by_sector": {}, "max_sector_pct": 0.0}


def _setup_client():
    tool_calls = [
        tool_block("get_open_positions",     {}, "t1"),
        tool_block("get_today_pnl",          {}, "t2"),
        tool_block("get_buying_power",       {}, "t3"),
        tool_block("get_portfolio_exposure", {}, "t4"),
    ]
    client = MagicMock()
    client.messages.create.side_effect = [
        make_api_response("tool_use", tool_calls),
        make_api_response("end_turn", [text_block(json.dumps(_VERDICTS))]),
    ]
    return client


def _run(tracer, mock_client=None):
    client = mock_client or _setup_client()
    with patch("agents.risk_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.risk_agent.get_open_positions",     return_value=_OPEN_POSITIONS), \
         patch("agents.risk_agent.get_today_pnl",          return_value=_TODAY_PNL), \
         patch("agents.risk_agent.get_buying_power",       return_value=_BUYING_POWER), \
         patch("agents.risk_agent.get_portfolio_exposure", return_value=_EXPOSURE):
        return run_risk_agent(tracer, _PROPOSALS, StrategyParams())


class TestSystemPromptSpecificity:
    """Guards the fix for the risk_verdict_specificity eval (was scoring ~0):
    the verdict reason must cite concrete constraint values, not 'all constraints passed'."""

    def test_prompt_requires_concrete_values_and_forbids_generic_reason(self):
        from agents.risk_agent import _SYSTEM
        low = _SYSTEM.lower()
        assert "concrete value" in low
        assert "all constraints passed" in low  # named explicitly as the thing to avoid
        assert "never use a generic reason" in low
        # gives guidance for BOTH decisions, not just rejections
        assert "approved:" in low and "rejected:" in low


class TestRunRiskAgent:
    def test_returns_verdicts_dict(self, tracer):
        result = _run(tracer)
        assert "verdicts" in result
        assert result["verdicts"][0]["ticker"] == "AAPL"
        assert result["verdicts"][0]["verdict"] == "APPROVED"

    def test_portfolio_state_in_result(self, tracer):
        result = _run(tracer)
        assert "portfolio_state" in result
        assert result["portfolio_state"]["limit_hit"] is False

    def test_dispatches_all_four_tools(self, tracer):
        with patch("agents.risk_agent.anthropic.Anthropic") as mock_cls, \
             patch("agents.risk_agent.get_open_positions",     return_value=_OPEN_POSITIONS) as m1, \
             patch("agents.risk_agent.get_today_pnl",          return_value=_TODAY_PNL)       as m2, \
             patch("agents.risk_agent.get_buying_power",       return_value=_BUYING_POWER)    as m3, \
             patch("agents.risk_agent.get_portfolio_exposure", return_value=_EXPOSURE)        as m4:
            mock_cls.return_value = _setup_client()
            run_risk_agent(tracer, _PROPOSALS, StrategyParams())
        m1.assert_called_once()
        m2.assert_called_once()
        m3.assert_called_once()
        m4.assert_called_once()

    def test_span_started(self, tracer):
        _run(tracer)
        assert tracer.get_agent_span("risk") is not None

    def test_proposals_json_in_user_message(self, tracer):
        client = _setup_client()
        with patch("agents.risk_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.risk_agent.get_open_positions",     return_value=_OPEN_POSITIONS), \
             patch("agents.risk_agent.get_today_pnl",          return_value=_TODAY_PNL), \
             patch("agents.risk_agent.get_buying_power",       return_value=_BUYING_POWER), \
             patch("agents.risk_agent.get_portfolio_exposure", return_value=_EXPOSURE):
            run_risk_agent(tracer, _PROPOSALS, StrategyParams())
        # Verify first API call includes AAPL in the user message
        first_call_args = client.messages.create.call_args_list[0]
        messages = first_call_args.kwargs.get("messages") or first_call_args[1].get("messages", [])
        assert "AAPL" in str(messages)

    def test_rejected_verdicts_returned(self, tracer):
        rejected = {
            **_VERDICTS,
            "verdicts": [{"ticker": "AAPL", "verdict": "REJECTED", "reason": "daily loss limit hit"}],
        }
        client = MagicMock()
        client.messages.create.side_effect = [
            make_api_response("tool_use", [
                tool_block("get_open_positions", {}, "t1"),
                tool_block("get_today_pnl", {}, "t2"),
                tool_block("get_buying_power", {}, "t3"),
                tool_block("get_portfolio_exposure", {}, "t4"),
            ]),
            make_api_response("end_turn", [text_block(json.dumps(rejected))]),
        ]
        with patch("agents.risk_agent.anthropic.Anthropic", return_value=client), \
             patch("agents.risk_agent.get_open_positions",     return_value=_OPEN_POSITIONS), \
             patch("agents.risk_agent.get_today_pnl",          return_value=_TODAY_PNL), \
             patch("agents.risk_agent.get_buying_power",       return_value=_BUYING_POWER), \
             patch("agents.risk_agent.get_portfolio_exposure", return_value=_EXPOSURE):
            result = run_risk_agent(tracer, _PROPOSALS, StrategyParams())
        assert result["verdicts"][0]["verdict"] == "REJECTED"
