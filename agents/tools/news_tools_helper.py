#!/usr/bin/env python3
"""Fetch news for a single ticker via yfinance. Called by TypeScript News Analyst subprocess.

Usage: python3 news_tools_helper.py AAPL
Output: JSON to stdout
"""
from __future__ import annotations
import json
import sys


def _safe_str(val: object) -> str:
    return str(val) if val is not None else ""


def fetch_news(ticker: str) -> dict:
    import yfinance as yf  # imported here so test mocking is straightforward
    t = yf.Ticker(ticker)
    raw_news = t.news or []
    headlines = []
    for item in raw_news[:5]:
        # yfinance news format changed in v0.2.x — handle both old and new shapes
        content = item.get("content") or {}
        if isinstance(content, dict) and content.get("title"):
            title = _safe_str(content.get("title"))
            published = _safe_str(
                content.get("pubDate") or content.get("publishedAt") or ""
            )
            provider = content.get("provider") or {}
            publisher = (
                _safe_str(provider.get("displayName"))
                if isinstance(provider, dict)
                else _safe_str(provider)
            )
        else:
            title = _safe_str(item.get("title") or "")
            published = _safe_str(item.get("providerPublishTime") or "")
            publisher = _safe_str(item.get("publisher") or "")

        if title:
            headlines.append({
                "title": title,
                "published": published,
                "publisher": publisher,
            })

    return {"ticker": ticker, "headlines": headlines, "count": len(headlines), "error": None}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ticker": "", "headlines": [], "count": 0, "error": "no_ticker"}))
        sys.exit(0)

    ticker = sys.argv[1].strip().upper()
    try:
        result = fetch_news(ticker)
    except Exception as e:
        result = {"ticker": ticker, "headlines": [], "count": 0, "error": str(e)}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
