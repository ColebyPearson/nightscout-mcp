"""Tests for analytics.py — pure algorithm tests, no HTTP.

We construct synthetic SGV/Treatment sequences and assert the analytics
functions surface the expected signals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nightscout_mcp.analytics import (
    analyze_meal,
    carb_ratio_check,
    compare_periods,
    compression_low_analysis,
    daily_report,
    detect_patterns,
    insulin_sensitivity_check,
    overnight_analysis,
)
from nightscout_mcp.models import Sgv, Treatment


def _sgv(mgdl: int, dt: datetime, direction: str = "Flat") -> Sgv:
    return Sgv.model_validate(
        {
            "sgv": mgdl,
            "date": int(dt.timestamp() * 1000),
            "dateString": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "direction": direction,
            "type": "sgv",
        }
    )


def _tx(
    event_type: str,
    dt: datetime,
    insulin: float | None = None,
    carbs: float | None = None,
    notes: str | None = None,
) -> Treatment:
    return Treatment.model_validate(
        {
            "_id": f"t-{int(dt.timestamp())}",
            "eventType": event_type,
            "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "insulin": insulin,
            "carbs": carbs,
            "notes": notes,
        }
    )


# --- daily_report -------------------------------------------------------------


def test_daily_report_aggregates_insulin_and_carbs() -> None:
    base = datetime(2026, 5, 22, tzinfo=UTC)
    sgvs = [_sgv(120, base + timedelta(minutes=i * 5)) for i in range(20)]
    txs = [
        _tx("Bolus", base + timedelta(hours=8), insulin=2.5),
        _tx("Carb Correction", base + timedelta(hours=12), carbs=40),
        _tx("Note", base + timedelta(hours=14), notes="ran 5k"),
    ]
    r = daily_report("2026-05-22", sgvs, txs)
    assert r.date == "2026-05-22"
    assert r.stats.reading_count == 20
    assert r.treatment_count == 3
    assert r.total_insulin_u == 2.5
    assert r.total_carbs_g == 40.0
    assert r.notes == ["ran 5k"]


def test_daily_report_filters_auto_generated_profile_switch_notes() -> None:
    """AAPS Profile Switch / Temp Basal / Temporary Override rows often carry
    the profile name as `notes` — that's sync noise, not real user notes."""
    base = datetime(2026, 5, 22, tzinfo=UTC)
    txs = [
        _tx("Profile Switch", base, notes="regular (60%)"),
        _tx("Temp Basal", base + timedelta(minutes=5), notes="0.8U/h auto"),
        _tx("Note", base + timedelta(hours=6), notes="manual: felt low"),
    ]
    r = daily_report("2026-05-22", [], txs)
    assert r.notes == ["manual: felt low"]


# --- compare_periods ----------------------------------------------------------


def test_compare_periods_flags_improved_tir() -> None:
    base = datetime(2026, 5, 22, tzinfo=UTC)
    # Period A: half in-range, half high → TIR 50%
    a = [_sgv(120, base + timedelta(minutes=i * 5)) for i in range(10)] + [
        _sgv(220, base + timedelta(minutes=(i + 10) * 5)) for i in range(10)
    ]
    # Period B: all in-range → TIR 100%
    b = [_sgv(120, base + timedelta(minutes=i * 5)) for i in range(20)]
    result = compare_periods("last_week", a, "this_week", b, hours_each=24)
    assert result.delta_tir_pp == 50.0  # 100 - 50
    assert "TIR +50" in result.improvement_summary


# --- analyze_meal -------------------------------------------------------------


def test_analyze_meal_finds_peak_and_time_to_peak() -> None:
    meal_time = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        _sgv(110, meal_time - timedelta(minutes=10)),  # pre
        _sgv(120, meal_time + timedelta(minutes=30)),
        _sgv(200, meal_time + timedelta(minutes=90)),  # peak
        _sgv(180, meal_time + timedelta(minutes=120)),
        _sgv(140, meal_time + timedelta(hours=4) - timedelta(minutes=5)),  # end
    ]
    meal = _tx("Carb Correction", meal_time, insulin=4.0, carbs=60)
    r = analyze_meal(meal_time, meal, sgvs, window_hours=4)
    assert r.peak_mgdl == 200
    assert r.time_to_peak_minutes == 90
    assert r.rise_mgdl == 90  # 200 - 110
    assert r.carbs_g == 60
    assert r.insulin_u == 4.0
    assert any("Large post-meal rise" in n for n in r.notes)


def test_analyze_meal_handles_empty_window() -> None:
    meal_time = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    r = analyze_meal(meal_time, None, [], window_hours=4)
    assert r.peak_mgdl == 0  # degenerate
    assert any("No CGM readings" in n for n in r.notes)


# --- overnight_analysis -------------------------------------------------------


