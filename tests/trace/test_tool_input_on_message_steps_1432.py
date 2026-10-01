"""argus#1432 / argus#1478: a step records the values it was GIVEN as `tool_input`, and a cap never leaves unparseable JSON.

The scanner computes its shortlist in code and shows it to the model only inside the prompt. Provy's quality judge read the scanner's
tool results (price frames downloaded), saw no scores, and convicted a code-only step of fabricating them (1 Oct 2026, session
00e8f271). The shortlist is now recorded as the step's `tool_input`, which the judge shows as "what this agent was given". What the
step PRODUCED stays in its reply and its payload keys: a result written down as an input would ground itself.

The 4,000-character cap that used to clip `argus.tool_input` cut a JSON string mid-token, which Provy can only keep as an unreadable
string. For message and decision steps the logger now shortens a structured input by dropping trailing list items and says how many.
"""
import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from trace.logger import _bounded_input, GIVEN_INPUT_MAX_CHARS
from trace.otel_exporter import ArgusExporter


def _attrs(span):
    return dict(span.attributes or {})


def _message_spans(recorder):
    return [s for s in recorder.spans if _attrs(s).get("argus.step_type") in ("agent_message", "decision")]


SHORTLIST = [{"ticker": f"T{i:02d}", "technical_score": 10 - i % 5, "premarket_change_pct": round(i / 10, 2), "sector": "Tech"}
             for i in range(15)]


# ── emission ──────────────────────────────────────────────────────────────────

