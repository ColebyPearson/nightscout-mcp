"""Tests for the metrics module (Klonoff 2023 GRI, Kovatchev 1998 LBGI/HBGI, etc.).

Each test verifies the formula against either a textbook reference value, a
synthetic distribution with known expected output, or an analytical property
the formula must satisfy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nightscout_mcp.metrics import (
    CV_TARGET_PERCENT,
    adrr,
    agp_hourly_percentiles,
    cogi,
    conga,
    cusum_change_points,
    cv_percent,
    exponential_iob_fraction,
    fit_dia_to_residuals,
    gmi_percent,
    gri,
    gvp,
    hbgi_band,
    hourly_aggregate,
    j_index,
    lbgi_band,
    lbgi_hbgi,
    m_value,
    mage,
    modd,
    partition_by_local_hour,
    percentile,
    wilson_ci_95,
)

# --- Wilson CI ---------------------------------------------------------------


def test_wilson_ci_95_zero_total_returns_zero():
    assert wilson_ci_95(0, 0) == (0.0, 0.0)


def test_wilson_ci_95_known_values():
    # 50/100 → about (40.4, 59.6) for Wilson 95%
    lo, hi = wilson_ci_95(50, 100)
    assert 40.0 < lo < 41.0
    assert 59.0 < hi < 60.0


def test_wilson_ci_95_extreme_proportions():
    # 1/100 should have a TIGHT lower bound (near zero) but not exactly zero
    lo, hi = wilson_ci_95(1, 100)
    assert 0 <= lo < 1
    assert 4.0 < hi < 6.0  # Wilson upper for 1/100 is ~5.4%


# --- GMI --------------------------------------------------------------------


def test_gmi_textbook_example():
    # Bergenstal 2018: mean 154 mg/dL → GMI 7.0%
    assert gmi_percent(154) == pytest.approx(7.0, abs=0.05)
    # mean 200 → GMI ~ 8.1
    assert gmi_percent(200) == pytest.approx(8.09, abs=0.05)


# --- CV ---------------------------------------------------------------------


def test_cv_empty_returns_zero():
    assert cv_percent([]) == 0.0


def test_cv_constant_series_is_zero():
    assert cv_percent([100, 100, 100, 100]) == 0.0


def test_cv_known_value():
    # 100 ± SD 20 → CV = 20%
    values = [80, 90, 100, 110, 120]
    cv = cv_percent(values)
    # mean=100, SD~15.81 → CV ~15.8%
    assert 15.0 < cv < 17.0


def test_cv_target_threshold_constant():
    assert CV_TARGET_PERCENT == 36.0


# --- J-index ----------------------------------------------------------------


def test_j_index_known_value():
    # mean=100, SD~15.81 → J = 0.001 × (100 + 15.81)² ~ 13.41
    values = [80, 90, 100, 110, 120]
    j = j_index(values)
    assert 13.0 < j < 14.0


def test_j_index_empty():
    assert j_index([]) == 0.0
    assert j_index([100]) == 0.0  # need >=2 for SD


# --- M-value ----------------------------------------------------------------


def test_m_value_at_target_is_zero():
    # All BGs at target → M = 0
    assert m_value([120, 120, 120]) == 0.0


def test_m_value_higher_for_more_extreme_excursions():
    near = m_value([100, 110, 130, 140])
    far = m_value([50, 60, 250, 300])
    assert far > near


# --- LBGI / HBGI ------------------------------------------------------------


def test_lbgi_hbgi_at_kovatchev_neutral_point():
    # f(112.5) ≈ 0, so all BGs at 112.5 → LBGI = HBGI = 0
    lbgi, hbgi = lbgi_hbgi([112.5] * 10)
    assert lbgi == pytest.approx(0.0, abs=0.05)
    assert hbgi == pytest.approx(0.0, abs=0.05)


def test_lbgi_high_for_low_bgs():
    # All BGs at 50 (very low) → high LBGI, zero HBGI
    lbgi, hbgi = lbgi_hbgi([50] * 20)
    assert lbgi > 5.0  # very high
    assert hbgi == 0.0


def test_hbgi_high_for_high_bgs():
    # All BGs at 350 (very high) → zero LBGI, high HBGI
    lbgi, hbgi = lbgi_hbgi([350] * 20)
    assert lbgi == 0.0
    assert hbgi > 5.0


def test_lbgi_band_thresholds():
    assert lbgi_band(0.5) == "low"
    assert lbgi_band(1.5) == "moderate"
    assert lbgi_band(3.0) == "high"
    assert lbgi_band(10.0) == "very_high"


def test_hbgi_band_thresholds():
    assert hbgi_band(2.0) == "low"
    assert hbgi_band(7.0) == "moderate"
    assert hbgi_band(12.0) == "high"
    assert hbgi_band(20.0) == "very_high"


# --- ADRR -------------------------------------------------------------------


def test_adrr_empty():
    assert adrr([]) == 0.0
    assert adrr([[], []]) == 0.0


def test_adrr_combines_max_low_and_high_per_day():
    # Day 1: mix of low + high. Day 2: only middle. ADRR > 0.
    day1 = [50, 60, 130, 350]
    day2 = [120, 130, 140]
    result = adrr([day1, day2])
    assert result > 0.0


# --- GRI (Klonoff 2023) ------------------------------------------------------


def test_gri_empty():
    result = gri([])
    assert result["gri"] == 0.0


def test_gri_all_in_target_is_zero():
    # All BGs in 70-180 → GRI = 0
    values = [80, 100, 120, 150, 170] * 10
    result = gri(values)
    assert result["gri"] == 0.0
    assert result["pct_in_target_70_180"] == 100.0


def test_gri_hypo_weighting():
    # 50% very_low + 50% target: GRI_hypo = 3.0 × 50 = 150, capped to 100 total
    values = [40] * 50 + [120] * 50
    result = gri(values)
    # Hypo component should be 3.0 × 50% = 150 (but clamped on total)
    assert result["gri_hypo"] == pytest.approx(150.0, abs=1.0)
    assert result["pct_very_low_lt54"] == pytest.approx(50.0, abs=1.0)


def test_gri_weighting_factors():
    # 100% in low band (54-69): GRI_hypo = 2.4 × 100 = 240; clamped GRI = 100
    values = [60] * 100
    result = gri(values)
    assert result["gri"] == 100.0
    assert result["gri_hypo"] == pytest.approx(240.0, abs=1.0)


def test_gri_hyper_weighting():
    # 100% at high band (181-250): GRI_hyper = 0.8 × 100 = 80
    values = [220] * 100
    result = gri(values)
    assert result["gri_hyper"] == pytest.approx(80.0, abs=1.0)
    assert result["gri_hypo"] == 0.0


# --- MAGE -------------------------------------------------------------------


def test_mage_empty():
    assert mage([]) == 0.0
    assert mage([100, 100]) == 0.0


def test_mage_constant_series():
    assert mage([100] * 10) == 0.0


def test_mage_detects_oscillations():
    # Big swings: 80↔180 alternating
    values = [80, 180, 80, 180, 80, 180, 80, 180]
    result = mage(values)
    # Excursion amplitude ~100, well above 1×SD ~50
    assert 90 < result < 110


# --- MODD -------------------------------------------------------------------


def test_modd_empty():
    assert modd([]) == 0.0


def test_modd_identical_24h_apart():
    # Two days, same values → MODD = 0
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = []
    for hour in range(0, 24, 1):
        for day in range(2):
            ts = base + timedelta(days=day, hours=hour)
            pairs.append((ts, 120.0))
    # Not enough points (<50) → returns 0
    assert modd(pairs) == 0.0


def test_modd_5min_cadence_two_days():
    # Real-cadence 2 days of 5-min CGM data, identical → MODD = 0
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = []
    for day in range(2):
        for tick in range(0, 24 * 12):
            ts = base + timedelta(days=day, minutes=5 * tick)
            pairs.append((ts, 120.0))
    result = modd(pairs)
    assert result == pytest.approx(0.0, abs=0.5)


# --- CONGA ------------------------------------------------------------------


def test_conga_empty():
    assert conga([]) == 0.0


def test_conga_constant_series_is_zero():
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = [(base + timedelta(minutes=5 * i), 120.0) for i in range(50)]
    result = conga(pairs, n_hours=1)
    assert result == pytest.approx(0.0, abs=0.5)


# --- GVP --------------------------------------------------------------------


def test_gvp_flat_trace_is_zero():
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = [(base + timedelta(minutes=5 * i), 120.0) for i in range(20)]
    result = gvp(pairs)
    assert result == pytest.approx(0.0, abs=0.5)


def test_gvp_high_for_volatile_trace():
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = []
    for i in range(20):
        v = 100 + 80 * ((-1) ** i)  # alternating 180/20
        pairs.append((base + timedelta(minutes=5 * i), v))
    result = gvp(pairs)
    assert result > 100  # arc-length much greater than time-length


# --- COGI -------------------------------------------------------------------


def test_cogi_perfect():
    # 100% TIR, 0% TBR, CV 30 → COGI ~ 100
    score = cogi(100.0, 0.0, 30.0)
    assert score == pytest.approx(100.0, abs=0.1)


def test_cogi_worst_case():
    # 0% TIR, 20% TBR, 120% CV → COGI ~ 0
    score = cogi(0.0, 20.0, 120.0)
    assert score == pytest.approx(0.0, abs=1.0)


def test_cogi_weights_sum_to_100():
    # All sub-scores at 50 → weighted sum ≈ 50
    score = cogi(50.0, 7.5, 72.0)
    # TIR sub=50, TBR sub at midpoint (7.5/15) = 50, CV sub at midpoint=50
    assert 49 < score < 51


# --- Percentile + AGP -------------------------------------------------------


def test_percentile_median():
    assert percentile([10, 20, 30, 40, 50], 50) == pytest.approx(30.0, abs=0.5)


def test_percentile_extremes():
    assert percentile([10, 20, 30], 0) == 10.0
    assert percentile([10, 20, 30], 100) == 30.0


def test_percentile_empty():
    assert percentile([], 50) == 0.0


def test_agp_hourly_partition():
    # Synthetic: one BG per hour at hour H for H+10 readings
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = []
    for h in range(24):
        for _ in range(5):
            pairs.append((base.replace(hour=h), 100.0 + h * 5))
    result = agp_hourly_percentiles(pairs)
    assert len(result) == 24
    # Hour 0 should have median 100
    assert result[0]["p50"] == pytest.approx(100.0, abs=1.0)
    # Hour 23 should have median 100 + 23*5 = 215
    assert result[23]["p50"] == pytest.approx(215.0, abs=1.0)


def test_partition_by_local_hour_wrapping_window():
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = []
    for h in [2, 5, 8, 15, 22, 23]:
        pairs.append((base.replace(hour=h), 100.0))
    # overnight wraps 22-6
    out = partition_by_local_hour(pairs, windows={"overnight": (22, 6), "day": (6, 22)})
    assert len(out["overnight"]) == 4  # 22, 23, 2, 5
    assert len(out["day"]) == 2  # 8, 15


# --- CUSUM change-point ------------------------------------------------------


def test_cusum_no_change_in_stationary():
    # Pseudo-stationary series — should detect zero or very few change-points
    import random

    random.seed(42)
    values = [100 + random.gauss(0, 5) for _ in range(200)]
    result = cusum_change_points(values, threshold_sigma=4.0)
    # Stationary should produce ≤ ~3 false positives at sigma=4
    assert len(result) <= 3


def test_cusum_detects_step_change():
    # 100 samples at 100, then 100 samples at 130
    # SD of this entire series is ~15 (inflated by the step itself), so we
    # use threshold_sigma=1.5 which translates to a ~22-point threshold — well
    # below the actual 30-point step. Real-world hourly BG series have less
    # inflated SD relative to a true regime change.
    values = [100.0] * 100 + [130.0] * 100
    result = cusum_change_points(values, threshold_sigma=1.5)
    assert len(result) >= 1
    first = result[0]
    assert first["direction"] == 1.0
    # Change point should be near the step (around index 100)
    assert 80 <= first["index"] <= 120


def test_cusum_empty_input():
    assert cusum_change_points([]) == []
    assert cusum_change_points([100.0] * 5) == []  # too few samples


def test_hourly_aggregate_simple():
    base = datetime(2026, 5, 23, 0, 0, tzinfo=UTC)
    pairs = [
        (base + timedelta(minutes=10), 100.0),
        (base + timedelta(minutes=20), 110.0),
        (base + timedelta(hours=1, minutes=5), 200.0),
    ]
    result = hourly_aggregate(pairs)
    assert len(result) == 2
    assert result[0][1] == pytest.approx(105.0)  # mean of 100, 110
    assert result[1][1] == pytest.approx(200.0)


# --- IOB exponential curve --------------------------------------------------


def test_exp_iob_at_t_zero_is_one():
    assert exponential_iob_fraction(0, dia_hours=5.0, peak_min=55) == pytest.approx(1.0, abs=0.01)


def test_exp_iob_at_dia_is_zero():
    assert exponential_iob_fraction(300, dia_hours=5.0, peak_min=55) == 0.0


def test_exp_iob_decreases_monotonically():
    dia = 5.0
    peak = 55
    prev = 1.0
    for t in range(10, int(dia * 60), 10):
        cur = exponential_iob_fraction(t, dia, peak)
        assert cur <= prev + 0.001  # allow tiny float jitter
        prev = cur


def test_exp_iob_peak_min_invariant():
    # Different peak_min → different curve shape, still bounded
    f1 = exponential_iob_fraction(60, 5.0, 45)
    f2 = exponential_iob_fraction(60, 5.0, 75)
    assert 0 < f1 < 1
    assert 0 < f2 < 1


def test_fit_dia_to_residuals_empty():
    result = fit_dia_to_residuals([])
    assert result["sample_count"] == 0
    assert result["best_dia_hours"] == 0.0


def test_fit_dia_recovers_known_dia_from_synthetic():
    # Generate observations matching DIA=5h, peak=55min
    true_dia = 5.0
    true_peak = 55
    observations = []
    for t in range(30, int(true_dia * 60), 30):
        true_frac = exponential_iob_fraction(t, true_dia, true_peak)
        observations.append((float(t), 0.0, true_frac))
    # Repeat each observation to give enough "samples"
    observations = observations * 5

    result = fit_dia_to_residuals(observations)
    # Should recover something close to 5.0 (within grid spacing)
    assert abs(result["best_dia_hours"] - true_dia) <= 1.0
