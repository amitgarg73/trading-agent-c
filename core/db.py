from __future__ import annotations

import os
from pathlib import Path
from supabase import create_client, Client

_client: Client | None = None


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
