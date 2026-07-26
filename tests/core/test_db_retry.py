from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from core.db import RETRY_ATTEMPTS, execute_with_retry, is_transient

"""
2026-07-24: premarket died on its first DB statement. Supabase sits behind Cloudflare, which
returned a 525 (SSL handshake to the origin failed), so postgrest got an HTML error page where it
expected JSON. Every run an hour later succeeded. The session had no retry, so a few minutes of
infrastructure trouble cost the whole day's premarket entry decisions.

See design/incident-2026-07-24-premarket-config-retry.md.
"""


def api_error(code) -> APIError:
    return APIError({"message": "JSON could not be generated", "code": code,
                     "hint": "Refer to full message for details", "details": "<!DOCTYPE html>"})


class Boom(Exception):
    """Stand-in for an httpx transport failure, matched on class name."""


class ReadTimeout(Exception):
    pass


class ConnectError(Exception):
    pass


def failing_query(errors: list[Exception], data=None) -> MagicMock:
    """A query that raises the given errors in order, then returns data."""
    result = MagicMock()
    result.data = data if data is not None else [{"config_key": "phase", "config_value": "paper"}]
    q = MagicMock()
    q.execute.side_effect = [*errors, result]
    return q


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries back off for seconds; tests should not."""
    monkeypatch.setattr("core.db._time.sleep", lambda _s: None)


class TestIsTransient:
    def test_the_cloudflare_525_that_broke_premarket_is_transient(self):
        assert is_transient(api_error(525)) is True

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 526, 527])
    def test_infrastructure_statuses_are_transient(self, status):
        assert is_transient(api_error(status)) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_client_errors_are_not_retried(self, status):
        # A permission or schema error will not improve on a second attempt.
        assert is_transient(api_error(status)) is False

    def test_a_postgres_sqlstate_is_not_mistaken_for_an_http_status(self):
        # 42501 is "insufficient privilege". int('42501') is a valid integer, so a naive
        # comparison against HTTP statuses would be nonsense; five characters means SQLSTATE.
        assert is_transient(api_error("42501")) is False
        assert is_transient(api_error("23505")) is False   # unique violation

    def test_a_three_digit_string_status_still_counts(self):
        assert is_transient(api_error("503")) is True
        assert is_transient(api_error("403")) is False

    def test_transport_failures_are_transient_by_class_name(self):
        assert is_transient(ReadTimeout("read timed out")) is True
        assert is_transient(ConnectError("connection refused")) is True

    def test_an_unrelated_exception_is_not_transient(self):
        assert is_transient(Boom("something else")) is False
        assert is_transient(ValueError("bad value")) is False
        assert is_transient(KeyError("SUPABASE_URL")) is False


class TestExecuteWithRetry:
    def test_returns_immediately_when_the_query_succeeds(self):
        q = failing_query([])
        assert execute_with_retry(q).data == [{"config_key": "phase", "config_value": "paper"}]
        assert q.execute.call_count == 1

    def test_recovers_from_a_transient_failure(self):
        q = failing_query([api_error(525)])
        assert execute_with_retry(q).data
        assert q.execute.call_count == 2

    def test_recovers_from_repeated_transient_failures(self):
        q = failing_query([api_error(525), api_error(503)])
        assert execute_with_retry(q).data
        assert q.execute.call_count == 3

    def test_reraises_a_non_transient_error_without_retrying(self):
        q = MagicMock()
        q.execute.side_effect = api_error(403)
        with pytest.raises(APIError):
            execute_with_retry(q)
        assert q.execute.call_count == 1

    def test_still_fails_loudly_when_retries_are_exhausted(self):
        # The whole point of not defaulting: after real effort, the caller must still see the error.
        q = MagicMock()
        q.execute.side_effect = api_error(525)
        with pytest.raises(APIError):
            execute_with_retry(q)
        assert q.execute.call_count == RETRY_ATTEMPTS

    def test_attempt_count_is_configurable(self):
        q = MagicMock()
        q.execute.side_effect = api_error(503)
        with pytest.raises(APIError):
            execute_with_retry(q, attempts=5)
        assert q.execute.call_count == 5

    def test_a_single_attempt_does_not_retry(self):
        q = MagicMock()
        q.execute.side_effect = api_error(503)
        with pytest.raises(APIError):
            execute_with_retry(q, attempts=1)
        assert q.execute.call_count == 1
