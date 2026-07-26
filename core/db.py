from __future__ import annotations

import os
import time as _time
from pathlib import Path
from supabase import create_client, Client

_client: Client | None = None

# HTTP statuses worth a second attempt. 5xx and the Cloudflare 52x band are the infrastructure
# between us and Postgres failing, not our query; 429 is rate limiting; 408 is a server-side timeout.
# 4xx is deliberately absent: a permission, schema or constraint error will not improve on a retry,
# and retrying it only delays a real failure.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0


def _load_secrets_if_needed() -> None:
    """
    Fallback for local runs: load .streamlit/secrets.toml into env when
    SUPABASE_URL is not already set (e.g. running outside GitHub Actions or Streamlit).
    """
    if os.environ.get("SUPABASE_URL"):
        return
    toml_path = Path(__file__).parent.parent / ".streamlit" / "secrets.toml"
    if not toml_path.exists():
        return
    try:
        with open(toml_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except Exception:
        pass


_load_secrets_if_needed()


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
    return _client


def reset_client() -> None:
    """Force re-initialization on next get_client() call. Used in tests."""
    global _client
    _client = None


def _status_of(exc: Exception) -> int | None:
    """
    Pull an HTTP status off a postgrest APIError.

    `code` carries two different things depending on what failed. When the transport failed it is
    the HTTP status (int 525 for the Cloudflare case). When Postgres itself refused it is a
    five-character SQLSTATE string ('42501' = insufficient privilege). Those must not be confused:
    int('42501') is a perfectly good integer and would otherwise be compared against HTTP statuses.
    A SQLSTATE is always five characters and an HTTP status is always three, so length settles it.

    Returns None when there is no usable HTTP status, which callers treat as "not retryable".
    """
    code = getattr(exc, "code", None)
    if code is None:
        raw = getattr(exc, "json", None) or getattr(exc, "_raw_error", None)
        if isinstance(raw, dict):
            code = raw.get("code")
    if isinstance(code, str):
        if len(code) != 3 or not code.isdigit():
            return None          # SQLSTATE, or something else entirely
        return int(code)
    if isinstance(code, bool):   # bool is an int subclass; never a status
        return None
    if isinstance(code, int):
        return code
    return None


def is_transient(exc: Exception) -> bool:
    """
    Is this failure worth retrying? Transport-level errors (connection reset, read timeout, DNS)
    and the retryable HTTP statuses are; anything else is treated as a real answer from the
    database and re-raised immediately. Pure, so it can be tested without a network.
    """
    status = _status_of(exc)
    if status is not None:
        return status in _RETRYABLE_STATUS
    name = type(exc).__name__
    # httpx transport failures, without importing httpx here (supabase may swap its client).
    return name.endswith(("TimeoutException", "ConnectError", "ReadError", "RemoteProtocolError",
                          "TransportError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                          "PoolTimeout", "NetworkError"))


def execute_with_retry(
    query,
    *,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    description: str = "query",
):
    """
    Run a built postgrest query, retrying only transient infrastructure failures.

    Why this exists: on 2026-07-24 Supabase sat behind a Cloudflare 525 (SSL handshake to the
    origin failed) for a few minutes. The premarket session read c_agent_config as its very first
    statement, got an HTML error page where JSON was expected, and died before it could even decide
    whether that day was a trading day. Every run an hour later was fine. See
    design/incident-2026-07-24-premarket-config-retry.md.

    Retries are linear (base_delay, 2*base_delay, ...) and few on purpose: premarket runs inside a
    fixed window, so a long retry ladder would trade one failure mode for another. A non-transient
    error is re-raised untouched on the first attempt, and exhausting the attempts re-raises the
    last error so the caller still fails loudly.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return query.execute()
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            if not is_transient(exc):
                raise
            last = exc
            if attempt == attempts:
                break
            delay = base_delay * attempt
            print(f"[db] transient failure on {description} "
                  f"(attempt {attempt}/{attempts}, {type(exc).__name__}); retrying in {delay:.0f}s")
            _time.sleep(delay)
    assert last is not None
    print(f"[db] {description} failed after {attempts} attempts: {last}")
    raise last
