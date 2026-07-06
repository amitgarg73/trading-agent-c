from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agents.scanner_agent import (
    run_scanner_agent, _regime_bounds, _gather_and_rank, _select_message,
)
from core.params import StrategyParams
from tests.conftest import make_api_response, text_block


_MARKET_GO = {
    "decision": "GO", "max_positions": 5, "bias": "BULLISH",
    "vix_value": 18.5, "vix_level": "ELEVATED", "skip_reason": None, "summary": "Clean open.",
}
_MARKET_CAUTION = {
    "decision": "CAUTION", "max_positions": 2, "bias": "NEUTRAL",
    "vix_value": 27.0, "vix_level": "HIGH", "skip_reason": None, "summary": "High VIX.",
}

# get_scan_results already maps score->technical_score, so fixtures use technical_score.
_SCAN = [
    {"ticker": "AAPL", "technical_score": 8, "price": 185.0, "sector": "Technology"},
    {"ticker": "MSFT", "technical_score": 7, "price": 420.0, "sector": "Technology"},
]
_PREMARKET = [
    {"ticker": "AAPL", "premarket_change_pct": 1.5},
    {"ticker": "MSFT", "premarket_change_pct": 0.8},
]
_SECTORS = [{"etf": "XLK", "change_pct": 2.7}, {"etf": "XLF", "change_pct": 0.3}]

_SELECT_JSON = json.dumps({
    "selected": ["AAPL", "MSFT"],
    "scan_rationale": "Tech leading; two high-score momentum names.",
    "signals_used": ["technical_score", "premarket_momentum", "sector_rotation"],
})


def _client(select_json: str | None = None, raises: Exception | None = None) -> MagicMock:
    c = MagicMock()
    if raises is not None:
        c.messages.create.side_effect = raises
    else:
        c.messages.create.return_value = make_api_response("end_turn", [text_block(select_json)])
    return c


def _patch_tools(scan=_SCAN, premarket=_PREMARKET, gaps=None, sectors=_SECTORS, scan_exc=None):
    """Patch the four data tools in the scanner namespace; filter_and_rank runs for real."""
    gaps = gaps if gaps is not None else []
    def scan_fn(_min_score):
        if scan_exc is not None:
            raise scan_exc
        return scan
    return [
        patch("agents.scanner_agent.get_scan_results", side_effect=scan_fn),
        patch("agents.scanner_agent.get_premarket_snapshot", return_value=premarket),
        patch("agents.scanner_agent.get_gap_ups", return_value=gaps),
        patch("agents.scanner_agent.get_sector_leaders", return_value=sectors),
    ]


def _run(tracer, client, **tool_kwargs):
    market = tool_kwargs.pop("market", _MARKET_GO)
    patches = _patch_tools(**tool_kwargs) + [
        patch("agents.scanner_agent.anthropic.Anthropic", return_value=client)
    ]
    for p in patches:
        p.start()
    try:
        return run_scanner_agent(tracer, market, StrategyParams())
    finally:
        for p in patches:
            p.stop()


class TestRegimeBounds:
    def test_caution_overrides_vix(self):
        assert _regime_bounds({"decision": "CAUTION", "vix_level": "LOW"}) == (7, 15, True, "caution")

    def test_high_vix(self):
        assert _regime_bounds({"decision": "GO", "vix_level": "HIGH"}) == (7, 15, False, "high_vix")

    def test_low_vix(self):
        assert _regime_bounds({"decision": "GO", "vix_level": "LOW"}) == (5, 25, False, "low_vix")

    def test_elevated_default(self):
        assert _regime_bounds({"decision": "GO", "vix_level": "ELEVATED"}) == (5, 20, False, "elevated_vix")


