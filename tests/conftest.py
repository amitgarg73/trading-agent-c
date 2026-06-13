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


@pytest.fixture
def mock_ingest_post():
    """Patches trace.logger._ingest_post — POST writes (session open/close, traces, evals)."""
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
def tracer(mock_ingest_post, mock_ingest_patch, mock_supabase):
    """
    Shared TraceLogger backed by mocked ingest API.
    mock_supabase still required for trading-specific table reads (c_ tables, scanner_tools, etc.).
    Waits for the open-session daemon thread to complete before returning.
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
