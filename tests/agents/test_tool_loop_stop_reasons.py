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


def test_max_tokens_never_resends_the_same_request_and_stops_at_the_ceiling():
    """Nineteen identical calls buy nothing. A truncated answer is retried with a BIGGER budget
    (argus#865), and once the ceiling itself truncates the loop fails on the next turn, not the 20th."""
    budgets = []

    class _Recording(_Messages):
        def create(self, **kw):
            budgets.append(kw["max_tokens"])
            return super().create(**kw)

    client = _Client(_Resp("max_tokens"))
    client.messages = _Recording(_Resp("max_tokens"))
    with pytest.raises(RuntimeError) as e:
        base.run_tool_loop(
            client=client, model="m", system="s", tools=[], initial_message="x",
            dispatch=lambda name, inp: {}, tracer=_Tracer(), agent_name="learner", max_turns=20,
            max_tokens=2048, max_tokens_ceiling=8192,
        )
    assert budgets == [2048, 4096, 8192], "each retry must double the budget, never repeat it"
    assert "turn 3 of 20" in str(e.value)
    assert "8192-token ceiling" in str(e.value)


def test_a_truncated_turn_is_retried_and_the_loop_finishes():
    """The learner's failure shape: turn 1 reads, turn 2 truncates while writing. It must continue."""
    tool = types.SimpleNamespace(type="tool_use", id="t1", name="write_learning", input={"finding": "x"})
    done = types.SimpleNamespace(type="text", text='{"ok": true}')
    responses = [_Resp("max_tokens", [tool]), _Resp("tool_use", [tool]), _Resp("end_turn", [done])]
    budgets, dispatched = [], []

    class _Seq:
        def create(self, **kw):
            budgets.append(kw["max_tokens"])
            # The truncated response must not have been appended to the history.
            assert all(m["role"] != "assistant" for m in kw["messages"][:1])
            return responses.pop(0)

    client = types.SimpleNamespace(messages=_Seq())
    out = base.run_tool_loop(
        client=client, model="m", system="s", tools=[], initial_message="x",
        dispatch=lambda name, inp: dispatched.append(name) or {"status": "written"},
        tracer=_Tracer(), agent_name="learner", max_turns=20, max_tokens=2048,
    )
    assert out == '{"ok": true}'
    assert budgets == [2048, 4096, 4096]
    # The half-formed tool call from the truncated turn was NOT executed; only the complete one was.
    assert dispatched == ["write_learning"]


def test_the_retry_is_traced_when_the_tracer_can_record_it():
    seen = []

    class _T(_Tracer):
        def log_decision(self, agent, outcome, detail=None, **k):
            seen.append((agent, outcome, detail))

    done = types.SimpleNamespace(type="text", text="ok")
    responses = [_Resp("max_tokens"), _Resp("end_turn", [done])]
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: responses.pop(0)))
    base.run_tool_loop(
        client=client, model="m", system="s", tools=[], initial_message="x",
        dispatch=lambda name, inp: {}, tracer=_T(), agent_name="learner", max_turns=5,
    )
    assert seen == [("learner", "max_tokens_retry", {"turn": 1, "from": 2048, "to": 4096})]


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
