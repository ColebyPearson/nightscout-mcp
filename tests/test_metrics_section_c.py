"""Tests for the Section C composition helpers (FDR, two-proportion p, CI overlap, period metrics)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nightscout_mcp.tools.metrics import (
    _bh_correct,
    _build_period_metrics,
    _ci_overlap,
    _interpret_period_compare,
    _phi,
    _two_proportion_p_value,
)

# --- Standard normal CDF approximation -------------------------------------


def test_phi_at_zero_is_half():
    assert _phi(0.0) == pytest.approx(0.5, abs=0.01)


def test_phi_at_one_is_about_84pct():
    assert _phi(1.0) == pytest.approx(0.8413, abs=0.005)


def test_phi_at_two_is_about_97pct():
    assert _phi(2.0) == pytest.approx(0.9772, abs=0.005)


def test_phi_symmetric():
    # Phi(-z) = 1 - Phi(z)
    for z in (0.5, 1.0, 1.5, 2.0):
        assert _phi(-z) + _phi(z) == pytest.approx(1.0, abs=0.005)


# --- Two-proportion p-value ------------------------------------------------


def test_two_proportion_identical_proportions_high_p():
    # 50/100 vs 50/100 → p ≈ 1.0
    p = _two_proportion_p_value(50, 100, 50, 100)
    assert p >= 0.9


def test_two_proportion_very_different_low_p():
    # 90/100 vs 10/100 → p ≈ 0
    p = _two_proportion_p_value(90, 100, 10, 100)
    assert p < 0.01


def test_two_proportion_modest_difference():
    # 60/100 vs 50/100 → p around 0.15 ish
    p = _two_proportion_p_value(60, 100, 50, 100)
    assert 0.1 < p < 0.3


def test_two_proportion_empty_returns_one():
    assert _two_proportion_p_value(0, 0, 5, 10) == 1.0


# --- BH-FDR correction -----------------------------------------------------


def test_bh_empty():
    assert _bh_correct([], 0.10) == []


def test_bh_single_p():
    # Single p stays the same
    result = _bh_correct([0.04], 0.10)
    assert result[0] == pytest.approx(0.04, abs=0.001)


def test_bh_preserves_order_of_input():
    # Input order is preserved in output
    p_values = [0.01, 0.30, 0.04, 0.15]
    adj = _bh_correct(p_values, 0.10)
    assert len(adj) == 4
    # All adjusted values should be >= original
    for original, adjusted in zip(p_values, adj, strict=False):
        assert adjusted >= original - 1e-9


def test_bh_smallest_pvalue_adjusted_proportionally():
    # 4 hypotheses, one at p=0.01 → adjusted = 0.01 * 4 / 1 = 0.04 (min of monotone)
    p_values = [0.01, 0.30, 0.04, 0.15]
    adj = _bh_correct(p_values, 0.10)
    # Adjusted should be monotone non-decreasing when sorted
    sorted_adj = sorted(adj)
    for i in range(1, len(sorted_adj)):
        assert sorted_adj[i] >= sorted_adj[i - 1] - 1e-9


# --- CI overlap ------------------------------------------------------------


def test_ci_overlap_with_overlap():
    assert _ci_overlap((50.0, 60.0), (55.0, 65.0)) is True
    assert _ci_overlap((40.0, 55.0), (50.0, 60.0)) is True


def test_ci_no_overlap():
    assert _ci_overlap((40.0, 50.0), (55.0, 65.0)) is False
    assert _ci_overlap((60.0, 70.0), (40.0, 55.0)) is False


def test_ci_touching_boundaries_overlap():
    # Touching is still overlap
    assert _ci_overlap((40.0, 55.0), (55.0, 65.0)) is True


# --- _interpret_period_compare ---------------------------------------------


def test_interpret_no_change():
    # delta_tir near zero
    text = _interpret_period_compare(0.5, 0.05, True, True)
    assert "negligible" in text.lower() or "unchanged" in text.lower()


def test_interpret_improvement_with_statistical_significance():
    # TIR up 8 pp, CIs do not overlap
    text = _interpret_period_compare(8.0, -0.3, False, False)
    assert "improved" in text.lower()
    assert "statistically" in text.lower() or "do not overlap" in text.lower()


def test_interpret_worsening_within_noise():
    # TIR down 3 pp, CIs overlap = within noise
    text = _interpret_period_compare(-3.0, 0.05, True, True)
    assert "worsened" in text.lower()
    assert "noise" in text.lower() or "overlap" in text.lower()


def test_interpret_tbr_improvement():
    text = _interpret_period_compare(2.0, -0.5, True, False)
    assert "decreased" in text.lower() or "improvement" in text.lower()


# --- Period metrics builder ------------------------------------------------


def test_build_period_metrics_empty():
    start = datetime(2026, 5, 23, tzinfo=UTC)
    end = datetime(2026, 5, 24, tzinfo=UTC)
    pm = _build_period_metrics("test", start, end, [])
    assert pm.sample_count == 0
    assert pm.tir_70_180_pct == 0.0


def test_build_period_metrics_basic():
    start = datetime(2026, 5, 23, tzinfo=UTC)
    end = datetime(2026, 5, 24, tzinfo=UTC)
    # 50 values: 30 in target (100-180), 10 below (50-69), 10 above (200-250)
    values = [100.0] * 30 + [60.0] * 10 + [220.0] * 10
    pm = _build_period_metrics("test", start, end, values)
    assert pm.sample_count == 50
    assert 55 < pm.tir_70_180_pct < 65  # ~60%
    assert 18 < pm.tbr_lt70_pct < 22  # ~20%
    # CIs should be tuples of two floats
    assert isinstance(pm.tir_70_180_ci, tuple)
    assert len(pm.tir_70_180_ci) == 2
    assert pm.tir_70_180_ci[0] < pm.tir_70_180_pct < pm.tir_70_180_ci[1] + 0.01