def test_overnight_analysis_computes_drift_and_dawn_rise() -> None:
    base = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
    # Readings every 30 minutes from 00:00 to 07:00, stable at 100 then dawn rise
    sgvs: list[Sgv] = []
    for hour in range(8):
        v = 100 if hour < 3 else 100 + (hour - 2) * 15  # rises after 03:00
        sgvs.append(_sgv(v, base + timedelta(hours=hour)))
    r = overnight_analysis("2026-05-22", sgvs)
    assert r.start_mgdl == 100
    assert r.end_mgdl == 175
    assert r.drift_mgdl == 75
    assert r.dawn_rise_mgdl is not None and r.dawn_rise_mgdl > 30


def test_overnight_analysis_counts_below_70_time() -> None:
    base = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
    # 5min cadence; 4 readings at 60 mg/dL = 20 min below 70
    sgvs = [_sgv(60, base + timedelta(minutes=i * 5)) for i in range(4)] + [
        _sgv(100, base + timedelta(minutes=20 + i * 5)) for i in range(10)
    ]
    r = overnight_analysis("2026-05-22", sgvs)
    assert r.time_below_70_minutes == 20


# --- detect_patterns ---------------------------------------------------------


def test_detect_patterns_flags_recurring_overnight_low() -> None:
    """Three of 5 days have a 02:00 dip below 70."""
    groups = []
    for day in range(1, 6):
        d = datetime(2026, 5, day, 0, 0, tzinfo=UTC)
        readings = [_sgv(110, d + timedelta(hours=h)) for h in range(8)]
        if day <= 3:
            # Replace 02:00 reading with a low
            readings[2] = _sgv(58, d + timedelta(hours=2))
        groups.append((d.strftime("%Y-%m-%d"), readings))
    r = detect_patterns(5, groups)
    types = {p.type for p in r.patterns}
    assert "overnight_low" in types
    overnight = next(p for p in r.patterns if p.type == "overnight_low")
    assert overnight.occurrence_count == 3


def test_detect_patterns_flags_dawn_phenomenon() -> None:
    groups = []
    for day in range(1, 4):
        d = datetime(2026, 5, day, 0, 0, tzinfo=UTC)
        readings = []
        for hour in range(8):
            v = 100 if hour < 3 else 100 + (hour - 2) * 20  # big dawn rise
            readings.append(_sgv(v, d + timedelta(hours=hour)))
        groups.append((d.strftime("%Y-%m-%d"), readings))
    r = detect_patterns(3, groups)
    dawn = next((p for p in r.patterns if p.type == "dawn_phenomenon"), None)
    assert dawn is not None
    assert dawn.occurrence_count == 3


# --- insulin_sensitivity_check -----------------------------------------------


def test_isf_check_derives_from_isolated_corrections() -> None:
    """Two correction boluses: each drops 50 mg/dL on 1.0U → derived ISF 50 mg/dL/U."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        # Bolus 1: pre 200 mg/dL → drop to 150 within DIA
        _sgv(200, base - timedelta(minutes=10)),
        _sgv(180, base + timedelta(hours=1)),
        _sgv(150, base + timedelta(hours=3)),
        _sgv(155, base + timedelta(hours=4)),
        # Bolus 2: pre 180 → drop to 130
        _sgv(180, base + timedelta(hours=6) - timedelta(minutes=10)),
        _sgv(160, base + timedelta(hours=7)),
        _sgv(130, base + timedelta(hours=9)),
    ]
    txs = [
        _tx("Correction Bolus", base, insulin=1.0),
        _tx("Correction Bolus", base + timedelta(hours=6), insulin=1.0),
    ]
    r = insulin_sensitivity_check(txs, sgvs, profile_isf_mmol=2.8)  # 2.8 mmol = 50 mg/dL
    assert r.sample_count == 2
    assert r.derived_isf_mgdl_per_unit == 50.0
    assert r.confidence == "low"  # <3 samples


def test_isf_check_excludes_boluses_near_carbs() -> None:
    """A correction bolus within ±60 min of a carb entry should be ignored."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        _sgv(200, base - timedelta(minutes=10)),
        _sgv(150, base + timedelta(hours=3)),
    ]
    txs = [
        _tx("Correction Bolus", base, insulin=1.0),
        _tx("Carb Correction", base + timedelta(minutes=30), carbs=15),
    ]
    r = insulin_sensitivity_check(txs, sgvs, profile_isf_mmol=2.8)
    # The correction bolus had carbs nearby, so it's excluded.
    assert r.sample_count == 0
    assert "Not enough" in r.recommendation


# --- compression_low_analysis ------------------------------------------------


def test_compression_low_detects_fast_drop_and_recovery() -> None:
    """Classic compression-low pattern: 100 → 50 → 100 in 10 min total."""
    base = datetime(2026, 5, 22, 2, 0, tzinfo=UTC)
    sgvs = [
        _sgv(105, base - timedelta(minutes=10)),
        _sgv(100, base - timedelta(minutes=5)),
        _sgv(50, base),  # rapid drop
        _sgv(100, base + timedelta(minutes=5)),  # rapid recovery
        _sgv(105, base + timedelta(minutes=10)),
    ]
    r = compression_low_analysis(1, sgvs)
    assert len(r.suspected) == 1
    assert r.suspected[0].min_mgdl == 50


