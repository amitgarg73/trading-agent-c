"""
6 Oct 2026: the EOD Learning Agent died with "Object of type date is not JSON serializable".

⛔ THE CAUSE. agents/tools/learning_tools.py adjust_param() returns AdjustResult.cooldown_until, a
`datetime.date` (core/params.py builds it from date.today() + cooldown_days, or date.fromisoformat).
run_tool_loop then did json.dumps(result) with no `default`, so the first night the model actually
called adjust_param (applied, or rejected with cooldown_active) the loop raised, AFTER the tool had
already run. The line is from June; no model call had reached it with a date until then. Neither the
5 Oct settle_day() change nor the context manifest touched this path.
"""
import json
from datetime import date

import pytest

from agents import base
from agents.tools.learning_tools import adjust_param
from tests.agents.test_tool_loop_stop_reasons import _Client, _Resp, _Tracer
from tests.conftest import make_query


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        self.__dict__.update(kw)


class _Messages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw["messages"], default=lambda o: getattr(o, "__dict__", str(o)))))
        if len(self.calls) == 1:
            return _Resp("tool_use", [_Block("tool_use", id="t1", name="adjust_param", input={})])
        return _Resp("end_turn", [_Block("text", text="{}")])


def test_tool_result_with_a_date_goes_through_the_loop_as_iso_text():
    client = _Client(None)
    client.messages = _Messages()
    base.run_tool_loop(
        client=client, model="m", system="s", tools=[], initial_message="x",
        dispatch=lambda name, inp: {"status": "applied", "cooldown_until": date(2026, 10, 9)},
        tracer=_Tracer(), agent_name="learner", max_turns=5,
    )
    sent = client.messages.calls[1][-1]["content"][0]["content"]
    assert json.loads(sent) == {"status": "applied", "cooldown_until": "2026-10-09"}


@pytest.mark.parametrize("cooldown", [None, "2026-12-31"])
def test_adjust_param_return_is_json_safe(mock_supabase, cooldown):
    mock_supabase.table.return_value = make_query([
        {"param_key": "strategy_min_score", "param_value": 4, "min_bound": 3, "max_bound": 9,
         "cooldown_until": cooldown, "default_value": 5, "cooldown_days": 3, "previous_value": None}
    ])
    applied_or_cooling = adjust_param("strategy_min_score", 5.0, "r")
    assert applied_or_cooling["status"] in ("applied", "rejected")
    json.dumps(applied_or_cooling)  # no default=: the tool's own return must already be JSON
    assert isinstance(applied_or_cooling["cooldown_until"], (str, type(None)))
