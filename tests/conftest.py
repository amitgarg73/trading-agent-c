from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(path: str) -> dict | list:
    return json.loads((FIXTURES / path).read_text())


def make_db_result(data: list) -> MagicMock:
    result = MagicMock()
    result.data = data
    return result


def make_query(data: list) -> MagicMock:
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.gte.return_value = q
    q.lte.return_value = q
    q.order.return_value = q
    q.limit.return_value = q
    q.update.return_value = q
    q.insert.return_value = q
    q.upsert.return_value = q
    q.in_.return_value = q
    q.neq.return_value = q
    q.ilike.return_value = q
    q.maybeSingle = MagicMock(return_value=q)
    q.single.return_value = q
    q.execute.return_value = make_db_result(data)
    return q


@pytest.fixture
def mock_supabase(monkeypatch):
    """
    Patches core.db.get_client to return a mock Supabase client.
    Still needed for flush_cost_breakdown (direct write) and judge.py quality score patch.
    """
    mock_client = MagicMock()
    monkeypatch.setattr("core.db.get_client", lambda: mock_client)
    mock_client.table.return_value = make_query([])
    return mock_client


class RecordingExporter:
    """Captures OTel spans exported by ArgusExporter for test assertions."""
    def __init__(self):
        self.spans: list = []

    def export(self, spans) -> int:
        self.spans.extend(spans)
        return 0  # SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


@pytest.fixture
def mock_argus_exporter():
    """Replaces ArgusExporter with RecordingExporter so test can inspect exported spans."""
    recorder = RecordingExporter()
    # Patch the source module — ArgusExporter is imported inside __init__.
    # Also force the export-gate vars truthy: TraceLogger only wires the span
    # processor when _ARGUS_URL and _ARGUS_API_KEY are set (correct prod behavior),
    # so on a runner with no creds (CI) the recorder would never receive spans.
    with patch("trace.otel_exporter.ArgusExporter", return_value=recorder), \
         patch("trace.logger._ARGUS_URL", "http://argus.test"), \
         patch("trace.logger._ARGUS_API_KEY", "test-key"):
        yield recorder


@pytest.fixture
def mock_ingest_post():
    """Patches trace.logger._ingest_post — used only for session open/close (not trace rows)."""
    with patch("trace.logger._ingest_post") as m:
        yield m


@pytest.fixture
def mock_ingest_patch():
    """Patches trace.logger._ingest_patch — PATCH writes (quality_score, cost_breakdown)."""
    with patch("trace.logger._ingest_patch") as m:
        yield m


@pytest.fixture
def mock_ingest_get():
    """Patches trace.logger._ingest_get — GET reads (eval configs, traces)."""
    with patch("trace.logger._ingest_get") as m:
        m.return_value = {}
        yield m


@pytest.fixture
def tracer(mock_argus_exporter, mock_ingest_post, mock_ingest_patch, mock_supabase):
    """
    Shared TraceLogger with OTel spans captured by RecordingExporter.
    Session open/close still go through mock_ingest_post (ingest API).
    """
    from trace.logger import TraceLogger
    t = TraceLogger("test-session-id-1234")
    t._open_thread.join(timeout=1.0)
    return t


def make_api_response(stop_reason, blocks, in_tok=200, out_tok=100):
    r = MagicMock()
    r.stop_reason = stop_reason
    r.content = blocks
    r.usage = MagicMock(input_tokens=in_tok, output_tokens=out_tok)
    return r


def tool_block(name, inp=None, bid="tool-1"):
    b = MagicMock()
    b.type = "tool_use"
    b.id = bid
    b.name = name
    b.input = inp or {}
    return b


def text_block(text):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b
