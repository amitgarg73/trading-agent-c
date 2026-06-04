"""
Regression tests for the conviction gate relaxation in research_agent.py.

The rule change: conviction LOW is now acceptable at MEDIUM confidence
when above_vwap=true AND rs_vs_spy >= 1.0. Previously conviction LOW
always resulted in SKIP for pre-market candidates regardless of price action.

These tests verify the logic the agent prompt encodes — they test the
decision rules directly rather than calling the LLM.
"""
from __future__ import annotations


def _confidence_rule(
    score: int,
    above_vwap: bool | None,
    rs_vs_spy: float | None,
    today_pct_change: float,
    premarket_change_pct: float | None,
    conviction: str,
    is_premarket: bool,
) -> str:
    """
    Replicates the confidence rules from _INVESTIGATE_SYSTEM prompt.
    Returns "HIGH", "MEDIUM", "LOW", or "SKIP".
    """
    # SKIP rules (hard)
    if today_pct_change > 4:
        return "SKIP"

    if is_premarket:
        # Pre-market HIGH: score >= 7, positive premarket_change, conviction HIGH or MODERATE
        if (score >= 7
                and (premarket_change_pct or 0) > 0.5
                and conviction in ("HIGH", "MODERATE")):
            return "HIGH"

        # Pre-market MEDIUM: score 6-7, positive premarket_change,
        # conviction MODERATE+, OR conviction LOW with VWAP+RS confirmation
        if score >= 6 and (premarket_change_pct or 0) > 0:
            if conviction in ("MODERATE", "HIGH"):
                return "MEDIUM"
            if (conviction == "LOW"
                    and above_vwap is True
                    and (rs_vs_spy or 0) >= 1.0):
                return "MEDIUM"

        # LOW: score 5-6, conviction LOW acceptable if VWAP or RS confirms
        if score >= 5 and (above_vwap or (rs_vs_spy or 0) >= 0.8):
            return "LOW"

        return "SKIP"

    else:
        # Intraday HIGH
        if (score >= 7
                and above_vwap is True
                and (rs_vs_spy or 0) >= 1.5
                and today_pct_change <= 2):
            return "HIGH"

        # Intraday MEDIUM
        if (score >= 5
                and above_vwap is True
                and (rs_vs_spy or 0) >= 0.8
                and today_pct_change <= 4):
            return "MEDIUM"

        # LOW
        if score >= 5 and today_pct_change <= 4:
            return "LOW"

        return "SKIP"


class TestConvictionRelaxation:
    """Pre-market scenarios that were SKIP before the change, MEDIUM after."""

    def test_mpc_scenario_now_medium(self):
        """MPC on June 4: score=6, +4.68%, above_vwap=True, rs=3.84, conviction=LOW.
        Old rule: SKIP (conviction not LOW required). New rule: MEDIUM."""
        result = _confidence_rule(
            score=6, above_vwap=True, rs_vs_spy=3.84,
            today_pct_change=0.6, premarket_change_pct=4.68,
            conviction="LOW", is_premarket=True,
        )
        assert result == "MEDIUM", f"Expected MEDIUM, got {result}"

    def test_conviction_low_vwap_false_still_skip(self):
        """conviction=LOW + above_vwap=False should still SKIP — no price confirmation."""
        result = _confidence_rule(
            score=6, above_vwap=False, rs_vs_spy=0.5,
            today_pct_change=0.3, premarket_change_pct=2.0,
            conviction="LOW", is_premarket=True,
        )
        assert result == "SKIP", f"Expected SKIP, got {result}"

    def test_conviction_low_rs_below_threshold_not_medium(self):
        """conviction=LOW + rs < 1.0 → cannot reach MEDIUM (RS too weak for confirmation)."""
        result = _confidence_rule(
            score=6, above_vwap=True, rs_vs_spy=0.5,
            today_pct_change=0.3, premarket_change_pct=2.0,
            conviction="LOW", is_premarket=True,
        )
        assert result != "MEDIUM", f"Should not reach MEDIUM with rs=0.5"

    def test_conviction_moderate_still_medium(self):
        """conviction=MODERATE + good setup → MEDIUM (unchanged)."""
        result = _confidence_rule(
            score=6, above_vwap=True, rs_vs_spy=1.2,
            today_pct_change=0.5, premarket_change_pct=1.5,
            conviction="MODERATE", is_premarket=True,
        )
        assert result == "MEDIUM", f"Expected MEDIUM, got {result}"

    def test_conviction_high_still_high(self):
        """conviction=HIGH + score >= 7 → HIGH (unchanged)."""
        result = _confidence_rule(
            score=7, above_vwap=True, rs_vs_spy=2.0,
            today_pct_change=0.5, premarket_change_pct=2.0,
            conviction="HIGH", is_premarket=True,
        )
        assert result == "HIGH", f"Expected HIGH, got {result}"

    def test_negative_premarket_change_not_medium(self):
        """Negative premarket change cannot reach MEDIUM confidence."""
        result = _confidence_rule(
            score=6, above_vwap=True, rs_vs_spy=1.5,
            today_pct_change=0.3, premarket_change_pct=-1.0,
            conviction="MODERATE", is_premarket=True,
        )
        assert result != "MEDIUM", f"Should not reach MEDIUM with negative premarket move"

    def test_score_too_low_skips(self):
        """Score < 6 in pre-market → can only reach LOW or SKIP."""
        result = _confidence_rule(
            score=4, above_vwap=True, rs_vs_spy=2.0,
            today_pct_change=0.3, premarket_change_pct=3.0,
            conviction="LOW", is_premarket=True,
        )
        assert result in ("LOW", "SKIP")

    def test_amt_june4_scenario_low(self):
        """AMT June 4: score=7, conviction=LOW, above_vwap=False, rs=-2.78.
        Should still SKIP — RS is deeply negative, no confirmation."""
        result = _confidence_rule(
            score=7, above_vwap=False, rs_vs_spy=-2.78,
            today_pct_change=-0.43, premarket_change_pct=1.92,
            conviction="LOW", is_premarket=True,
        )
        assert result == "SKIP", f"Expected SKIP, got {result}"


class TestIntraday:

    def test_strong_intraday_setup_high(self):
        result = _confidence_rule(
            score=7, above_vwap=True, rs_vs_spy=2.0,
            today_pct_change=1.0, premarket_change_pct=None,
            conviction="LOW", is_premarket=False,
        )
        assert result == "HIGH"

    def test_moderate_intraday_medium(self):
        result = _confidence_rule(
            score=5, above_vwap=True, rs_vs_spy=1.0,
            today_pct_change=1.5, premarket_change_pct=None,
            conviction="LOW", is_premarket=False,
        )
        assert result == "MEDIUM"

    def test_extended_stock_skips(self):
        result = _confidence_rule(
            score=7, above_vwap=True, rs_vs_spy=2.0,
            today_pct_change=5.0, premarket_change_pct=None,
            conviction="HIGH", is_premarket=False,
        )
        assert result == "SKIP"
