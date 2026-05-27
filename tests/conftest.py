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
    q.execute.return_value = make_db_result(data)
    return q


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
