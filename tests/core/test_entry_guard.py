"""Tests for the entry-chase guard (core/scoring.entry_premium_pct + is_chasing_entry).

The guard skips entries that have already run up off the day's open, which the
execution analysis (design/why-we-are-losing-plain-english.md) showed is the
dominant source of the realized P&L drag.
"""
import pytest

from core.scoring import entry_premium_pct, is_chasing_entry


# ── entry_premium_pct ────────────────────────────────────────────────────────

def test_premium_above_open_is_positive():
    assert entry_premium_pct(101.4, 100.0) == pytest.approx(0.014)


def test_premium_below_open_is_negative():
    assert entry_premium_pct(99.0, 100.0) == pytest.approx(-0.01)


def test_premium_at_open_is_zero():
    assert entry_premium_pct(100.0, 100.0) == 0.0


@pytest.mark.parametrize("price,day_open", [
    (101.0, 0.0),      # no open
    (101.0, None),     # missing open
    (101.0, -5.0),     # invalid open
    (0.0, 100.0),      # no price
    (None, 100.0),     # missing price
])
def test_premium_none_on_missing_or_invalid(price, day_open):
    assert entry_premium_pct(price, day_open) is None


# ── is_chasing_entry ─────────────────────────────────────────────────────────

def test_chasing_when_above_threshold():
    # +1.0% above open, max 0.5% → chasing
    assert is_chasing_entry(101.0, 100.0, 0.005) is True


def test_not_chasing_when_below_threshold():
    # +0.3% above open, max 0.5% → allowed
    assert is_chasing_entry(100.3, 100.0, 0.005) is False


def test_threshold_is_strict_greater_than():
    # exactly +0.5% above open is allowed (only strictly above is blocked)
    assert is_chasing_entry(100.5, 100.0, 0.005) is False


def test_not_chasing_when_below_open():
    assert is_chasing_entry(99.0, 100.0, 0.005) is False


def test_guard_disabled_when_max_premium_zero():
    # zero or negative max_premium disables the guard entirely
    assert is_chasing_entry(110.0, 100.0, 0.0) is False
    assert is_chasing_entry(110.0, 100.0, -0.01) is False


@pytest.mark.parametrize("day_open", [None, 0.0, -1.0])
def test_guard_fails_safe_on_unknown_open(day_open):
    # unknown/invalid open must never block an entry (premarket, data error)
    assert is_chasing_entry(101.0, day_open, 0.005) is False


def test_realistic_observed_late_entry_is_blocked():
    # the +1.4% above open we actually observed would be rejected at the 0.5% gate
    assert is_chasing_entry(101.4, 100.0, 0.005) is True


def test_realistic_near_open_entry_is_allowed():
    # entering near the open (the fix's goal) is allowed
    assert is_chasing_entry(100.4, 100.0, 0.005) is False


# ── live default threshold (recalibrated 2026-07-21: 0.5% → 2%) ───────────────

def test_default_max_entry_premium_is_two_percent():
    # 0.5% was calibrated for the buy-at-open path; once that was rolled back it
    # rejected ~every intraday entry. The live default is 2%.
    from core.params import PARAM_DEFAULTS, StrategyParams
    assert PARAM_DEFAULTS["max_entry_premium"] == 0.02
    assert StrategyParams().max_entry_premium == 0.02


def test_normal_intraday_entry_allowed_at_two_percent():
    # the +1.0-1.8% off-open picks we observed being rejected at 0.5% are now allowed
    assert is_chasing_entry(101.1, 100.0, 0.02) is False   # NET +1.1%
    assert is_chasing_entry(101.7, 100.0, 0.02) is False   # UPS +1.7%


def test_runaway_still_blocked_at_two_percent():
    # names that already ran >2% off the open are still refused (no wild chasing)
    assert is_chasing_entry(104.2, 100.0, 0.02) is True    # +4.2%
    assert is_chasing_entry(108.5, 100.0, 0.02) is True    # +8.5%
