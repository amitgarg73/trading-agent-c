from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.tools.news_tools_helper import fetch_news


def _make_ticker_mock(news_items: list) -> MagicMock:
    t = MagicMock()
    t.news = news_items
    return t


class TestFetchNews:
    def test_returns_headlines_new_format(self):
        """New yfinance format: item["content"]["title"] etc."""
        items = [
            {
                "content": {
                    "title": "Apple unveils M4 chip",
                    "pubDate": "2026-05-27T08:00:00Z",
                    "provider": {"displayName": "Reuters"},
                }
            }
        ]
        with patch("yfinance.Ticker", return_value=_make_ticker_mock(items)):
            result = fetch_news("AAPL")
        assert result["ticker"] == "AAPL"
        assert result["count"] == 1
        assert result["headlines"][0]["title"] == "Apple unveils M4 chip"
        assert result["headlines"][0]["publisher"] == "Reuters"
        assert result["error"] is None

    def test_returns_headlines_old_format(self):
        """Old yfinance format: item["title"] / item["publisher"] directly."""
        items = [
            {
                "title": "Nvidia wins contract",
                "providerPublishTime": "2026-05-27T07:00:00Z",
                "publisher": "Bloomberg",
            }
        ]
        with patch("yfinance.Ticker", return_value=_make_ticker_mock(items)):
            result = fetch_news("NVDA")
        assert result["count"] == 1
        assert result["headlines"][0]["title"] == "Nvidia wins contract"
        assert result["headlines"][0]["publisher"] == "Bloomberg"

    def test_caps_at_five_headlines(self):
        items = [
            {"title": f"Headline {i}", "providerPublishTime": "", "publisher": "Pub"}
            for i in range(10)
        ]
        with patch("yfinance.Ticker", return_value=_make_ticker_mock(items)):
            result = fetch_news("AAPL")
        assert result["count"] == 5
        assert len(result["headlines"]) == 5

    def test_empty_news_returns_zero_count(self):
        with patch("yfinance.Ticker", return_value=_make_ticker_mock([])):
            result = fetch_news("AAPL")
        assert result["count"] == 0
        assert result["headlines"] == []
        assert result["error"] is None

    def test_none_news_returns_zero_count(self):
        ticker_mock = MagicMock()
        ticker_mock.news = None
        with patch("yfinance.Ticker", return_value=ticker_mock):
            result = fetch_news("AAPL")
        assert result["count"] == 0

    def test_yfinance_exception_returns_error(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network error")):
            # fetch_news raises; main() catches it
            with pytest.raises(RuntimeError):
                fetch_news("AAPL")

    def test_skips_items_with_no_title(self):
        items = [
            {"content": {"title": ""}, "publisher": "X"},
            {"title": "Real headline", "providerPublishTime": "", "publisher": "Y"},
        ]
        with patch("yfinance.Ticker", return_value=_make_ticker_mock(items)):
            result = fetch_news("AAPL")
        assert result["count"] == 1
        assert result["headlines"][0]["title"] == "Real headline"