def test_compression_low_does_not_flag_slow_real_hypo() -> None:
    """A real hypo: gradual drop, slow recovery over an hour — NOT a compression."""
    base = datetime(2026, 5, 22, 2, 0, tzinfo=UTC)
    sgvs = [_sgv(110 - i * 4, base + timedelta(minutes=i * 5)) for i in range(8)] + [
        _sgv(78 + i * 3, base + timedelta(minutes=40 + i * 5)) for i in range(10)
    ]
    r = compression_low_analysis(1, sgvs)
    assert len(r.suspected) == 0


# --- carb_ratio_check --------------------------------------------------------


def test_carb_ratio_check_derives_average_applied_cr() -> None:
    """Two meal boluses (combined carbs+insulin rows): 60g/6U=10 and 40g/4U=10.
    Profile CR=15. Derived=10, ratio=0.67."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        # Meal 1: pre 120, ends at 130
        _sgv(120, base - timedelta(minutes=10)),
        _sgv(140, base + timedelta(hours=1)),
        _sgv(130, base + timedelta(hours=4) - timedelta(minutes=5)),
        # Meal 2: pre 110, ends at 120
        _sgv(110, base + timedelta(hours=8) - timedelta(minutes=10)),
        _sgv(140, base + timedelta(hours=9)),
        _sgv(120, base + timedelta(hours=12) - timedelta(minutes=5)),
    ]
    txs = [
        _tx("Meal Bolus", base, insulin=6.0, carbs=60),
        _tx("Meal Bolus", base + timedelta(hours=8), insulin=4.0, carbs=40),
    ]
    r = carb_ratio_check(txs, sgvs, profile_cr_g_per_unit=15.0)
    assert r.sample_count == 2
    assert r.derived_cr_g_per_unit == 10.0
    assert r.ratio_derived_over_profile == 0.67
    assert r.confidence == "low"  # <3 samples


def test_carb_ratio_check_pairs_aaps_style_split_carb_and_insulin_rows() -> None:
    """AAPS writes carbs and insulin as SEPARATE treatment rows close in time.
    Discovered against gladoctopus.my.nightscoutpro.com — 0 of 50 rows had
    both fields populated in one row.
    """
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        _sgv(120, base - timedelta(minutes=10)),
        _sgv(135, base + timedelta(hours=4) - timedelta(minutes=5)),
    ]
    txs = [
        # AAPS style: separate carb row + insulin row at the same minute
        _tx("Carb Correction", base, carbs=50),
        _tx("Correction Bolus", base + timedelta(seconds=30), insulin=5.0),
    ]
    r = carb_ratio_check(txs, sgvs, profile_cr_g_per_unit=10.0)
    assert r.sample_count == 1
    assert r.derived_cr_g_per_unit == 10.0  # 50g / 5U


def test_carb_ratio_check_excludes_meal_with_subsequent_meal_in_window() -> None:
    """A meal followed by another carb meal within 4h is excluded — the
    subsequent meal pollutes the post-meal residual measurement. Prior meals
    don't contaminate because they're absorbed before our analysis window."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        # For meal #1 (eligible base): pre + end
        _sgv(120, base - timedelta(minutes=10)),
        _sgv(130, base + timedelta(hours=3, minutes=50)),
        # For meal #2 (subsequent — eligible since no later meal)
        _sgv(118, base + timedelta(hours=2) - timedelta(minutes=10)),
        _sgv(135, base + timedelta(hours=6) - timedelta(minutes=5)),
    ]
    txs = [
        _tx("Meal Bolus", base, insulin=5.0, carbs=50),  # contaminated by meal #2
        _tx("Meal Bolus", base + timedelta(hours=2), insulin=4.0, carbs=40),  # eligible
    ]
    r = carb_ratio_check(txs, sgvs, profile_cr_g_per_unit=10.0)
    # Only meal #2 should be eligible: it has no subsequent meal within 4h.
    assert r.sample_count == 1
    assert r.derived_cr_g_per_unit == 10.0  # 40/4


def test_carb_ratio_check_flags_systematic_post_meal_rise() -> None:
    """Meals consistently ending ~40 mg/dL higher than they started → under-bolused."""
    base = datetime(2026, 5, 22, 7, 0, tzinfo=UTC)
    sgvs: list[Sgv] = []
    txs: list[Treatment] = []
    for day in range(5):
        meal_time = base + timedelta(days=day)
        sgvs.append(_sgv(100, meal_time - timedelta(minutes=10)))
        sgvs.append(_sgv(140, meal_time + timedelta(hours=4) - timedelta(minutes=5)))
        txs.append(_tx("Meal Bolus", meal_time, insulin=4.0, carbs=40))
    r = carb_ratio_check(txs, sgvs, profile_cr_g_per_unit=10.0)
    assert r.sample_count == 5
    assert r.avg_end_minus_pre_mgdl == 40.0
    assert "may be too high" in r.recommendation.lower()  # CR too high → undercovered
