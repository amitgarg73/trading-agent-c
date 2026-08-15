"""
argus#583: the tool loop must not spin on a stop reason it cannot act on.

⛔ WHAT THIS COST. The loop handled `end_turn` and `tool_use` and fell through on everything else,
appending nothing to `messages`. The next iteration therefore sent a byte-identical request and got a
byte-identical response, up to max_turns, then died with "hit the 20-turn limit without end_turn" —
a description of the symptom that hides the cause.

The Learning Agent failed that way on every EOD run from 10 Aug 2026: four reads, then sixteen
no-progress turns, twenty Sonnet calls a night, and on 12 Aug it tripped the account's API usage
limit. The evidence was in the traces the whole time and the error message pointed at the loop.
"""
import types

import pytest

from agents import base


class _Usage:
    input_tokens = 10
    output_tokens = 20


class _Resp:
    def __init__(self, stop_reason, content=None):
        self.stop_reason = stop_reason
        self.content = content or []
        self.usage = _Usage()


class _Messages:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    def create(self, **_kw):
        self.calls += 1
        return self._resp


class _Client:
    def __init__(self, resp):
        self.messages = _Messages(resp)


class _Tracer:
    def __init__(self):
        self.session_id = "s1"
    def log_tokens(self, *a, **k): pass
    def log_agent_message(self, *a, **k): pass
    def log_tool_call(self, *a, **k): pass


def _run(resp, max_turns=20):
    client = _Client(resp)
    return client, base.run_tool_loop(
        client=client, model="m", system="s", tools=[], initial_message="x",
        dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=max_turns,
    )


def test_it_fails_on_the_first_unactionable_turn_not_the_twentieth():
    """Nineteen identical calls buy nothing. The first one already told us."""
    client = _Client(_Resp("max_tokens"))
    with pytest.raises(RuntimeError) as e:
        base.run_tool_loop(
            client=client, model="m", system="s", tools=[], initial_message="x",
            dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=20,
        )
    assert client.messages.calls == 1, "must not re-send an identical request"
    assert "turn 1 of 20" in str(e.value)


def test_the_error_names_the_stop_reason():
    """'hit the 20-turn limit' described the symptom. The cause is the stop reason."""
    client = _Client(_Resp("max_tokens"))
    with pytest.raises(RuntimeError) as e:
        base.run_tool_loop(
            client=client, model="m", system="s", tools=[], initial_message="x",
            dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=20,
        )
    msg = str(e.value)
    assert "max_tokens" in msg
    assert "raise max_tokens or ask for a shorter response" in msg, (
        "for a truncated answer the fix is the budget, not the tools, and the message should say so"
    )


def test_an_unknown_stop_reason_still_fails_fast_without_a_hint():
    client = _Client(_Resp("refusal"))
    with pytest.raises(RuntimeError) as e:
        base.run_tool_loop(
            client=client, model="m", system="s", tools=[], initial_message="x",
            dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=20,
        )
    assert client.messages.calls == 1
    assert "'refusal'" in str(e.value)
    assert "max_tokens" not in str(e.value), "do not offer a fix that does not apply"


def test_end_turn_still_returns_the_text():
    block = types.SimpleNamespace(text="done", type="text")
    client = _Client(_Resp("end_turn", [block]))
    out = base.run_tool_loop(
        client=client, model="m", system="s", tools=[], initial_message="x",
        dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=20,
    )
    assert out == "done"