class TestGatherAndRank:
    def test_merges_and_ranks_deterministically(self, tracer):
        with patch("agents.scanner_agent.get_scan_results", return_value=_SCAN), \
             patch("agents.scanner_agent.get_premarket_snapshot", return_value=_PREMARKET), \
             patch("agents.scanner_agent.get_gap_ups", return_value=[]), \
             patch("agents.scanner_agent.get_sector_leaders", return_value=_SECTORS):
            g = _gather_and_rank(tracer, _MARKET_GO)
        assert [c["ticker"] for c in g["ranked"]] == ["AAPL", "MSFT"]   # sorted by score desc
        assert g["regime"] == "elevated_vix"
        assert g["ranked"][0]["premarket_change_pct"] == 1.5            # enriched from premarket

    def test_adds_gap_ups_not_in_scan(self, tracer):
        gaps = [{"ticker": "TSLA", "gap_pct": 4.2, "price": 250.0, "sector": "Consumer"}]
        with patch("agents.scanner_agent.get_scan_results", return_value=_SCAN), \
             patch("agents.scanner_agent.get_premarket_snapshot", return_value=_PREMARKET), \
             patch("agents.scanner_agent.get_gap_ups", return_value=gaps), \
             patch("agents.scanner_agent.get_sector_leaders", return_value=_SECTORS):
            g = _gather_and_rank(tracer, _MARKET_GO)
        tsla = next((c for c in g["ranked"] if c["ticker"] == "TSLA"), None)
        assert tsla is not None
        assert tsla["technical_score"] == 6                            # gap-up-only default score
        assert tsla["premarket_change_pct"] == 4.2

    def test_caps_premarket_enrichment_breadth(self, tracer):
        big = [{"ticker": f"T{i}", "technical_score": 6, "price": 10.0, "sector": "X"} for i in range(60)]
        snap = MagicMock(return_value=[])
        with patch("agents.scanner_agent.get_scan_results", return_value=big), \
             patch("agents.scanner_agent.get_premarket_snapshot", snap), \
             patch("agents.scanner_agent.get_gap_ups", return_value=[]), \
             patch("agents.scanner_agent.get_sector_leaders", return_value=_SECTORS):
            _gather_and_rank(tracer, _MARKET_GO)
        assert len(snap.call_args.args[0]) == 40                       # top-40 cap on enrichment


class TestRunScannerAgent:
    def test_happy_path_uses_llm_pick(self, tracer):
        result = _run(tracer, _client(_SELECT_JSON))
        assert result["scanner_status"] == "ok"
        assert [c["ticker"] for c in result["candidates"]] == ["AAPL", "MSFT"]
        assert result["scan_rationale"].startswith("Tech leading")

    def test_span_started(self, tracer):
        _run(tracer, _client(_SELECT_JSON))
        assert tracer.get_agent_span("scanner") is not None

    def test_llm_failure_falls_back_to_deterministic(self, tracer):
        # A slow/failed model must never kill the day — deterministic ranking still returns.
        result = _run(tracer, _client(raises=RuntimeError("model down")))
        assert result["scanner_status"] == "llm_fallback"
        assert [c["ticker"] for c in result["candidates"]] == ["AAPL", "MSFT"]

    def test_llm_invalid_selection_falls_back(self, tracer):
        bad = json.dumps({"selected": ["ZZZZ"], "scan_rationale": "x", "signals_used": []})
        result = _run(tracer, _client(bad))
        assert result["scanner_status"] == "llm_fallback"
        assert len(result["candidates"]) == 2

    def test_gather_failure_returns_scanner_error(self, tracer):
        result = _run(tracer, _client(_SELECT_JSON), scan_exc=RuntimeError("db down"))
        assert result["scanner_status"] == "error"
        assert result["candidates"] == []
        assert result["n_returned"] == 0

    def test_no_candidates_is_benign_halt(self, tracer):
        result = _run(tracer, _client(_SELECT_JSON), scan=[], premarket=[])
        assert result["scanner_status"] == "ok"
        assert result["candidates"] == []
        assert result["n_returned"] == 0

    def test_llm_pick_respects_max_n(self, tracer):
        # CAUTION regime caps max_n at 15; a shortlist of 2 is well under, both selected.
        result = _run(tracer, _client(_SELECT_JSON), market=_MARKET_CAUTION)
        assert result["regime"] == "caution"
        assert len(result["candidates"]) <= 15


class TestSelectMessage:
    def test_includes_shortlist_and_regime(self):
        msg = _select_message(_SCAN, _SECTORS, _MARKET_GO, "elevated_vix", 20)
        assert "AAPL" in msg and "elevated_vix" in msg and "XLK" in msg
