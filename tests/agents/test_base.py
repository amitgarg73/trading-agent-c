from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agents.base import _dispatch_with_timeout, parse_json_response, run_tool_loop
from tests.conftest import make_api_response, text_block


# ── _dispatch_with_timeout ─────────────────────────────────────────────────────

class TestDispatchWithTimeout:
    def test_returns_result_when_fast(self):
        result = _dispatch_with_timeout(lambda n, i: {"ok": True}, "t", {})
        assert result == {"ok": True}

    def test_returns_error_dict_on_timeout(self):
        def slow(name, inp):
            time.sleep(60)

        with __import__("unittest.mock", fromlist=["patch"]).patch("agents.base._TOOL_TIMEOUT_S", 0.05):
            result = _dispatch_with_timeout(slow, "t", {})
        assert "error" in result
        assert "timeout" in result["error"]


# ── run_tool_loop wall-clock timeout ───────────────────────────────────────────

class TestRunToolLoopWallClock:
    def test_raises_on_wall_clock_breach(self, tracer):
        client = MagicMock()
        client.messages.create.return_value = make_api_response(
            "end_turn", [text_block('{"proposals":[]}')]
        )
        with pytest.raises(RuntimeError, match="wall-clock timeout"):
            run_tool_loop(
                client=client,
                model="claude-haiku-4-5-20251001",
                system="",
                tools=[],
                initial_message="go",
                dispatch=lambda n, i: {},
                tracer=tracer,
                agent_name="test",
                max_turns=10,
                wall_clock_timeout_s=0,  # fires before first turn
            )

    def test_completes_normally_within_timeout(self, tracer):
        client = MagicMock()
        client.messages.create.return_value = make_api_response(
            "end_turn", [text_block('{"proposals":[]}')]
        )
        result = run_tool_loop(
            client=client,
            model="claude-haiku-4-5-20251001",
            system="",
            tools=[],
            initial_message="go",
            dispatch=lambda n, i: {},
            tracer=tracer,
            agent_name="test",
            max_turns=5,
            wall_clock_timeout_s=30,
        )
        assert '{"proposals":[]}' in result

    def test_no_timeout_when_param_is_none(self, tracer):
        client = MagicMock()
        client.messages.create.return_value = make_api_response(
            "end_turn", [text_block('{"ok":true}')]
        )
        result = run_tool_loop(
            client=client,
            model="claude-haiku-4-5-20251001",
            system="",
            tools=[],
            initial_message="go",
            dispatch=lambda n, i: {},
            tracer=tracer,
            agent_name="test",
            wall_clock_timeout_s=None,
        )
        assert "ok" in result

    def test_timeout_fires_mid_loop_after_first_turn(self, tracer):
        """Timeout set to fire after turn 1 completes but before turn 2 starts."""
        responses = [
            make_api_response("end_turn", [text_block('{"proposals":[]}')]),
            make_api_response("end_turn", [text_block('{"proposals":[]}')]),
        ]
        client = MagicMock()
        call_count = 0

        def slow_create(**kwargs):
            nonlocal call_count
            call_count += 1
            time.sleep(0.15)
            return responses[0]

        client.messages.create.side_effect = slow_create

        with pytest.raises(RuntimeError, match="wall-clock timeout"):
            run_tool_loop(
                client=client,
                model="claude-haiku-4-5-20251001",
                system="",
                tools=[],
                initial_message="go",
                dispatch=lambda n, i: {},
                tracer=tracer,
                agent_name="test",
                max_turns=5,
                wall_clock_timeout_s=0,  # always fires at turn boundary
            )


# ── parse_json_response ────────────────────────────────────────────────────────

class TestParseJsonResponse:
    def test_plain_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_json_in_code_fence(self):
        text = '```json\n{"a": 1}\n```'
        assert parse_json_response(text) == {"a": 1}

    def test_json_in_plain_fence(self):
        text = '```\n{"a": 1}\n```'
        assert parse_json_response(text) == {"a": 1}

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError):
            parse_json_response("no json here")
