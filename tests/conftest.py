from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(path: str) -> dict | list:
    return json.loads((FIXTURES / path).read_text())


def make_db_result(data: list) -> MagicMock:
    """Build a mock Supabase execute() result with a .data attribute."""
    result = MagicMock()
    result.data = data
    return result


def make_query(data: list) -> MagicMock:
    """
    Build a fully-chainable Supabase query mock.
    All builder methods (.select, .eq, .gte, etc.) return self.
    .execute() returns make_db_result(data).
    """
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
    q.execute.return_value = make_db_result(data)
    return q


@pytest.fixture
def tracer(mock_supabase):
    """Shared TraceLogger backed by mock_supabase for agent tests."""
    from trace.logger import TraceLogger
    mock_supabase.table.return_value = make_query([])
    return TraceLogger("test-session-id-1234")


def make_api_response(stop_reason, blocks, in_tok=200, out_tok=100):
    """Build a mock anthropic.messages.create response."""
    r = MagicMock()
    r.stop_reason = stop_reason
    r.content = blocks
    r.usage = MagicMock(input_tokens=in_tok, output_tokens=out_tok)
    return r


def tool_block(name, inp=None, bid="tool-1"):
    """Build a mock tool_use content block."""
    b = MagicMock()
    b.type = "tool_use"
    b.id = bid
    b.name = name
    b.input = inp or {}
    return b


def text_block(text):
    """Build a mock text content block."""
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


@pytest.fixture
def mock_supabase(monkeypatch):
    """
    Patches core.db.get_client to return a mock Supabase client.

    Usage in tests:
        def test_foo(mock_supabase):
            mock_supabase.table.return_value = make_query([{"key": "val"}])
            ...
    """
    mock_client = MagicMock()
    monkeypatch.setattr("core.db.get_client", lambda: mock_client)
    # Default: all table calls return an empty-result query
    mock_client.table.return_value = make_query([])
    return mock_client
