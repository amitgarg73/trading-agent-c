# Tool Schemas — Trading Agent C

All tools return structured JSON. No free text in tool outputs.
Tool errors return `{ "error": str }` — agents handle gracefully.
No agent can call tools outside its registered set.

---

## Market Agent Tools (4 tools, all required per session)

### get_vix
```
input:  {}
output: { "value": float, "level": "LOW|ELEVATED|HIGH|CRISIS|EXTREME" }
        # LOW <15, ELEVATED 15-20, HIGH 20-25, CRISIS 25-30, EXTREME 30+
source: yfinance ^VIX (both phases)
```

### get_futures
```
input:  {}
output: {
  "S&P500":  { "change_pct": float },
  "Nasdaq":  { "change_pct": float },
  "Dow":     { "change_pct": float },
  "avg_change_pct": float,
  "bias": "BULLISH|BEARISH|NEUTRAL"
}
source: yfinance ES=F, NQ=F, YM=F (both phases)
```

### get_fear_greed
```
input:  {}
output: { "value": int, "classification": str }
        # 0-24 Extreme Fear, 25-44 Fear, 45-55 Neutral, 56-75 Greed, 76-100 Extreme Greed
source: alternative.me API (both phases)
```

### get_sector_rotation
```
input:  {}
output: [
  { "etf": "XLK", "change_pct": float },
  ...
]  // sorted best to worst, all 11 sector ETFs
source: yfinance (both phases)
```

---

## Research Agent Tools (6 tools, selective use)

### get_candidates
**Called exactly once per session.**
Returns ticker + score + price only — NOT full signals.
This is the key design difference from Strategy A.
Agent reads scores, decides which tickers to investigate.

```
input:  { "min_score": int }  // default 5
output: [
  {
    "ticker": str,
    "technical_score": int,      // -10 to +10
    "current_price": float,
    "avg_volume": int            // liquidity indicator only
  },
  ...
]  // sorted by score descending, max 100 results
source: scanner (yfinance-based, both phases)
```

### get_news
**Called per ticker being considered. If blackout: true, ticker is dropped.**

```
input:  { "ticker": str }
output: {
  "blackout": bool,             // true = earnings today or tomorrow — do not trade
  "reason": str | null,         // why blackout (e.g. "earnings 2026-05-26")
  "headlines": [str]            // last 3 relevant headlines, empty list if none
}
source: yfinance earnings calendar + headlines (both phases)
```

### get_live_price
```
input:  { "ticker": str }
output: {
  "price": float,               // best available current price
  "source": "alpaca|yfinance",  // which source responded
  "stale_minutes": int          // how old the data is
}
source: Phase 1: yfinance 1-min close
        Phase 2: Alpaca ask price
```

### get_intraday_signals
```
input:  { "ticker": str }
output: {
  "above_vwap": bool,
  "vwap": float,
  "rs_vs_spy": float | null,    // relative strength vs SPY since open; null if SPY flat
  "today_pct_change": float     // % move from today's open to now
}
source: Phase 1: yfinance 1-min bars (VWAP calculated)
        Phase 2: Alpaca bars API
```

### get_atr
```
input:  { "ticker": str }
output: {
  "atr_pct": float,             // ATR as % of price (e.g. 1.8 means 1.8%)
  "orb_pct": float | null       // opening range breakout % (first 30 min H-L / open)
}
source: yfinance daily bars (both phases)
```

### get_position_history
```
input:  { "ticker": str, "days": int }  // default days=30
output: {
  "trades": int,
  "wins": int,
  "win_rate_pct": float,
  "avg_pnl": float,
  "last_exit": str | null       // last close reason: TARGET, STOP, EOD, etc.
}
source: Supabase c_positions (simulation uses synthetic data or empty)
```

---

## Risk Agent Tools (4 tools, all required per session)

### get_open_positions
```
input:  {}
output: [
  {
    "ticker": str,
    "position_size": float,
    "entry_price": float,
    "unrealized_pnl": float,
    "sector": str
  }
]
source: Supabase c_positions (both phases)
```

### get_today_pnl
```
input:  {}
output: {
  "realized_pnl": float,
  "trades_closed": int,
  "loss_limit": float,          // DAILY_LOSS_LIMIT from settings
  "limit_hit": bool
}
source: Supabase c_positions (both phases)
```

### get_buying_power
```
input:  {}
output: {
  "buying_power": float,
  "total_capital": float,
  "deployed": float
}
source: Phase 1: TOTAL_CAPITAL - sum(open position sizes from DB)
        Phase 2: Alpaca account.buying_power
```

### get_portfolio_exposure
```
input:  {}
output: {
  "positions_open": int,
  "total_deployed": float,
  "by_sector": { "Technology": float, "Healthcare": float, ... },
  "max_sector_pct": float       // highest single-sector concentration
}
source: Supabase c_positions + sector lookup (both phases)
```

---

## Tool Registration Rules

- Market Agent: registered tools = [get_vix, get_futures, get_fear_greed, get_sector_rotation]
- Research Agent: registered tools = [get_candidates, get_news, get_live_price, get_intraday_signals, get_atr, get_position_history]
- Risk Agent: registered tools = [get_open_positions, get_today_pnl, get_buying_power, get_portfolio_exposure]
- Orchestrator: no tools registered (coordinates agents, does not call tools directly)

An agent cannot call a tool not in its registered set. The Anthropic tool use API
enforces this — if a tool is not in the `tools=[]` parameter, Claude cannot call it.

---

## Tool Call Sequence Example (typical Research Agent session)

```
Agent: calls get_candidates(min_score=5)
  → receives 47 tickers with scores and prices

Agent reasoning: "Top scores: NVDA 9, AAPL 8, MSFT 7, AMD 7, CRWD 6.
  Will investigate NVDA, AAPL, AMD, CRWD, PANW."

Agent: calls get_news(ticker="NVDA")
  → { blackout: true, reason: "earnings 2026-05-28" }
Agent reasoning: "NVDA earnings in 3 days — blackout. Drop. Next: TSLA score 6."

Agent: calls get_news(ticker="AAPL")
  → { blackout: false, headlines: ["Apple supply chain strong..."] }
Agent: calls get_intraday_signals(ticker="AAPL")
  → { above_vwap: true, rs_vs_spy: 1.8, today_pct_change: 0.6 }
Agent: calls get_atr(ticker="AAPL")
  → { atr_pct: 1.2, orb_pct: 0.4 }
Agent: calls get_live_price(ticker="AAPL")
  → { price: 187.42, source: "yfinance", stale_minutes: 1 }

[repeats for AMD, CRWD, PANW, TSLA]

Agent reasoning: "Proposing AAPL (above VWAP, RS 1.8x, clean ATR),
  AMD (volume surge, MACD bullish), CRWD (sector leader).
  Skipping PANW (below VWAP). Skipping TSLA (ATR 3.2% — too wide for stop)."

Agent: returns trade_proposals[AAPL, AMD, CRWD]
Total tool calls: 1 + 5×4 = 21 calls. Within budget.
```
