from __future__ import annotations

import os
from datetime import date
from typing import Any


_SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD":  "Technology", "GOOGL": "Technology", "META": "Technology",
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "JPM":  "Financials", "BAC": "Financials", "GS": "Financials",
    "JNJ":  "Healthcare",  "UNH": "Healthcare", "ABBV": "Healthcare",
    "XOM":  "Energy",      "CVX": "Energy",
    "CAT":  "Industrials", "BA": "Industrials",
    "PG":   "Consumer Staples", "KO": "Consumer Staples",
}


def _get_sector(ticker: str) -> str:
    return _SECTOR_MAP.get(ticker, "Other")


def get_open_positions() -> list[dict[str, Any]]:
    """Fetch open positions from c_positions."""
    try:
        from core.db import get_client
        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select("ticker,position_size,entry_price,realized_pnl")
            .eq("status", "open")
            .eq("open_date", today)
            .execute()
            .data
        ) or []

        return [
            {
                "ticker":         r["ticker"],
                "position_size":  r.get("position_size", 0),
                "entry_price":    r.get("entry_price", 0),
                "unrealized_pnl": r.get("realized_pnl", 0),
                "sector":         _get_sector(r["ticker"]),
            }
            for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)}]


def get_today_pnl() -> dict[str, Any]:
    """Fetch today's realized P&L from closed positions."""
    try:
        from core.db import get_client
        from core.agent_config import get_config
        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select("realized_pnl")
            .eq("status", "closed")
            .eq("close_date", today)
            .execute()
            .data
        ) or []

        realized_pnl  = sum(r.get("realized_pnl") or 0 for r in rows)
        trades_closed = len(rows)
        loss_limit    = float(get_config("daily_loss_limit", default=-500))
        limit_hit     = realized_pnl <= loss_limit

        return {
            "realized_pnl":  round(realized_pnl, 2),
            "trades_closed": trades_closed,
            "loss_limit":    loss_limit,
            "limit_hit":     limit_hit,
        }
    except Exception as e:
        return {"error": str(e)}


def get_buying_power() -> dict[str, Any]:
    """
    Phase 1: buying power = total_capital - sum of open position sizes.
    Phase 2: Alpaca account.buying_power.
    """
    try:
        from core.db import get_client
        from core.agent_config import get_config
        total_capital = float(get_config("total_capital", default=50_000))

        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select("position_size")
            .eq("status", "open")
            .eq("open_date", today)
            .execute()
            .data
        ) or []

        deployed = sum(r.get("position_size") or 0 for r in rows)
        buying_power = total_capital - deployed

        return {
            "buying_power":  round(buying_power, 2),
            "total_capital": total_capital,
            "deployed":      round(deployed, 2),
        }
    except Exception as e:
        return {"error": str(e)}


def get_portfolio_exposure() -> dict[str, Any]:
    """Compute open position count, total deployed, and sector concentration."""
    try:
        from core.db import get_client
        from core.agent_config import get_config
        total_capital = float(get_config("total_capital", default=50_000))

        today = date.today().isoformat()
        rows = (
            get_client()
            .table("c_positions")
            .select("ticker,position_size")
            .eq("status", "open")
            .eq("open_date", today)
            .execute()
            .data
        ) or []

        by_sector: dict[str, float] = {}
        total_deployed = 0.0
        for r in rows:
            size   = r.get("position_size") or 0
            sector = _get_sector(r["ticker"])
            by_sector[sector] = round(by_sector.get(sector, 0) + size, 2)
            total_deployed += size

        # Express sector allocation as % of total capital
        by_sector_pct = {
            k: round(v / total_capital * 100, 1) for k, v in by_sector.items()
        }
        max_sector_pct = max(by_sector_pct.values()) if by_sector_pct else 0.0

        return {
            "positions_open":  len(rows),
            "total_deployed":  round(total_deployed, 2),
            "by_sector":       by_sector_pct,
            "max_sector_pct":  max_sector_pct,
        }
    except Exception as e:
        return {"error": str(e)}
