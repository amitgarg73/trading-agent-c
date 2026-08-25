"""A span must end when its agent finishes, and a sub-agent must nest (#668).

⛔ BOTH DEFECTS SHIPPED AND BOTH WERE INVISIBLE TO THE SUITE. Measured on production:
`agent:risk` reported 377s for 9.5s of work because every span was swept at close_session, and all
24 `research_*` spans in one session sat beside `agent:research` rather than under it because the
parent was looked up by exact name.
"""

from trace.logger import TraceLogger


class _FakeSpan:
    def __init__(self, name):
        self.name = name
        self.ended = 0

    def end(self):
        self.ended += 1

    def get_span_context(self):
        return None


def _tracer():
    t = TraceLogger.__new__(TraceLogger)
    t._agent_otel_spans = {}
    return t


class TestVariantRule:
    def test_a_ticker_suffix_folds_into_its_parent(self):
        assert _tracer()._base("research_GILD") == "research"

    def test_a_real_second_agent_is_not_folded(self):
        # market_shadow has 104 spans of its own on the reference fleet. split("_")[0] would
        # merge it into market, which is the reason that shortcut is not used here.
        t = _tracer()
        assert t._base("market_shadow") == "market_shadow"
        assert t._base("insights_agent") == "insights_agent"

    def test_a_plain_name_is_untouched(self):
        assert _tracer()._base("risk") == "risk"

    def test_a_bare_uppercase_name_is_not_emptied(self):
        assert _tracer()._base("RISK") == "RISK"


class TestEndAgentSpan:
    def test_ends_the_span_for_a_plain_agent(self):
        t = _tracer()
        span = _FakeSpan("agent:risk")
        t._agent_otel_spans["risk"] = span
        t.end_agent_span("risk")
        assert span.ended == 1

    def test_a_variant_ends_its_parents_span(self):
        # research_GILD finishing is research finishing; there is no research_GILD span.
        t = _tracer()
        span = _FakeSpan("agent:research")
        t._agent_otel_spans["research"] = span
        t.end_agent_span("research_GILD")
        assert span.ended == 1

    def test_the_span_is_still_findable_after_ending(self):
        # Four existing tests assert a span EXISTS for the agent after the run. That is a fair
        # thing to assert and stays true once it has ended, so ending must not remove it.
        t = _tracer()
        t._agent_otel_spans["risk"] = _FakeSpan("agent:risk")
        t.end_agent_span("risk")
        assert "risk" in t._agent_otel_spans

    def test_ending_an_agent_that_never_started_is_a_no_op(self):
        _tracer().end_agent_span("never_ran")   # must not raise

    def test_a_span_that_raises_on_end_does_not_break_the_run(self):
        class Angry(_FakeSpan):
            def end(self):
                raise RuntimeError("no")

        t = _tracer()
        t._agent_otel_spans["risk"] = Angry("agent:risk")
        t.end_agent_span("risk")   # must not raise: a tracer never fails a trading run
