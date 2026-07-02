"""Tests for the telemetry emit gate — dev/agent runs must not write to prod Provy."""
import os
from unittest.mock import patch

import trace.logger as L


def _emit(url="https://provy.example", key="k", env=None):
    """Evaluate _emit_enabled with patched creds and a clean, explicit env."""
    env = env or {}
    with patch.object(L, "_ARGUS_URL", url), \
         patch.object(L, "_ARGUS_API_KEY", key), \
         patch.dict(os.environ, env, clear=True):
        return L._emit_enabled()


def test_no_creds_never_emits():
    assert _emit(url="", key="") is False
    assert _emit(url="https://provy.example", key="") is False
    assert _emit(url="", key="k") is False


def test_creds_present_but_no_opt_in_does_not_emit():
    # The core dev-safety case: a prod .env alone must not trigger emission.
    assert _emit(env={}) is False


def test_provy_emit_flag_opts_in():
    for val in ("1", "true", "TRUE", "yes", "on"):
        assert _emit(env={"PROVY_EMIT": val}) is True, val


def test_provy_emit_false_does_not_emit():
    for val in ("0", "false", "no", "off", ""):
        assert _emit(env={"PROVY_EMIT": val}) is False, val


def test_github_actions_is_automatic_opt_in():
    assert _emit(env={"GITHUB_ACTIONS": "true"}) is True


def test_github_actions_without_creds_still_does_not_emit():
    assert _emit(url="", key="", env={"GITHUB_ACTIONS": "true"}) is False
