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


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    """
    No test may open a real database connection.

    core/db.py loads .streamlit/secrets.toml when SUPABASE_URL is unset, so a local test run has
    live production credentials sitting in the environment. Any test that forgets to mock the
    client therefore writes to the real trading database, silently and with a passing result.

    That is not hypothetical. On 2026-07-27 a heartbeat write was added to position_watchdog.main()
    inside a finally block. The existing test mocked everything main() touched at the time, but not
    the database client, so the suite wrote a fake heartbeat into the live table -- one that would
    have told the watchdog the agent was healthy when it was not.

    Blocking client construction turns that class of mistake into a loud failure. Tests that mock
    core.db.get_client never reach this.
    """
    def _refuse(*_args, **_kwargs):
        raise RuntimeError(
            "This test tried to open a real Supabase connection. Local runs carry production "
            "credentials, so the write would have hit the live trading database. Use the "
            "mock_supabase fixture, or patch the specific function that reaches the database."
        )

    monkeypatch.setattr("core.db.create_client", _refuse)
    # core.db caches its client; a real one built by an earlier test would otherwise be reused.
    monkeypatch.setattr("core.db._client", None, raising=False)


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
    # Force the export gate open: TraceLogger only wires the span processor when
    # telemetry emission is enabled (creds present + explicit opt-in). Tests must
    # capture spans regardless of ambient creds or CI env, so patch _emit_enabled
    # True and keep the creds truthy for the ArgusExporter constructor args.
    with patch("trace.otel_exporter.ArgusExporter", return_value=recorder), \
         patch("trace.logger._emit_enabled", return_value=True), \
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


@pytest.fixture(autouse=True)
def _no_real_broker(monkeypatch):
    """
    No test may open a real broker connection.

    ⛔ THIS IS THE OTHER HALF OF `_no_real_database` AND IT WAS MISSING FOR MONTHS. That fixture
    exists because a local run carries live credentials, so a test that forgets to mock reaches
    production. Every word of it is equally true of Alpaca, and nothing guarded Alpaca: a test that
    called into `core.alpaca` without patching would have built a real `TradingClient` from the same
    environment and talked to the live broker. The database side of that mistake writes a fake
    heartbeat; this side places or cancels an order.

    It is guarded at CLIENT CONSTRUCTION rather than per function, the same as the database, because
    an allowlist of "the dangerous ones" is a list somebody has to keep correct forever. Reading is
    blocked along with writing on purpose: a quote fetched from the live market makes a test's result
    depend on the time of day, which is its own kind of silent failure.

    Tests that patch `core.alpaca.<function>` never reach this. Tests that need a client should patch
    `core.alpaca._client` (or `_dclient` / `_nclient`) with a mock.
    """
    def _refuse(name: str):
        def _inner(*_args, **_kwargs):
            raise RuntimeError(
                f"This test tried to construct a real Alpaca {name}. Local runs carry live broker "
                "credentials, so the call would have reached the real account. Patch the specific "
                "core.alpaca function you need, or patch core.alpaca._client / _dclient / _nclient."
            )
        return _inner

    monkeypatch.setattr("core.alpaca._client",  _refuse("TradingClient"))
    monkeypatch.setattr("core.alpaca._dclient", _refuse("StockHistoricalDataClient"))
    monkeypatch.setattr("core.alpaca._nclient", _refuse("NewsClient"))
    # Cached clients: a real one built by an earlier test would otherwise be reused past the patch.
    monkeypatch.setattr("core.alpaca._trading_client", None, raising=False)
    monkeypatch.setattr("core.alpaca._data_client",    None, raising=False)
    monkeypatch.setattr("core.alpaca._news_client",    None, raising=False)
