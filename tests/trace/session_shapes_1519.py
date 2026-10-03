"""argus#1519: one synthetic premarket + EOD session that drives the REAL agent functions with fake models and fake data.

Used by the manifest tests and by the replay into a throwaway Provy workspace. No network, no database, no model: every client is a
MagicMock and every tool is patched where the agent looks it up. The text in here is made of sentinels so a test can prove that none
of it reaches a manifest.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from core.params import StrategyParams
from tests.conftest import make_api_response, text_block, tool_block

SENTINEL_TOOL = "SENTINEL-TOOL-OUTPUT-71a"
SENTINEL_REPLY = "SENTINEL-REPLY-8e2"
SENTINEL_NEWS = "SENTINEL-HEADLINE-5d0"
TICKERS = ("ZZTA", "ZZTB")
MODEL = "claude-haiku-4-5-20251001"

VIX = {"value": 17.0, "level": "LOW", "note": SENTINEL_TOOL}
FUTURES = {"avg_change_pct": 0.4, "bias": "BULLISH", "S&P500": {"change_pct": 0.4}, "Nasdaq": {"change_pct": 0.5}, "Dow": {"change_pct": 0.3}}
CB_VIX = {"value": 38.0, "level": "EXTREME"}
CB_FUTURES = {"avg_change_pct": -2.5, "bias": "BEARISH", "S&P500": {"change_pct": -2.5}, "Nasdaq": {"change_pct": -2.6}, "Dow": {"change_pct": -2.4}}
FEAR = {"value": 55, "label": "Neutral"}
SECTOR_ROT = [{"etf": "XLK", "change_pct": 1.1}]
CALENDAR = {"events": [], "high_impact": False}
YIELDS = {"ten_year": 4.1, "change_bp": 1.0}
MARKET_REPORT = {"decision": "GO", "max_positions": 5, "bias": "BULLISH", "skip_reason": None, "confidence": "HIGH",
                 "key_factors": ["a", "b", "c"], "summary": SENTINEL_REPLY, "vix_level": "LOW"}

SCAN = [{"ticker": TICKERS[0], "technical_score": 8, "price": 185.0, "sector": "Technology"},
        {"ticker": TICKERS[1], "technical_score": 7, "price": 420.0, "sector": "Technology"}]
PREMARKET = [{"ticker": TICKERS[0], "premarket_change_pct": 1.5}, {"ticker": TICKERS[1], "premarket_change_pct": 0.8}]
SECTORS = [{"etf": "XLK", "change_pct": 2.7}, {"etf": "XLF", "change_pct": 0.3}]
SELECT_JSON = {"selected": list(TICKERS), "scan_rationale": SENTINEL_REPLY, "signals_used": ["technical_score"]}

FUNDAMENTALS = {"prev_high": 186.0, "prev_low": 183.0, "note": SENTINEL_TOOL}
MARKET_DATA = {"available": False, "atr_pct": 2.0, "premarket_volume": 1000, "note": SENTINEL_TOOL}
HISTORY = {"trade_count": 2, "win_rate": 0.5}
NEWS = {"blackout": False, "reason": "", "headlines": [SENTINEL_NEWS]}
PROPOSE = {"action": "PROPOSE", "ticker": TICKERS[0], "entry_price": 185.0, "target_price": 199.8, "stop_loss": 183.0,
           "position_size": 3000.0, "confidence": "HIGH", "evidence": [SENTINEL_REPLY], "skip_reason": None}
PROPOSALS = {"proposals": [{k: PROPOSE[k] for k in ("ticker", "entry_price", "target_price", "stop_loss", "position_size", "confidence", "evidence")}],
             "skipped": [], "summary": "one"}

OPEN_POSITIONS: list = []
TODAY_PNL = {"realized_pnl": 0.0, "trades_closed": 0, "loss_limit": -500.0, "limit_hit": False}
BUYING_POWER = {"buying_power": 50000.0, "total_capital": 50000.0, "deployed": 0.0}
EXPOSURE = {"positions_open": 0, "total_deployed": 0.0, "by_sector": {}, "max_sector_pct": 0.0}
VERDICTS = {"verdicts": [{"ticker": TICKERS[0], "verdict": "APPROVED", "reason": SENTINEL_REPLY}],
            "portfolio_state": {"buying_power": 50000.0, "positions_open": 0, "today_pnl": 0.0, "limit_hit": False}}
SYNTH = {"date": "2026-10-01", "market_context": "x", "trades": [], "total_estimated_profit": 12.5, "total_max_loss": 6.0,
         "risk_note": SENTINEL_REPLY, "retry_needed": False, "session_meta": {"terminal_reason": "converged"}}

TRADES = [{"ticker": TICKERS[0], "realized_pnl": 100.0, "exit_reason": "TARGET", "entry_price": 185.0, "exit_price": 187.5}]
SESSION = {"id": "sess-1", "terminal_reason": "converged", "total_steps": 20}
PARAMS = [{"param_name": "strategy_min_score", "current_value": 5, "min_value": 3, "max_value": 9, "cooldown_until": None}]
LEARNINGS = [{"learning_date": "2026-09-29", "finding": SENTINEL_TOOL, "learning_type": "observation"},
             {"learning_date": "2026-09-30", "finding": "older", "learning_type": "observation"}]
WRITE_OK = {"status": "written", "id": "abc-123"}
SUMMARY = {"session_date": "2026-10-01", "trades_analyzed": 1, "win_rate": 1.0, "total_pnl": 100.0, "learnings_written": 1,
           "params_adjusted": 0, "goal_recommended": False, "top_finding": SENTINEL_REPLY, "context_for_tomorrow": "x"}


def resp(stop_reason, blocks, inp=200, out=100):
    r = make_api_response(stop_reason, blocks, inp, out)
    r.model = MODEL
    r.usage = NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return r


def _client(*responses):
    c = MagicMock()
    c.messages.create.side_effect = list(responses)
    return c


def run_market(tracer, vix=VIX, futures=FUTURES):
    from agents.market_agent import run_market_agent
    tools = [tool_block(n, {}, f"m{i}") for i, n in enumerate(
        ("get_vix", "get_futures", "get_fear_greed", "get_sector_rotation", "get_economic_calendar", "get_treasury_yields"))]
    client = _client(resp("tool_use", tools), resp("end_turn", [text_block(json.dumps(MARKET_REPORT))]))
    with patch("agents.market_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.market_agent.get_vix", return_value=vix), patch("agents.market_agent.get_futures", return_value=futures), \
         patch("agents.market_agent.get_fear_greed", return_value=FEAR), patch("agents.market_agent.get_sector_rotation", return_value=SECTOR_ROT), \
         patch("agents.market_agent.get_economic_calendar", return_value=CALENDAR), patch("agents.market_agent.get_treasury_yields", return_value=YIELDS):
        return run_market_agent(tracer, StrategyParams())


def run_scanner(tracer, scan=SCAN):
    from agents.scanner_agent import run_scanner_agent
    client = _client(resp("end_turn", [text_block(json.dumps(SELECT_JSON))]))
    with patch("agents.scanner_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.scanner_agent.get_scan_results", return_value=scan), patch("agents.scanner_agent.get_premarket_snapshot", return_value=PREMARKET), \
         patch("agents.scanner_agent.get_gap_ups", return_value=[]), patch("agents.scanner_agent.get_sector_leaders", return_value=SECTORS):
        return run_scanner_agent(tracer, MARKET_REPORT, StrategyParams())


def run_research(tracer, ticker, news=NEWS):
    from agents.research_agent import _investigate_ticker
    tools = [tool_block("get_ticker_fundamentals", {"ticker": ticker}, "r1"), tool_block("get_ticker_market_data", {"ticker": ticker}, "r2"),
             tool_block("get_position_history", {"ticker": ticker}, "r3")]
    client = _client(resp("tool_use", tools), resp("end_turn", [text_block(json.dumps({**PROPOSE, "ticker": ticker}))]))
    ctx = {"score": 8, "premarket_change_pct": 1.5, "scanner_price": 185.0}
    with patch("agents.research_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.research_agent.get_ticker_fundamentals", return_value=FUNDAMENTALS), \
         patch("agents.research_agent.get_ticker_market_data", return_value=MARKET_DATA), \
         patch("agents.research_agent.get_position_history", return_value=HISTORY):
        return _investigate_ticker(ticker, ctx, MARKET_REPORT, tracer, news)


def run_risk(tracer):
    from agents.risk_agent import run_risk_agent
    tools = [tool_block(n, {}, f"k{i}") for i, n in enumerate(("get_open_positions", "get_today_pnl", "get_buying_power", "get_portfolio_exposure"))]
    client = _client(resp("tool_use", tools), resp("end_turn", [text_block(json.dumps(VERDICTS))]))
    with patch("agents.risk_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.risk_agent.get_open_positions", return_value=OPEN_POSITIONS), patch("agents.risk_agent.get_today_pnl", return_value=TODAY_PNL), \
         patch("agents.risk_agent.get_buying_power", return_value=BUYING_POWER), patch("agents.risk_agent.get_portfolio_exposure", return_value=EXPOSURE):
        return run_risk_agent(tracer, PROPOSALS, StrategyParams())


def run_orchestrator(tracer):
    from agents.orchestrator import _run_synthesis_call
    client = _client(resp("end_turn", [text_block(json.dumps(SYNTH))]))
    return _run_synthesis_call(client, MARKET_REPORT, PROPOSALS, VERDICTS, tracer, loop_iteration=1)


def run_learner(tracer, learnings=LEARNINGS):
    from agents.learning_agent import run_learning_agent
    reads = [tool_block("read_today_trades", {}, "l1"), tool_block("read_session_context", {"session_id": "sess-1"}, "l2"),
             tool_block("read_strategy_params", {}, "l3"), tool_block("read_recent_learnings", {}, "l4"),
             tool_block("write_learning", {"learning_type": "observation", "dimension": "entry_quality", "finding": SENTINEL_REPLY}, "l5")]
    client = _client(resp("tool_use", reads), resp("end_turn", [text_block(json.dumps(SUMMARY))]))
    with patch("agents.learning_agent.anthropic.Anthropic", return_value=client), \
         patch("agents.learning_agent.read_today_trades", return_value=TRADES), patch("agents.learning_agent.read_session_context", return_value=SESSION), \
         patch("agents.learning_agent.read_strategy_params", return_value=PARAMS), patch("agents.learning_agent.read_recent_learnings", return_value=learnings), \
         patch("agents.learning_agent.write_learning", return_value=WRITE_OK), patch("agents.learning_agent.adjust_param", return_value={"status": "applied"}), \
         patch("agents.learning_agent.recommend_goal", return_value=WRITE_OK):
        return run_learning_agent(tracer, "sess-1", StrategyParams())


def run_session(tracer) -> dict:
    """Premarket in pipeline order, then the EOD learner. Returns each step's result so a test can compare runs."""
    out = {}
    out["market"] = run_market(tracer)
    out["scanner"] = run_scanner(tracer)
    out["research"] = [run_research(tracer, t) for t in TICKERS]
    out["risk"] = run_risk(tracer)
    out["orchestrator"] = run_orchestrator(tracer)
    out["learner"] = run_learner(tracer)
    return out


def stable_attrs(spans) -> list:
    """Every span's attributes in emission order, minus the one clock-dependent value, so two runs can be compared byte for byte."""
    rows = []
    for s in spans:
        a = {k: v for k, v in dict(s.attributes or {}).items() if k != "argus.latency_ms"}
        rows.append({"name": s.name, "attrs": {k: (v if isinstance(v, (str, int, float, bool)) else repr(v)) for k, v in sorted(a.items())},
                     "status": s.status.status_code.value if s.status else 0})
    return rows
