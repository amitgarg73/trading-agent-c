from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.tools.risk_tools import (
    _get_sector,
    get_buying_power,
    get_open_positions,
    get_portfolio_exposure,
    get_today_pnl,
)
from tests.conftest import make_query


# ── _get_sector ────────────────────────────────────────────────────────────────

class TestGetSector:
    def test_known_ticker(self):  assert _get_sector("AAPL") == "Technology"
    def test_unknown_ticker(self): assert _get_sector("ZZZZZ") == "Other"


# ── get_open_positions ─────────────────────────────────────────────────────────

class TestGetOpenPositions:
    def test_returns_open_positions_with_sector(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"ticker": "AAPL", "position_size": 5000, "entry_price": 185.0, "unrealized_pnl": 50.0},
        ])
        result = get_open_positions()
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["sector"] == "Technology"

    def test_returns_empty_when_no_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        assert get_open_positions() == []

    def test_returns_error_on_db_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = get_open_positions()
        assert "error" in result[0]


# ── get_today_pnl ──────────────────────────────────────────────────────────────

class TestGetTodayPnl:
    def test_sums_realized_pnl(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"realized_pnl": 150.0},
            {"realized_pnl": -50.0},
        ])
        with patch("core.agent_config.get_config", return_value=-500):
            result = get_today_pnl()
        assert result["realized_pnl"] == pytest.approx(100.0)
        assert result["trades_closed"] == 2

    def test_limit_not_hit_when_above_floor(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"realized_pnl": 200.0}])
        with patch("core.agent_config.get_config", return_value=-500):
            result = get_today_pnl()
        assert result["limit_hit"] is False

    def test_limit_hit_when_at_floor(self, mock_supabase):
        mock_supabase.table.return_value = make_query([{"realized_pnl": -500.0}])
        with patch("core.agent_config.get_config", return_value=-500):
            result = get_today_pnl()
        assert result["limit_hit"] is True

    def test_returns_zero_pnl_when_no_trades(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.agent_config.get_config", return_value=-500):
            result = get_today_pnl()
        assert result["realized_pnl"] == 0.0
        assert result["trades_closed"] == 0

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = get_today_pnl()
        assert "error" in result


# ── get_buying_power ───────────────────────────────────────────────────────────

class TestGetBuyingPower:
    def test_buying_power_equals_capital_minus_deployed(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"position_size": 10_000},
            {"position_size": 5_000},
        ])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_buying_power()
        assert result["buying_power"]  == pytest.approx(35_000.0)
        assert result["deployed"]      == pytest.approx(15_000.0)
        assert result["total_capital"] == 50_000

    def test_full_capital_available_when_no_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_buying_power()
        assert result["buying_power"] == pytest.approx(50_000.0)

    def test_returns_error_on_exception(self, mock_supabase):
        mock_supabase.table.side_effect = Exception("db error")
        result = get_buying_power()
        assert "error" in result


# ── get_portfolio_exposure ─────────────────────────────────────────────────────

class TestGetPortfolioExposure:
    def test_counts_open_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"ticker": "AAPL", "position_size": 5000},
            {"ticker": "MSFT", "position_size": 5000},
        ])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_portfolio_exposure()
        assert result["positions_open"] == 2

    def test_sector_concentration_calculated(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"ticker": "AAPL", "position_size": 10_000},
            {"ticker": "MSFT", "position_size": 5_000},
            {"ticker": "JPM",  "position_size": 5_000},
        ])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_portfolio_exposure()
        assert result["by_sector"]["Technology"] == pytest.approx(30.0)
        assert result["by_sector"]["Financials"] == pytest.approx(10.0)

    def test_max_sector_pct_is_highest(self, mock_supabase):
        mock_supabase.table.return_value = make_query([
            {"ticker": "AAPL", "position_size": 20_000},
            {"ticker": "JPM",  "position_size": 5_000},
        ])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_portfolio_exposure()
        assert result["max_sector_pct"] == pytest.approx(40.0)

    def test_empty_when_no_positions(self, mock_supabase):
        mock_supabase.table.return_value = make_query([])
        with patch("core.agent_config.get_config", return_value=50_000):
            result = get_portfolio_exposure()
        assert result["positions_open"] == 0
        assert result["by_sector"] == {}
        assert result["max_sector_pct"] == 0.0
