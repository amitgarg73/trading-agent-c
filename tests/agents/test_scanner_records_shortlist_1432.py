"""argus#1432: the scanner's computed scores are recorded as structured values beside its answer, so Provy can check them."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.scanner_agent import _llm_select


def test_the_shortlist_it_was_given_is_recorded():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"selected": ["AAA"], "scan_rationale": "r", "signals_used": []}')],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1), model="m")
    tracer = MagicMock()
    ranked = [{"ticker": "AAA", "technical_score": 9, "premarket_change_pct": 1.5, "sector": "Tech"},
              {"ticker": "BBB", "technical_score": 6, "premarket_change_pct": -0.2, "sector": "Health"}]
    _llm_select(client, tracer, ranked, [], {"decision": "GO"}, "normal", 2)
    payload = tracer.log_agent_message.call_args.kwargs["payload"]
    assert payload["regime"] == "normal" and payload["max_n"] == 2
    assert payload["shortlist"][0] == {"ticker": "AAA", "technical_score": 9, "premarket_change_pct": 1.5, "sector": "Tech"}
    assert [c["ticker"] for c in payload["shortlist"]] == ["AAA", "BBB"]
