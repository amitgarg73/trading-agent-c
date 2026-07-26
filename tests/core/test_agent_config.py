from __future__ import annotations

import pytest
from postgrest.exceptions import APIError

from core.agent_config import _DEFAULTS, get_config, is_trading_day, load_agent_config
from tests.conftest import make_query


class TestLoadAgentConfig:
    def test_returns_db_values_over_defaults(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "enable_intraday_entries", "config_value": True},
            {"config_key": "phase",                   "config_value": "paper"},
        ])
        config = load_agent_config()
        assert config["enable_intraday_entries"] is True
        assert config["phase"] == "paper"

    def test_returns_defaults_for_missing_keys(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        config = load_agent_config()
        assert config["enable_intraday_entries"] == _DEFAULTS["enable_intraday_entries"]
        assert config["trading_days"] == _DEFAULTS["trading_days"]

    def test_returns_full_defaults_when_db_empty(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        config = load_agent_config()
        for key, val in _DEFAULTS.items():
            assert config[key] == val

    def test_trading_days_parsed_as_list(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "trading_days", "config_value": ["MON", "WED", "FRI"]},
        ])
        config = load_agent_config()
        assert isinstance(config["trading_days"], list)
        assert "MON" in config["trading_days"]
        assert "WED" in config["trading_days"]

    def test_boolean_values_preserved(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "enable_learning_agent", "config_value": False},
        ])
        config = load_agent_config()
        assert config["enable_learning_agent"] is False

    def test_integer_values_preserved(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "intraday_min_score_bonus", "config_value": 2},
        ])
        config = load_agent_config()
        assert config["intraday_min_score_bonus"] == 2

    def test_string_values_preserved(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "intraday_entry_window_end", "config_value": "14:00"},
        ])
        config = load_agent_config()
        assert config["intraday_entry_window_end"] == "14:00"

    def test_db_value_overrides_default(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "enable_news_analyst", "config_value": False},
        ])
        config = load_agent_config()
        # Default is True; DB says False
        assert config["enable_news_analyst"] is False

    def test_returns_dict(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert isinstance(load_agent_config(), dict)


class TestGetConfig:
    def test_returns_config_value(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "phase", "config_value": "simulation"},
        ])
        assert get_config("phase") == "simulation"

    def test_returns_none_for_completely_unknown_key(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_config("completely_unknown_key") is None

    def test_returns_explicit_default_for_unknown_key(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_config("unknown_key", default="fallback") == "fallback"

    def test_returns_builtin_default_for_known_key_not_in_db(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        # trading_days has a builtin default
        result = get_config("trading_days")
        assert result == _DEFAULTS["trading_days"]


class TestIsTradingDay:
    def test_mon_is_trading_day_by_default(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert is_trading_day("MON") is True

    def test_fri_is_trading_day_by_default(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert is_trading_day("FRI") is True

    def test_sat_not_trading_day_by_default(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert is_trading_day("SAT") is False

    def test_sun_not_trading_day_by_default(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert is_trading_day("SUN") is False

    def test_case_insensitive(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert is_trading_day("mon") is True
        assert is_trading_day("Mon") is True

    def test_custom_trading_days_from_db(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"config_key": "trading_days", "config_value": ["MON", "WED", "FRI"]},
        ])
        assert is_trading_day("MON") is True
        assert is_trading_day("WED") is True
        assert is_trading_day("TUE") is False
        assert is_trading_day("THU") is False


class TestConfigReadResilience:
    """
    2026-07-24: a Cloudflare 525 in front of Supabase killed premarket on this exact read, before
    the session could even decide whether it was a trading day. See
    design/incident-2026-07-24-premarket-config-retry.md.
    """

    def test_survives_the_transient_failure_that_killed_premarket(self, mock_supabase, monkeypatch):
        monkeypatch.setattr("core.db._time.sleep", lambda _s: None)
        q = make_query([{"config_key": "phase", "config_value": "paper"}])
        result = q.execute.return_value
        q.execute.side_effect = [
            APIError({"message": "JSON could not be generated", "code": 525,
                      "hint": "", "details": "<!DOCTYPE html>"}),
            result,
        ]
        mock_supabase.table.return_value = q
        assert load_agent_config()["phase"] == "paper"
        assert q.execute.call_count == 2

    def test_is_trading_day_survives_a_transient_failure(self, mock_supabase, monkeypatch):
        monkeypatch.setattr("core.db._time.sleep", lambda _s: None)
        q = make_query([])
        result = q.execute.return_value
        q.execute.side_effect = [APIError({"message": "x", "code": 503, "hint": "", "details": ""}), result]
        mock_supabase.table.return_value = q
        assert is_trading_day("FRI") is True

    def test_does_NOT_silently_fall_back_to_defaults_when_the_db_is_unreachable(
        self, mock_supabase, monkeypatch
    ):
        # The defaults describe a suspended system (phase=simulation, intraday entries off).
        # Substituting them on a network error would run a live trading day on the wrong config.
        # Failing loudly is the correct behaviour for a financial system.
        monkeypatch.setattr("core.db._time.sleep", lambda _s: None)
        q = make_query([])
        q.execute.side_effect = APIError({"message": "x", "code": 525, "hint": "", "details": ""})
        mock_supabase.table.return_value = q
        with pytest.raises(APIError):
            load_agent_config()

    def test_a_permission_error_is_not_retried(self, mock_supabase):
        q = make_query([])
        q.execute.side_effect = APIError({"message": "permission denied", "code": "42501",
                                          "hint": "", "details": ""})
        mock_supabase.table.return_value = q
        with pytest.raises(APIError):
            load_agent_config()
        assert q.execute.call_count == 1
