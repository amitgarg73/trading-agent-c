"""argus#1507: news the caller pre-fetched and put in the prompt is also recorded as a tool_call step, so a grader can check a claim against it.

Promises: with PROVY_RECORD_GIVEN on and record=True the given data is written as one tool_call step carrying the data, whether or not the
context manifest is on; with the switch off nothing is written (the golden shapes stay byte-identical); without record=True nothing is
written; a failing write never raises."""
from __future__ import annotations

import pytest

from trace import context_manifest as cm
from trace.logger import TraceLogger


class _Spy(TraceLogger):
    def __init__(self):            # no exporter, no environment
        self.written = []
        self._sequence = 0
    def _write(self, fields):
        self.written.append(fields)
        return "span"
    def _cm_recorder(self):
        class R:
            def note(self, *a, **k): pass
        return R()


NEWS = {"headlines": ["Mizuho cuts PSX to Neutral, raises target to $300"], "blackout": False}


def test_recorded_when_on(monkeypatch):
    monkeypatch.setenv("PROVY_RECORD_GIVEN", "1")
    t = _Spy()
    t.note_context("research_PSX", "get_news", NEWS, used=True, record=True, entity_id="PSX")
    assert len(t.written) == 1
    w = t.written[0]
    assert w["step_type"] == "tool_call" and w["tool_name"] == "get_news" and w["agent"] == "research_PSX"
    assert w["tool_output"] == NEWS and w["entity_id"] == "PSX"


def test_nothing_written_when_off(monkeypatch):
    monkeypatch.delenv("PROVY_RECORD_GIVEN", raising=False)
    t = _Spy()
    t.note_context("research_PSX", "get_news", NEWS, used=True, record=True, entity_id="PSX")
    assert t.written == []


def test_nothing_written_without_record(monkeypatch):
    monkeypatch.setenv("PROVY_RECORD_GIVEN", "1")
    t = _Spy()
    t.note_context("research_PSX", "get_news", NEWS, used=True)
    assert t.written == []


def test_a_failing_write_never_raises(monkeypatch):
    monkeypatch.setenv("PROVY_RECORD_GIVEN", "1")
    t = _Spy()
    def boom(fields): raise RuntimeError("exporter down")
    t._write = boom
    t.note_context("research_PSX", "get_news", NEWS, used=True, record=True, entity_id="PSX")   # must not raise