class TestEmission:
    def test_log_agent_message_emits_tool_input_as_a_json_attribute(self, tracer, mock_argus_exporter):
        tracer.log_agent_message("scanner", "picked", "completed",
                                 tool_input={"regime": "normal", "max_n": 5, "shortlist": SHORTLIST[:2]})
        a = _attrs(_message_spans(mock_argus_exporter)[-1])
        assert json.loads(a["argus.tool_input"]) == {"regime": "normal", "max_n": 5, "shortlist": SHORTLIST[:2]}

    def test_log_decision_takes_tool_input_too(self, tracer, mock_argus_exporter):
        tracer.log_decision("risk", "approved", detail={"approved": True}, tool_input={"limit": 3})
        a = _attrs(_message_spans(mock_argus_exporter)[-1])
        assert json.loads(a["argus.tool_input"]) == {"limit": 3}
        # what the decision PRODUCED is untouched and stays on the output side
        assert json.loads(a["argus.tool_output"]) == {"approved": True}

    def test_omitting_it_changes_nothing(self, tracer, mock_argus_exporter):
        tracer.log_agent_message("scanner", "picked", "completed")
        tracer.log_decision("risk", "approved")
        for s in _message_spans(mock_argus_exporter):
            assert "argus.tool_input" not in _attrs(s)

    def test_the_payload_keys_are_still_emitted(self, tracer, mock_argus_exporter):
        # Whatever reads argus.payload.<key> today keeps working: tool_input is added beside it, not instead of it.
        tracer.log_agent_message("scanner", "picked", "completed", payload={"regime": "normal", "shortlist": SHORTLIST[:1]},
                                 tool_input={"regime": "normal", "shortlist": SHORTLIST[:1]})
        a = _attrs(_message_spans(mock_argus_exporter)[-1])
        assert a["argus.payload.regime"] == "normal"
        assert json.loads(a["argus.payload.shortlist"]) == SHORTLIST[:1]
        assert "argus.tool_input" in a

    def test_a_tool_call_keeps_its_existing_behaviour(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("scanner", "download_frames", {"universe": "wide"}, {"message": "downloaded 90 frames"})
        s = [x for x in mock_argus_exporter.spans if _attrs(x).get("argus.step_type") == "tool_call"][-1]
        assert json.loads(_attrs(s)["argus.tool_input"]) == {"universe": "wide"}


# ── the cap ───────────────────────────────────────────────────────────────────

class TestCap:
    def test_the_bound_is_the_one_provy_shows_the_judge(self):
        # trace/logger.py and web/lib/judge-context.ts MAX_GIVEN_ITEM_CHARS must agree, or Provy cuts mid-string anyway.
        assert GIVEN_INPUT_MAX_CHARS == 2_000

    def test_a_small_input_is_untouched(self):
        v = {"regime": "normal", "shortlist": SHORTLIST[:3]}
        assert json.loads(_bounded_input(v)) == v

    def test_an_oversized_list_loses_trailing_items_and_says_how_many(self):
        big = {"regime": "normal", "max_n": 5,
               "shortlist": [dict(c, note="n" * 100) for c in SHORTLIST * 3]}   # 45 items, far over the bound
        out = _bounded_input(big)
        assert len(out) <= GIVEN_INPUT_MAX_CHARS
        parsed = json.loads(out)                                   # still valid JSON, which a string clip is not
        kept = parsed["shortlist"]
        assert 0 < len(kept) < 45
        assert kept == big["shortlist"][:len(kept)]                # the BEST-FIRST head survives, the tail goes
        assert parsed["not_recorded"] == {"shortlist": 45 - len(kept)}
        assert parsed["regime"] == "normal" and parsed["max_n"] == 5

    def test_the_longest_list_is_shortened_first(self):
        v = {"short": [1, 2, 3], "long": [{"k": "x" * 80} for _ in range(60)]}
        parsed = json.loads(_bounded_input(v))
        assert parsed["short"] == [1, 2, 3]
        assert "short" not in parsed["not_recorded"] and parsed["not_recorded"]["long"] > 0

    def test_a_list_at_the_top_is_wrapped_so_the_count_has_somewhere_to_go(self):
        parsed = json.loads(_bounded_input([{"k": "x" * 100} for _ in range(60)]))
        assert parsed["not_recorded"]["items"] > 0 and len(parsed["items"]) > 0

    def test_an_input_with_nothing_to_drop_is_cut_as_valid_json_and_marked(self):
        out = _bounded_input({"blob": "z" * 9_000})
        assert len(out) <= GIVEN_INPUT_MAX_CHARS
        parsed = json.loads(out)
        assert parsed["truncated"] is True and parsed["text"].startswith("{")

    def test_a_non_serialisable_value_does_not_raise(self):
        assert json.loads(_bounded_input({"when": object()}))["when"].startswith("<object")

    def test_a_message_step_gets_the_bounded_form_on_the_wire(self, tracer, mock_argus_exporter):
        big = {"shortlist": [dict(c, note="n" * 100) for c in SHORTLIST * 3]}
        tracer.log_agent_message("scanner", "picked", "completed", tool_input=big)
        raw = _attrs(_message_spans(mock_argus_exporter)[-1])["argus.tool_input"]
        assert len(raw) <= GIVEN_INPUT_MAX_CHARS
        assert json.loads(raw)["not_recorded"]["shortlist"] > 0

    def test_a_tool_call_keeps_the_old_4000_character_clip(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("scanner", "t", {"blob": "z" * 9_000}, {"ok": True})
        s = [x for x in mock_argus_exporter.spans if _attrs(x).get("argus.step_type") == "tool_call"][-1]
        assert len(_attrs(s)["argus.tool_input"]) == 4_000


# ── the scanner ───────────────────────────────────────────────────────────────

class TestScanner:
    def _run(self):
        from agents.scanner_agent import _llm_select
        client = MagicMock()
        client.messages.create.return_value = NS(
            content=[NS(text='{"selected": ["AAA"], "scan_rationale": "r", "signals_used": []}')],
            usage=NS(input_tokens=1, output_tokens=1), model="m")
        tracer = MagicMock()
        ranked = [{"ticker": "AAA", "technical_score": 9, "premarket_change_pct": 1.5, "sector": "Tech"},
                  {"ticker": "BBB", "technical_score": 6, "premarket_change_pct": -0.2, "sector": "Health"}]
        _llm_select(client, tracer, ranked, [], {"decision": "GO"}, "normal", 2)
        return tracer.log_agent_message.call_args.kwargs

    def test_the_scanner_declares_its_shortlist_and_regime_as_its_given_input(self):
        kw = self._run()
        assert kw["tool_input"]["regime"] == "normal" and kw["tool_input"]["max_n"] == 2
        assert [c["ticker"] for c in kw["tool_input"]["shortlist"]] == ["AAA", "BBB"]
        assert kw["tool_input"]["shortlist"][0] == {"ticker": "AAA", "technical_score": 9, "premarket_change_pct": 1.5, "sector": "Tech"}

    def test_the_existing_payload_keys_are_kept_so_nothing_that_reads_them_breaks(self):
        kw = self._run()
        assert kw["payload"]["regime"] == "normal" and kw["payload"]["max_n"] == 2
        assert [c["ticker"] for c in kw["payload"]["shortlist"]] == ["AAA", "BBB"]

    def test_the_model_s_answer_is_not_recorded_as_an_input(self):
        kw = self._run()
        assert "selected" not in kw["tool_input"] and "scan_rationale" not in kw["tool_input"]

    def test_the_input_is_the_same_object_in_both_places_so_they_cannot_drift(self):
        kw = self._run()
        assert kw["tool_input"]["shortlist"] == kw["payload"]["shortlist"]


# ── end to end: the real logger, the real exporter, Provy's gateway shape ─────

class TestThroughTheRealExporter:
    def _wire(self, recorder):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return NS()

        with patch("trace.otel_exporter.urllib.request.urlopen", fake_urlopen):
            assert ArgusExporter(api_key="k", endpoint="http://x").export(recorder.spans) == 0
        return seen["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]

    def _attr(self, span, key):
        for a in span["attributes"]:
            if a["key"] == key:
                return a["value"]["stringValue"]
        return None

    def test_the_scanner_message_span_reaches_the_gateway_with_a_parseable_tool_input(self, tracer, mock_argus_exporter):
        tracer.log_tool_call("scanner", "download_frames", {"universe": "wide"}, {"message": "downloaded 90 frames"})
        tracer.log_agent_message("scanner", "Picked AAA.", "completed", entity_id=None,
                                 payload={"regime": "normal", "max_n": 5, "shortlist": SHORTLIST},
                                 tool_input={"regime": "normal", "max_n": 5, "shortlist": SHORTLIST})
        spans = self._wire(mock_argus_exporter)
        msg = [s for s in spans if self._attr(s, "argus.step_type") == "agent_message"][-1]
        raw = self._attr(msg, "argus.tool_input")
        assert json.loads(raw)["shortlist"][0]["ticker"] == "T00"
        # the same keys are still sent under argus.payload.* for everything that reads them
        assert json.loads(self._attr(msg, "argus.payload.shortlist"))[0]["ticker"] == "T00"
        # and the tool call is untouched
        tool = [s for s in spans if self._attr(s, "argus.step_type") == "tool_call"][-1]
        assert json.loads(self._attr(tool, "argus.tool_output")) == {"message": "downloaded 90 frames"}


# ── NaN and Infinity (V9 D4) ──────────────────────────────────────────────────

def _strict(text):
    return json.loads(text, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))


class TestNonFiniteNumbers:
    """json.dumps writes NaN and Infinity, which are not JSON: Provy can only keep that as an unreadable string. They are recorded as null."""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_value_becomes_null_and_the_output_parses(self, bad):
        assert _strict(_bounded_input({"premarket_change_pct": bad, "ok": 1.5})) == {"premarket_change_pct": None, "ok": 1.5}

    def test_nested_in_a_list_of_dicts_and_over_the_bound(self):
        big = {"shortlist": [{"ticker": f"T{i}", "x": float("nan"), "note": "n" * 100} for i in range(60)]}
        out = _bounded_input(big)
        assert len(out) <= GIVEN_INPUT_MAX_CHARS
        parsed = _strict(out)
        assert parsed["shortlist"][0]["x"] is None and parsed["not_recorded"]["shortlist"] > 0

    def test_the_truncated_text_fallback_is_clean_too(self):
        _strict(_bounded_input({"blob": "z" * 9_000, "x": float("inf")}))

    def test_on_the_wire(self, tracer, mock_argus_exporter):
        tracer.log_agent_message("scanner", "picked", "completed", tool_input={"shortlist": [{"premarket_change_pct": float("nan")}]})
        raw = _attrs(_message_spans(mock_argus_exporter)[-1])["argus.tool_input"]
        assert _strict(raw) == {"shortlist": [{"premarket_change_pct": None}]}
