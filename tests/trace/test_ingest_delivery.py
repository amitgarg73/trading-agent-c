"""The ingest transport must report whether a POST was actually accepted.

It used to end in `except Exception: pass` and return None. Every caller therefore saw
the same thing whether the request landed, was rejected, or never left the machine. The
outcome push counted its own loop iterations and logged deliveries it had not made, which
is how the Outcome Ledger accumulated predictions nobody ever answered.

The retired argusobs host still answers 200 and still accepts the fleet's key, so a stale
ARGUS_URL fails in exactly this shape: quietly, and looking like success. A transport that
cannot say "no" cannot catch that.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

import trace.logger as L


def _post(status=200, exc=None, path="/api/ingest/outcome"):
    """Call _ingest_post with emission enabled and urlopen stubbed to a given result."""
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False

    def fake_urlopen(req, timeout=None):
        if exc:
            raise exc
        return resp

    with patch.object(L, "_ARGUS_URL", "https://provy.example"), \
         patch.object(L, "_ARGUS_API_KEY", "k"), \
         patch.dict(os.environ, {"PROVY_EMIT": "1"}, clear=True), \
         patch("urllib.request.urlopen", side_effect=fake_urlopen):
        return L._ingest_post(path, {"entity_id": "CAT", "value": 1.0})


def test_accepted_post_returns_true():
    assert _post(status=200) is True


def test_2xx_range_is_accepted():
    for s in (200, 201, 202, 204):
        assert _post(status=s) is True, s


def test_rejected_post_returns_false():
    # 401 is the realistic one: a key that no longer resolves after a database split.
    for s in (400, 401, 403, 500, 503):
        assert _post(status=s) is False, s


def test_transport_error_returns_false_and_does_not_raise():
    # A dropped connection must not take the trading session down with it, but it must
    # also not be reported as a delivery.
    assert _post(exc=OSError("connection reset")) is False


def test_emission_disabled_returns_false():
    """Not emitting is not delivering. A caller counting successes must not count these."""
    with patch.object(L, "_ARGUS_URL", ""), patch.object(L, "_ARGUS_API_KEY", ""):
        assert L._ingest_post("/api/ingest/outcome", {"entity_id": "CAT"}) is False


def test_failure_is_printed_so_it_is_visible_in_the_run_log(capsys):
    assert _post(exc=OSError("boom")) is False
    assert "failed" in capsys.readouterr().out.lower()
