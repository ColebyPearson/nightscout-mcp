"""Tests for glucose statistics. Pure math, no network."""

from __future__ import annotations

from nightscout_mcp.models import Sgv
from nightscout_mcp.stats import compute_stats, gmi_percent


def _sgv(mgdl: int, i: int = 0) -> Sgv:
    """Minimal SGV factory — only the fields stats reads."""
    return Sgv.model_validate(
        {
            "sgv": mgdl,
            "date": 1_700_000_000_000 + i * 300_000,
            "dateString": "2026-05-22T12:00:00.000Z",
            "type": "sgv",
        }
    )


def test_gmi_formula_matches_published_constants() -> None:
    # Bergenstal 2018: GMI(%) = 3.31 + 0.02392 × mean_mgdl
    # Mean 154 mg/dL → ~7.0% A1C-equivalent
    assert abs(gmi_percent(154) - 6.99) < 0.05


def test_empty_input_returns_zero_filled_stats_not_error() -> None:
    s = compute_stats([], window_hours=24)
    assert s.reading_count == 0
    assert s.mean_mgdl == 0.0
    assert s.tir_percent == 0.0


def test_in_range_all_100_means_100_pct_tir() -> None:
    readings = [_sgv(100, i) for i in range(20)]
    s = compute_stats(readings, window_hours=2)
    assert s.reading_count == 20
    assert s.mean_mgdl == 100.0
    assert s.tir_percent == 100.0
    assert s.tbr_lt70_percent == 0.0
    assert s.tar_gt180_percent == 0.0
    assert s.sd_mgdl == 0.0
    assert s.cv_percent == 0.0


def test_mixed_tir_bands_sum_correctly() -> None:
    # 5 lows (<70), 10 in-range, 5 highs (>180)
    readings = (
        [_sgv(60, i) for i in range(5)]
        + [_sgv(120, i + 5) for i in range(10)]
        + [_sgv(220, i + 15) for i in range(5)]
    )
    s = compute_stats(readings, window_hours=2)
    assert s.reading_count == 20
    assert s.tbr_lt70_percent == 25.0
    assert s.tir_percent == 50.0
    assert s.tar_gt180_percent == 25.0


def test_severe_lows_are_a_subset_of_lows() -> None:
    import pytest as _pt

    readings = [_sgv(50, 0), _sgv(60, 1), _sgv(120, 2)]
    s = compute_stats(readings, window_hours=1)
    # 1 of 3 is <54, 2 of 3 are <70
    assert s.tbr_lt54_percent == _pt.approx(33.3, abs=0.1)
    assert s.tbr_lt70_percent == _pt.approx(66.7, abs=0.1)


def test_custom_thresholds_are_respected() -> None:
    readings = [_sgv(100, i) for i in range(10)]  # all 100 mg/dL
    s = compute_stats(readings, window_hours=1, tir_low=90, tir_high=110)
    assert s.tir_percent == 100.0
    assert s.tir_low_threshold_mgdl == 90
    assert s.tir_high_threshold_mgdl == 110


def test_non_sgv_entries_excluded() -> None:
    # An MBG (meter blood glucose) entry should not contribute to SGV stats.
    sgv_rows = [_sgv(100, i) for i in range(3)]
    mbg = Sgv.model_validate(
        {
            "sgv": 250,
            "date": 1_700_000_000_000,
            "dateString": "2026-05-22T12:00:00.000Z",
            "type": "mbg",  # not sgv
        }
    )
    s = compute_stats([*sgv_rows, mbg], window_hours=1)
    assert s.reading_count == 3
    assert s.mean_mgdl == 100.0  # mbg value didn't pollute the mean
