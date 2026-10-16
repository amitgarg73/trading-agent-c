"""argus#1726: the orchestrator's entry summary declares the order calls it counted as its inputs.

Before this, 94 orchestrator steps in a week carried no input links, so a summary saying "2 entered" could not be tied to the
submit_bracket_order calls that did or did not fill, and Provy could only say the failure surfaced there.
"""
import inspect
import sessions.intraday as intraday


def test_trace_order_returns_the_span_id():
    calls = []

    class T:
        def log_tool_call(self, *a, **k):
            calls.append((a, k))
            return "abcd" * 4

    p = {"entry_price": 10.0, "target_price": 11.0, "stop_loss": 9.0}
    assert intraday._trace_order(T(), "NOW", p, 5, "oid", 10.0) == "abcd" * 4
    assert intraday._trace_order(None, "NOW", p, 5, "oid", 10.0) is None


def test_trace_order_never_raises_and_returns_none_on_a_telemetry_fault():
    class T:
        def log_tool_call(self, *a, **k):
            raise RuntimeError("exporter down")

    p = {"entry_price": 10.0, "target_price": 11.0, "stop_loss": 9.0}
    assert intraday._trace_order(T(), "NOW", p, 5, None, None) is None


def test_the_summary_steps_declare_inputs():
    src = inspect.getsource(intraday.main)
    assert "order_spans=order_spans" in src
    assert 'tracer.log_decision("orchestrator", outcome, detail={"count": count}, inputs=_inputs)' in src
    assert "outcome, inputs=_inputs)" in src
