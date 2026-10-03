"""argus#1519: the context manifest on the decision steps, behind a switch that defaults OFF.

The promises under test, in the order a reviewer would ask them:
  1. Switch OFF (the default and the merged state): every span of a whole session is byte-identical to what the code emitted before
     this change. `golden_off_1519.json` was captured from the unmodified logger and is compared whole.
  2. Switch ON but the builder is broken: the same bytes, every step still succeeds and returns what it returned. The manifest build is
     best effort and can never raise into a trading step.
  3. Switch ON: each agent's decision step carries what it was given, and only what the code truly knows.
  4. Bounded to the platform's 4 KB, cut from the end, saying so.
  5. Absent means unknown: nothing is invented for a step that was given nothing, a read that errored, or a source without a date.
  6. No prompt text, reply text, tool output text, news text or ticker is ever in a manifest (sentinel test).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from pathlib import Path

import pytest

from tests.trace import session_shapes_1519 as sh
from trace import context_manifest as cm
from trace.logger import TraceLogger

SWITCH = cm.SWITCH_ENV
GOLDEN = Path(__file__).with_name("golden_off_1519.json")
REPO = Path(__file__).resolve().parents[2]


def attrs(span):
    return dict(span.attributes or {})


def steps(rec, agent, step_type="agent_message"):
    return [s for s in rec.spans if attrs(s).get("argus.agent") == agent and attrs(s).get("argus.step_type") == step_type]


def manifest(span):
    raw = attrs(span).get(cm.ATTRIBUTE)
    return json.loads(raw) if raw is not None else None


def sha(text):
    import hashlib
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


@pytest.fixture
def session(on, tracer, mock_argus_exporter):
    """A whole session with the switch on. Returns the recorder."""
    sh.run_session(tracer)
    return mock_argus_exporter


# ── 1. the switch is off by default and off is inert ──────────────────────────────────────────

class TestSwitchOff:
    def test_default_is_off(self, off):
        assert cm.enabled() is False

    @pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True), (" on ", True),
                                                 ("0", False), ("", False), ("no", False), ("off", False), ("false", False), ("2", False)])
    def test_only_a_plain_yes_turns_it_on(self, monkeypatch, value, expected):
        monkeypatch.setenv(SWITCH, value)
        assert cm.enabled() is expected

    def test_a_whole_session_is_byte_identical_to_the_code_before_this_change(self, off, tracer, mock_argus_exporter):
        sh.run_session(tracer)
        assert sh.stable_attrs(mock_argus_exporter.spans) == json.loads(GOLDEN.read_text())

    def test_no_span_carries_the_attribute_and_the_builder_is_never_called(self, off, tracer, mock_argus_exporter, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the manifest builder ran with the switch off")
        monkeypatch.setattr(cm, "build", boom)
        monkeypatch.setattr(cm, "Recorder", boom)
        sh.run_session(tracer)
        assert all(cm.ATTRIBUTE not in attrs(s) for s in mock_argus_exporter.spans)
        assert sh.stable_attrs(mock_argus_exporter.spans) == json.loads(GOLDEN.read_text())

    def test_a_context_passed_by_hand_is_ignored_when_off(self, off, tracer, mock_argus_exporter):
        tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, context={"v": 1, "items": [{"kind": "other"}]})
        tracer.log_decision("market", "skip", context={"v": 1, "items": [{"kind": "other"}]})
        assert all(cm.ATTRIBUTE not in attrs(s) for s in mock_argus_exporter.spans)

    def test_the_circuit_breaker_helper_answers_none_when_off(self, off):
        assert cm.circuit_breaker_context(sh.CB_VIX, sh.CB_FUTURES) is None

    def test_the_workflows_wire_the_switch_from_a_repo_variable_and_never_hardcode_it_on(self):
        # The variable is unset until the founder sets it, so the merged workflows run with the switch off.
        flows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
        emitting = [f for f in flows if 'PROVY_EMIT: "1"' in f.read_text()]
        assert emitting, "expected the emitting workflows"
        for f in emitting:
            text = f.read_text()
            assert "PROVY_CONTEXT_MANIFEST: ${{ vars.PROVY_CONTEXT_MANIFEST }}" in text, f.name
            assert 'PROVY_CONTEXT_MANIFEST: "1"' not in text and "PROVY_CONTEXT_MANIFEST: 1" not in text, f.name


# ── 2. best effort: a broken builder changes nothing ──────────────────────────────────────────

class TestABrokenBuilderIsHarmless:
    @pytest.mark.parametrize("broken", ["build", "Recorder.record_tool", "Recorder.note", "Recorder.take", "Recorder.upstream",
                                        "Recorder.remember_span", "instruction_for", "content_hash", "tool_item"])
    def test_every_failure_leaves_the_session_byte_identical_and_successful(self, on, tracer, mock_argus_exporter, monkeypatch, broken):
        def boom(*a, **k):
            raise RuntimeError("manifest builder exploded")
        if "." in broken:
            cls, name = broken.split(".")
            monkeypatch.setattr(getattr(cm, cls), name, boom)
        else:
            monkeypatch.setattr(cm, broken, boom)
        results = sh.run_session(tracer)                      # must not raise
        assert results["market"]["decision"] == "GO"
        assert results["risk"]["verdicts"][0]["verdict"] == "APPROVED"
        assert results["orchestrator"]["total_estimated_profit"] == 12.5
        assert results["learner"]["learnings_confirmed"] == 1
        emitted = sh.stable_attrs(mock_argus_exporter.spans)
        golden = json.loads(GOLDEN.read_text())
        # A failing builder may only ever REMOVE the new attribute, never change another byte.
        stripped = [{**r, "attrs": {k: v for k, v in r["attrs"].items() if k != cm.ATTRIBUTE}} for r in emitted]
        assert stripped == golden

    def test_the_logger_survives_a_builder_that_raises_a_base_exception_subclass_of_exception(self, on, tracer, mock_argus_exporter, monkeypatch):
        monkeypatch.setattr(cm, "build", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
        span_id = tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
        assert isinstance(span_id, str) and len(span_id) == 16

    def test_the_circuit_breaker_decision_still_logs_when_the_builder_breaks(self, on, tracer, mock_argus_exporter, monkeypatch):
        monkeypatch.setattr(cm, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        result = sh.run_market(tracer, vix=sh.CB_VIX, futures=sh.CB_FUTURES)
        assert result["decision"] == "SKIP"
        assert steps(mock_argus_exporter, "market", "decision")

    def test_an_unserialisable_manifest_is_dropped_not_raised(self, on, tracer, mock_argus_exporter):
        class Odd:
            def __repr__(self):
                raise RuntimeError("no repr")
        tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, context={"v": 1, "items": [Odd()]})
        assert steps(mock_argus_exporter, "risk")                      # the step was written
        assert cm.ATTRIBUTE not in attrs(steps(mock_argus_exporter, "risk")[-1])

    def test_a_step_costs_microseconds_not_milliseconds(self, on):
        entries = [{"name": "get_x", "output": {"value": "v" * 2000, "rows": list(range(300))}} for _ in range(6)]
        sh_up = [{"agent": "market", "span_id": "a" * 16}, {"agent": "scanner", "span_id": "b" * 16}]
        samples = []
        for _ in range(200):
            t0 = time.perf_counter()
            assert cm.build("research_ZZTA", entries, sh_up, True)
            samples.append((time.perf_counter() - t0) * 1000)
        # Measured near 0.1 ms on a laptop; the bound is a hundred times looser than that and a thousand times under one model call.
        assert statistics.median(samples) < 10.0


# ── 3. what each agent's decision step sends ──────────────────────────────────────────────────

class TestMarket:
    def test_six_reads_the_instruction_and_nothing_it_does_not_know(self, session):
        m = manifest(steps(session, "market")[-1])
        assert [i["source"] for i in m["items"]] == ["tool:get_vix", "tool:get_futures", "tool:get_fear_greed", "tool:get_sector_rotation",
                                                    "tool:get_economic_calendar", "tool:get_treasury_yields"]
        assert all(i["kind"] == "tool_result" for i in m["items"])
        assert all("used" not in i and "as_of" not in i and "id" not in i for i in m["items"])
        assert m["items"][0]["hash"] == cm.content_hash(sh.VIX)
        assert "retrieval" not in m
        from agents.market_agent import _SYSTEM
        assert m["instruction"] == {"version": "auto-" + sha(_SYSTEM)[7:19], "hash": sha(_SYSTEM)}

    def test_the_circuit_breaker_decision_names_the_two_reads_it_decided_on(self, on, tracer, mock_argus_exporter):
        sh.run_market(tracer, vix=sh.CB_VIX, futures=sh.CB_FUTURES)
        m = manifest(steps(mock_argus_exporter, "market", "decision")[-1])
        assert [(i["source"], i.get("used")) for i in m["items"]] == [("tool:get_vix", True), ("tool:get_futures", True)]
        assert "instruction" not in m                                   # no model ran, so no instruction is claimed
        assert m["items"][0]["hash"] == cm.content_hash(sh.CB_VIX)


class TestScanner:
    def test_its_four_reads_the_market_step_and_the_scan_count(self, session):
        m = manifest(steps(session, "scanner")[-1])
        sources = [i["source"] for i in m["items"]]
        assert sources == ["step:market", "tool:get_scan_results", "tool:get_premarket_snapshot", "tool:get_gap_ups", "tool:get_sector_leaders"]
        up = m["items"][0]
        assert up["kind"] == "input" and up["used"] is True and len(up["id"]) == 16
        assert m["retrieval"] == {"returned": len(sh.SCAN)}
        from agents.scanner_agent import _SELECT_SYSTEM
        assert m["instruction"]["hash"] == sha(_SELECT_SYSTEM)

    def test_the_upstream_id_is_the_span_the_market_step_really_wrote(self, session):
        market_span = steps(session, "market")[-1]
        scanner_up = manifest(steps(session, "scanner")[-1])["items"][0]
        assert scanner_up["id"] == format(market_span.get_span_context().span_id, "016x")


class TestResearch:
    def test_each_ticker_sends_its_own_reads_and_the_news_it_was_handed(self, session):
        for t in sh.TICKERS:
            m = manifest(steps(session, f"research_{t}")[-1])
            assert [i["source"] for i in m["items"]] == ["step:market", "step:scanner", "tool:get_news", "tool:get_ticker_fundamentals",
                                                        "tool:get_ticker_market_data", "tool:get_position_history"]
            news = m["items"][2]
            assert news["used"] is True and news["hash"] == cm.content_hash(sh.NEWS)
            assert all("used" not in i for i in m["items"][3:])           # the model chose to ask: not the code's to say
            from agents.research_agent import _INVESTIGATE_SYSTEM
            assert m["instruction"]["hash"] == sha(_INVESTIGATE_SYSTEM)
            assert "retrieval" not in m

    def test_two_tickers_do_not_read_each_others_reads(self, session):
        a = manifest(steps(session, f"research_{sh.TICKERS[0]}")[-1])
        b = manifest(steps(session, f"research_{sh.TICKERS[1]}")[-1])
        assert len(a["items"]) == len(b["items"]) == 6


class TestRisk:
    def test_the_research_step_and_four_portfolio_reads(self, session):
        m = manifest(steps(session, "risk")[-1])
        assert [i["source"] for i in m["items"]] == ["step:research", "tool:get_open_positions", "tool:get_today_pnl", "tool:get_buying_power",
                                                    "tool:get_portfolio_exposure"]
        assert m["items"][0]["used"] is True
        from agents.risk_agent import _SYSTEM
        assert m["instruction"]["hash"] == sha(_SYSTEM)

    def test_an_empty_read_is_still_an_item_with_a_hash(self, session):
        m = manifest(steps(session, "risk")[-1])
        assert m["items"][1]["hash"] == cm.content_hash(sh.OPEN_POSITIONS)


class TestOrchestrator:
    def test_the_three_reports_it_synthesised_and_no_reads(self, session):
        m = manifest(steps(session, "orchestrator")[-1])
        assert [i["source"] for i in m["items"]] == ["step:market", "step:research", "step:risk"]
        assert all(i["used"] is True and i["kind"] == "input" for i in m["items"])
        from agents.orchestrator import _SYSTEM
        assert m["instruction"]["hash"] == sha(_SYSTEM)
        assert "retrieval" not in m

    def test_the_research_edge_is_the_newest_research_span(self, session):
        newest = [s for s in session.spans if attrs(s).get("argus.agent", "").startswith("research_") and attrs(s).get("argus.step_type") == "agent_message"][-1]
        edge = [i for i in manifest(steps(session, "orchestrator")[-1])["items"] if i["source"] == "step:research"][0]
        assert edge["id"] == format(newest.get_span_context().span_id, "016x")


class TestLearner:
    def test_four_reads_two_of_them_memory_and_the_write_left_out(self, session):
        m = manifest(steps(session, "learner")[-1])
        assert [(i["source"], i["kind"]) for i in m["items"]] == [("tool:read_today_trades", "tool_result"), ("tool:read_session_context", "tool_result"),
                                                                 ("tool:read_strategy_params", "memory"), ("tool:read_recent_learnings", "memory")]
        assert not any("write" in i["source"] for i in m["items"])
        from agents.learning_agent import _SYSTEM
        assert m["instruction"]["hash"] == sha(_SYSTEM)

    def test_memory_is_dated_by_its_newest_entry_and_counted(self, session):
        m = manifest(steps(session, "learner")[-1])
        learnings = m["items"][3]
        assert learnings["as_of"] == "2026-09-30T00:00:00.000Z"
        assert m["retrieval"] == {"returned": 2}
        assert "as_of" not in m["items"][2]                              # the params table carries no date of its own

    def test_no_learnings_yet_is_returned_zero_and_undated(self, on, tracer, mock_argus_exporter):
        sh.run_learner(tracer, learnings=[])
        m = manifest(steps(mock_argus_exporter, "learner")[-1])
        assert m["retrieval"] == {"returned": 0}
        assert "as_of" not in [i for i in m["items"] if i["source"] == "tool:read_recent_learnings"][0]

    def test_a_future_dated_learning_is_not_sent_as_an_as_of(self):
        assert cm.tool_item("read_recent_learnings", [{"learning_date": "2999-01-01"}]).get("as_of") is None


# ── 4. the size cap ───────────────────────────────────────────────────────────────────────────

class TestBound:
    def test_the_busiest_real_step_is_far_under_the_cap(self, session):
        for s in session.spans:
            raw = attrs(s).get(cm.ATTRIBUTE)
            if raw:
                assert len(raw.encode()) < 1_200

    def test_many_large_reads_are_cut_from_the_end_and_say_so(self):
        entries = [{"name": f"get_r{i}", "output": {"i": i}} for i in range(80)]
        m = cm.build("risk", entries, [{"agent": "research", "span_id": "c" * 16}], True)
        assert len(json.dumps(m, separators=(",", ":")).encode()) <= cm.MAX_BYTES
        assert m["truncated"] is True
        assert len(m["items"]) == cm.MAX_ITEMS
        assert m["items"][0]["source"] == "step:research"                 # upstream steps survive; the last reads go first
        assert m["items"][1]["source"] == "tool:get_r0"
        assert m["instruction"]["hash"].startswith("sha256:")             # the instruction is never the thing cut

    def test_a_size_cut_below_the_item_cap_still_fits(self, monkeypatch):
        monkeypatch.setattr(cm, "MAX_BYTES", 600)
        entries = [{"name": f"get_r{i}", "output": i} for i in range(10)]
        m = cm.build("risk", entries, [], True)
        assert len(json.dumps(m, separators=(",", ":")).encode()) <= 600 and m["truncated"] is True and 0 < len(m["items"]) < 10

    def test_a_runaway_step_that_never_finishes_cannot_grow_memory_without_bound(self):
        rec = cm.Recorder()
        for i in range(500):
            rec.record_tool("risk", "get_x", {"i": i})
        assert len(rec.take("risk")) == cm.PENDING_CAP

    def test_it_is_under_what_the_platform_stores(self):
        # The platform's own cap (web/lib/context-capture.ts CONTEXT_MAX_BYTES) is 4096 on the object it stores, which adds captured_by.
        assert cm.MAX_BYTES + len(',"captured_by":"otlp:provy"') < 4096


# ── 5. absent means unknown ───────────────────────────────────────────────────────────────────

class TestAbsentData:
    def test_a_model_step_given_nothing_it_can_name_sends_only_its_instruction(self, on, tracer, mock_argus_exporter):
        import agents.risk_agent  # noqa: F401  (the constant must be in memory, as it is in a real run)
        tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
        m = manifest(steps(mock_argus_exporter, "risk")[-1])
        assert set(m) == {"v", "instruction"}                              # no items key at all: unknown, never []

    def test_an_agent_with_no_known_instruction_and_nothing_given_sends_no_manifest(self, on, tracer, mock_argus_exporter):
        tracer.log_agent_message("mystery", "r", "completed", model=sh.MODEL, usage=sh.resp("end_turn", []).usage)
        assert cm.ATTRIBUTE not in attrs(steps(mock_argus_exporter, "mystery")[-1])

    def test_a_step_with_no_model_gets_none(self, on, tracer, mock_argus_exporter):
        # The nightly scanner has no model: its message is written by code from counts. Reads logged before it must not label it.
        tracer.log_tool_call("scanner", "fetch_scored_tickers", {}, {"to_score": 3})
        tracer.log_agent_message("scanner", "scored 3", "candidates_scored")
        assert cm.ATTRIBUTE not in attrs(steps(mock_argus_exporter, "scanner")[-1])

    def test_those_reads_do_not_leak_into_the_next_model_step(self, on, tracer, mock_argus_exporter):
        tracer.log_tool_call("scanner", "fetch_scored_tickers", {}, {"to_score": 3})
        tracer.log_agent_message("scanner", "scored 3", "candidates_scored")
        sh.run_scanner(tracer)
        m = manifest(steps(mock_argus_exporter, "scanner")[-1])
        assert "tool:fetch_scored_tickers" not in [i["source"] for i in m["items"]]

    def test_a_read_that_errored_is_listed_but_never_counted_as_zero_returned(self, on, tracer, mock_argus_exporter):
        sh.run_learner(tracer, learnings=[{"error": "timeout"}])
        m = manifest(steps(mock_argus_exporter, "learner")[-1])
        assert "tool:read_recent_learnings" in [i["source"] for i in m["items"]]
        assert "retrieval" not in m                                        # could not read is not the same as read nothing
        assert "as_of" not in [i for i in m["items"] if i["source"] == "tool:read_recent_learnings"][0]

    def test_writes_and_unknown_tool_prefixes_are_never_items(self):
        rec = cm.Recorder()
        for name in ("write_learning", "adjust_param", "recommend_goal", "place_order", "download_prices", "GET_x", "get_"):
            rec.record_tool("learner", name, {"a": 1})
        assert rec.take("learner") == []

    def test_an_upstream_id_the_logger_never_wrote_is_still_a_named_unknown_step(self, on, tracer, mock_argus_exporter):
        tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, inputs=["f" * 16])
        m = manifest(steps(mock_argus_exporter, "risk")[-1])
        assert m["items"][0] == {"kind": "input", "source": "step:unknown", "used": True, "id": "f" * 16}

    def test_a_malformed_upstream_id_is_ignored(self, on, tracer, mock_argus_exporter):
        tracer.log_agent_message("risk", "r", "completed", model=sh.MODEL, inputs=["short", None, 7])
        m = manifest(steps(mock_argus_exporter, "risk")[-1])
        assert "items" not in m


# ── 6. no text, no ticker ─────────────────────────────────────────────────────────────────────

class TestNothingButFingerprints:
    SENTINELS = (sh.SENTINEL_TOOL, sh.SENTINEL_REPLY, sh.SENTINEL_NEWS)

    def test_no_sentinel_and_no_ticker_appears_in_any_manifest(self, session):
        seen = 0
        for s in session.spans:
            raw = attrs(s).get(cm.ATTRIBUTE)
            if raw is None:
                continue
            seen += 1
            for needle in (*self.SENTINELS, *sh.TICKERS, "AAPL"):
                assert needle not in raw, (attrs(s).get("argus.agent"), needle)
        assert seen == 7

    def test_no_window_of_any_instruction_text_appears_in_a_manifest(self, session):
        from agents import learning_agent, market_agent, orchestrator, research_agent, risk_agent, scanner_agent
        texts = [market_agent._SYSTEM, scanner_agent._SELECT_SYSTEM, research_agent._INVESTIGATE_SYSTEM, risk_agent._SYSTEM,
                 orchestrator._SYSTEM, learning_agent._SYSTEM]
        blob = " ".join(attrs(s).get(cm.ATTRIBUTE, "") for s in session.spans)
        for t in texts:
            words = t.split()
            for i in range(0, len(words) - 3, 3):
                assert " ".join(words[i:i + 4]) not in blob

    def test_every_key_in_a_manifest_is_one_the_platform_reads(self, session):
        top, item = {"v", "items", "retrieval", "instruction", "tokens_in", "truncated"}, {"kind", "source", "id", "as_of", "used", "hash", "score", "version", "tokens"}
        for s in session.spans:
            m = manifest(s)
            if m is None:
                continue
            assert set(m) <= top
            assert all(set(i) <= item for i in m.get("items", []))
            assert set(m.get("instruction", {})) <= {"version", "hash"}

    def test_sources_are_generic_names_with_no_ticker_in_them(self, session):
        for s in session.spans:
            m = manifest(s)
            for i in (m or {}).get("items", []):
                assert re.fullmatch(r"(tool|step):[a-z_]+", i["source"]), i["source"]
                assert i.get("id") is None or re.fullmatch(r"[0-9a-f]{16}", i["id"])

    def test_every_hash_is_the_platforms_format(self, session):
        h = re.compile(r"sha256:[0-9a-f]{64}")
        for s in session.spans:
            m = manifest(s)
            if not m:
                continue
            for i in m.get("items", []):
                assert "hash" not in i or h.fullmatch(i["hash"])
            assert "hash" not in m.get("instruction", {}) or h.fullmatch(m["instruction"]["hash"])


# ── the contract with the instruction text and the logger ─────────────────────────────────────

class TestInstructionSource:
    def test_every_agent_constant_the_registry_names_exists_and_is_text(self):
        import importlib
        for agent, (module, name) in cm._INSTRUCTION_SOURCES.items():
            text = getattr(importlib.import_module(module), name)
            assert isinstance(text, str) and len(text) > 100, agent

    def test_the_version_changes_exactly_when_the_text_does(self, monkeypatch):
        import agents.risk_agent as risk
        before = cm.instruction_for("risk")
        assert cm.instruction_for("risk") == before
        monkeypatch.setattr(risk, "_SYSTEM", risk._SYSTEM + " one more rule")
        after = cm.instruction_for("risk")
        assert after["hash"] != before["hash"] and after["version"] != before["version"]

    def test_the_research_fan_out_resolves_to_the_research_instruction(self):
        import agents.research_agent  # noqa: F401
        assert cm.instruction_for("research_ZZTA") == cm.instruction_for("research")

    def test_the_base_name_rule_matches_the_loggers_own(self):
        for name in ("research_GILD", "market_shadow", "risk", "research_AB", "orchestrator", "x_y_ZZ"):
            assert cm.base_agent(name) == TraceLogger.__new__(TraceLogger)._base(name)

    def test_the_attribute_is_the_one_the_gateway_reads(self):
        # The OTLP gateway reads `provy.context` and, as the legacy spelling, `argus.context` (web/lib/otel-normalize.ts ourAttr).
        assert cm.ATTRIBUTE == "argus.context"


class TestWire:
    def test_the_attribute_is_one_json_object_and_nothing_else_about_the_span_changes(self, session):
        golden = json.loads(GOLDEN.read_text())
        emitted = sh.stable_attrs(session.spans)
        assert len(emitted) == len(golden)
        carrying = 0
        for got, was in zip(emitted, golden):
            rest = {k: v for k, v in got["attrs"].items() if k != cm.ATTRIBUTE}
            assert rest == was["attrs"] and got["name"] == was["name"] and got["status"] == was["status"]
            if cm.ATTRIBUTE in got["attrs"]:
                carrying += 1
                assert isinstance(json.loads(got["attrs"][cm.ATTRIBUTE]), dict)
        assert carrying == 7                              # market, scanner, two research, risk, orchestrator, learner

    def test_only_decision_steps_carry_one_never_a_tool_call(self, session):
        for s in session.spans:
            if cm.ATTRIBUTE in attrs(s):
                assert attrs(s)["argus.step_type"] in ("agent_message", "decision")

    def test_dump_the_shapes_for_the_replay(self, session):
        out = os.environ.get("CONTEXT_SHAPES_OUT")
        if not out:
            pytest.skip("set CONTEXT_SHAPES_OUT to write the synthetic session for the replay into a throwaway workspace")
        rows = []
        for s in session.spans:
            a = attrs(s)
            rows.append({"name": s.name, "span_id": format(s.get_span_context().span_id, "016x"),
                         "parent": format(s.parent.span_id, "016x") if s.parent else None, "attrs": {k: (v if isinstance(v, (str, int, float, bool)) else repr(v)) for k, v in a.items()},
                         "start": s.start_time, "end": s.end_time})
        Path(out).write_text(json.dumps(rows))
