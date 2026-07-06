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
    effective_isf_check,
    hypo_episodes,
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


def _ds(
    dt: datetime,
    sens: float | None = None,
    cob: float | None = None,
    variable_sens: float | None = None,
) -> dict:
    """Minimal devicestatus row. Supports both `sens` (oref0/Loop convention)
    and `variable_sens` (AAPS Dynamic ISF convention — always mg/dL/U).
    """
    suggested: dict = {}
    if sens is not None:
        suggested["sens"] = sens
    if variable_sens is not None:
        suggested["variable_sens"] = variable_sens
    if cob is not None:
        suggested["COB"] = cob
    return {
        "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "openaps": {"suggested": suggested},
    }


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


def test_isf_check_more_sensitive_recommends_raising_isf() -> None:
    """Regression: derived ISF > profile means over-dosing corrections -> hypo.

    The safe direction is to RAISE profile ISF (higher ISF number = smaller
    correction dose). Guards against a directional inversion that would deepen
    lows. See effective_isf_check, which resolves the same signal identically.
    """
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        _sgv(200, base - timedelta(minutes=10)),
        _sgv(150, base + timedelta(hours=3)),
    ]
    txs = [_tx("Correction Bolus", base, insulin=1.0)]
    # Derived ISF ~50 mg/dL/U (2.78 mmol); profile only 1.7 mmol -> ratio ~1.63.
    r = insulin_sensitivity_check(txs, sgvs, profile_isf_mmol=1.7)
    assert r.ratio_derived_over_profile is not None
    assert r.ratio_derived_over_profile > 1.15
    assert "RAISING profile ISF" in r.recommendation
    assert "lowering profile ISF" not in r.recommendation.lower()
    assert "healthcare provider" in r.recommendation  # safety disclaimer present


def test_isf_check_less_sensitive_recommends_lowering_isf() -> None:
    """Derived ISF < profile means under-dosing corrections -> the safe
    direction is to LOWER profile ISF (lower number = larger dose)."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs = [
        _sgv(200, base - timedelta(minutes=10)),
        _sgv(150, base + timedelta(hours=3)),
    ]
    txs = [_tx("Correction Bolus", base, insulin=1.0)]
    # Derived ISF ~50 mg/dL/U (2.78 mmol); profile 5.0 mmol -> ratio ~0.56.
    r = insulin_sensitivity_check(txs, sgvs, profile_isf_mmol=5.0)
    assert r.ratio_derived_over_profile is not None
    assert r.ratio_derived_over_profile < 0.85
    assert "LOWERING profile ISF" in r.recommendation
    assert "raising profile ISF" not in r.recommendation.lower()
    assert "healthcare provider" in r.recommendation


def test_overnight_analysis_excludes_zero_sgv_phantom_low() -> None:
    """A sensor-warmup/error 0 must not register as a severe low (min_mgdl=0)."""
    base = datetime(2026, 5, 22, 2, 0, tzinfo=UTC)
    readings = [
        _sgv(0, base),  # warmup/error — must be dropped
        _sgv(120, base + timedelta(minutes=5)),
        _sgv(110, base + timedelta(minutes=10)),
    ]
    r = overnight_analysis("2026-05-22", readings)
    assert r.min_mgdl == 110  # not 0
    assert r.time_below_54_minutes == 0  # phantom 0 must not inflate TBR<54


def test_overnight_analysis_dawn_anchors_use_local_time() -> None:
    """Dawn rise anchors 03:00/07:00 must be matched in LOCAL time.

    With a -5h offset, UTC 08:00/12:00 are local 03:00/07:00. A UTC-only match
    would find nothing and return dawn_rise_mgdl=None.
    """
    day = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
    readings = [
        _sgv(100, day + timedelta(hours=8)),  # local 03:00
        _sgv(140, day + timedelta(hours=12)),  # local 07:00
    ]
    r = overnight_analysis("2026-05-22", readings, tz_offset_hours=-5.0)
    assert r.dawn_rise_mgdl == 40
    # And with the (wrong) UTC default, these anchors don't resolve.
    r_utc = overnight_analysis("2026-05-22", readings)
    assert r_utc.dawn_rise_mgdl is None


def test_detect_patterns_overnight_low_uses_local_time() -> None:
    """An overnight low must be judged by local hour, not UTC hour.

    With offset -5h, a low at UTC 08:30 is local 03:30 -> counts as overnight.
    A low at UTC 02:00 is local 21:00 (evening) -> must NOT count.
    """
    day = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
    overnight_groups = [("2026-05-22", [_sgv(60, day + timedelta(hours=8, minutes=30))])]
    r = detect_patterns(1, overnight_groups, tz_offset_hours=-5.0)
    assert any(p.type == "overnight_low" for p in r.patterns)

    evening_groups = [("2026-05-22", [_sgv(60, day + timedelta(hours=2))])]
    r2 = detect_patterns(1, evening_groups, tz_offset_hours=-5.0)
    assert not any(p.type == "overnight_low" for p in r2.patterns)


# --- hypo_episodes -----------------------------------------------------------


def _low_run(base: datetime, values: list[int], step_min: int = 5) -> list[Sgv]:
    return [_sgv(v, base + timedelta(minutes=i * step_min)) for i, v in enumerate(values)]


def test_hypo_episode_requires_15_min_below_70() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    # 15-min run (0,5,10,15) all <70 -> one level-1 event.
    sgvs = _low_run(base, [68, 65, 62, 66]) + [_sgv(120, base + timedelta(minutes=25))]
    r = hypo_episodes(1, sgvs)
    assert r.total_episodes == 1
    e = r.episodes[0]
    assert e.duration_minutes == 15
    assert e.nadir_mgdl == 62
    assert e.level == 1
    assert e.rescue_carbs is False


def test_hypo_single_brief_low_is_not_an_episode() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    # Only two <70 readings spanning 5 min -> below the 15-min floor.
    sgvs = _low_run(base, [65, 66]) + [_sgv(120, base + timedelta(minutes=15))]
    r = hypo_episodes(1, sgvs)
    assert r.total_episodes == 0
    assert "No consensus" in r.summary


def test_hypo_level2_set_by_nadir_below_54() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    sgvs = _low_run(base, [66, 58, 50, 60]) + [_sgv(110, base + timedelta(minutes=30))]
    r = hypo_episodes(1, sgvs)
    assert r.total_episodes == 1
    assert r.episodes[0].level == 2
    assert r.level2_episodes == 1


def test_hypo_brief_rise_does_not_split_one_event() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    # Low, a single 75 blip (5 min above, <15 recovery), then low again.
    sgvs = (
        _low_run(base, [66, 64])
        + [_sgv(75, base + timedelta(minutes=10))]
        + _low_run(base + timedelta(minutes=15), [63, 62, 68])
    )
    r = hypo_episodes(1, sgvs)
    assert r.total_episodes == 1  # merged, not two


def test_hypo_nocturnal_flag_uses_local_time() -> None:
    day = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
    # UTC 08:00-08:15 == local 03:00 at offset -5 -> nocturnal.
    sgvs = _low_run(day + timedelta(hours=8), [66, 62, 64, 65])
    r = hypo_episodes(1, sgvs, tz_offset_hours=-5.0)
    assert r.total_episodes == 1
    assert r.episodes[0].nocturnal is True
    assert r.nocturnal_episodes == 1


def test_hypo_rescue_carbs_linked_to_event() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    sgvs = _low_run(base, [66, 60, 58, 64]) + [_sgv(120, base + timedelta(minutes=30))]
    txs = [_tx("Carb Correction", base + timedelta(minutes=10), carbs=15)]
    r = hypo_episodes(1, sgvs, txs)
    assert r.total_episodes == 1
    assert r.episodes[0].rescue_carbs is True
    assert r.episodes_with_rescue_carbs == 1


def test_hypo_flags_low_cgm_coverage() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    sgvs = _low_run(base, [66, 64, 62, 65])  # 4 readings over a claimed 14 days
    r = hypo_episodes(14, sgvs)
    assert r.pct_cgm_active < 70
    assert "CGM-active" in r.summary


def test_hypo_excludes_zero_sgv() -> None:
    base = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)
    # A 0 must not seed a phantom level-2 event.
    sgvs = [_sgv(0, base), _sgv(120, base + timedelta(minutes=5))]
    r = hypo_episodes(1, sgvs)
    assert r.total_episodes == 0


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


def test_compression_low_suppresses_when_carb_treatment_explains_recovery() -> None:
    """A textbook compression pattern is NOT flagged when a carb treatment
    landed near the minimum — that explains the recovery (real treated low)."""
    base = datetime(2026, 5, 22, 2, 0, tzinfo=UTC)
    sgvs = [
        _sgv(105, base - timedelta(minutes=10)),
        _sgv(100, base - timedelta(minutes=5)),
        _sgv(50, base),
        _sgv(100, base + timedelta(minutes=5)),
        _sgv(105, base + timedelta(minutes=10)),
    ]
    # User treated the low with 15g carbs at the moment of the minimum.
    txs = [_tx("Carb Correction", base + timedelta(minutes=2), carbs=15)]
    r = compression_low_analysis(1, sgvs, treatments=txs)
    assert len(r.suspected) == 0


def test_compression_low_still_flags_when_treatments_are_far_from_minimum() -> None:
    """A carb treatment 1 hour earlier doesn't explain this dip's recovery."""
    base = datetime(2026, 5, 22, 2, 0, tzinfo=UTC)
    sgvs = [
        _sgv(105, base - timedelta(minutes=10)),
        _sgv(100, base - timedelta(minutes=5)),
        _sgv(50, base),
        _sgv(100, base + timedelta(minutes=5)),
        _sgv(105, base + timedelta(minutes=10)),
    ]
    txs = [_tx("Carb Correction", base - timedelta(hours=1), carbs=20)]
    r = compression_low_analysis(1, sgvs, treatments=txs)
    assert len(r.suspected) == 1


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
    Discovered against a personal Nightscout instance — 0 of 50 rows had
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


# --- effective_isf_check -----------------------------------------------------


def _correction_with_drop(
    base: datetime,
    pre_mgdl: int,
    min_mgdl: int,
    units: float,
    sens_at_decision: float | None = None,
) -> tuple[list[Sgv], list[Treatment], list[dict]]:
    """Build a single correction-bolus scenario: pre SGV, bolus, post-min SGV,
    and (optionally) a devicestatus row 5 min before the bolus carrying sens.
    """
    sgvs = [
        _sgv(pre_mgdl, base - timedelta(minutes=10)),
        _sgv(min_mgdl, base + timedelta(hours=3)),
        _sgv(min_mgdl + 5, base + timedelta(hours=4)),
    ]
    txs = [_tx("Correction Bolus", base, insulin=units)]
    devicestatuses: list[dict] = []
    if sens_at_decision is not None:
        devicestatuses.append(_ds(base - timedelta(minutes=5), sens=sens_at_decision))
    return sgvs, txs, devicestatuses


def test_effective_isf_check_happy_path_in_target_band() -> None:
    """Four corrections all starting in [100,180), AAPS effective ISF matches realized."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs: list[Sgv] = []
    txs: list[Treatment] = []
    dss: list[dict] = []
    for i in range(4):
        t = base + timedelta(hours=6 * i)
        s, tx, ds = _correction_with_drop(t, pre_mgdl=140, min_mgdl=90, units=1.0, sens_at_decision=2.8)
        sgvs += s
        txs += tx
        dss += ds
    # realized = (140-90)/1 = 50 mg/dL/U = 2.8 mmol/L/U; effective = 2.8 → ratio 1.0
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 4
    assert r.samples_without_sens == 0
    assert r.overall_ratio_realized_over_effective == 1.0
    in_band = next(b for b in r.by_bg_band if b.band_label == "in_target")
    assert in_band.sample_count == 4
    assert "well-calibrated" in r.recommendation


def test_effective_isf_check_stratifies_by_bg_band() -> None:
    """3 in-target ratio 1.0; 3 above-target ratio 1.3 → bands distinct."""
    base = datetime(2026, 5, 22, 8, 0, tzinfo=UTC)
    sgvs: list[Sgv] = []
    txs: list[Treatment] = []
    dss: list[dict] = []
    # in_target band: pre=140, drop 50 → realized 2.8 mmol/U; sens=2.8 → ratio 1.0
    for i in range(3):
        t = base + timedelta(hours=8 * i)
        s, tx, ds = _correction_with_drop(t, 140, 90, 1.0, sens_at_decision=2.8)
        sgvs += s
        txs += tx
        dss += ds
    # above_target band: pre=220, drop ~65 → realized ~3.6 mmol/U; sens=2.8 → ratio ~1.3
    for i in range(3):
        t = base + timedelta(days=2, hours=8 * i)
        s, tx, ds = _correction_with_drop(t, 220, 155, 1.0, sens_at_decision=2.8)
        sgvs += s
        txs += tx
        dss += ds
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 6
    by = {b.band_label: b for b in r.by_bg_band}
    assert by["in_target"].sample_count == 3
    assert by["above_target"].sample_count == 3
    # in_target ratio ≈ 1.0; above_target ratio > 1.15
    assert by["in_target"].ratio_realized_over_effective == 1.0
    assert by["above_target"].ratio_realized_over_effective > 1.15
    assert "BG-curve" in r.recommendation


def test_effective_isf_check_excludes_samples_missing_sens() -> None:
    """5 corrections; 2 have no devicestatus within ±15 min → samples_without_sens=2."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs: list[Sgv] = []
    txs: list[Treatment] = []
    dss: list[dict] = []
    for i in range(5):
        t = base + timedelta(hours=8 * i)
        s, tx, _ = _correction_with_drop(t, 140, 90, 1.0)
        sgvs += s
        txs += tx
        if i < 3:
            dss.append(_ds(t - timedelta(minutes=5), sens=2.8))
        # 2 corrections have NO matching devicestatus
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 3
    assert r.samples_without_sens == 2


def test_effective_isf_check_handles_zero_sens_field() -> None:
    """Devicestatus rows present but none carry sens → diagnostic recommendation."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    # Row exists but no sens
    dss = [_ds(base - timedelta(minutes=5), cob=15)]
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 0
    assert r.devicestatus_rows_examined == 1
    assert "Dynamic ISF may be disabled" in r.recommendation


def test_effective_isf_check_handles_zero_devicestatus_rows() -> None:
    """No devicestatus rows at all → distinct prerequisite message."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    r = effective_isf_check(txs, sgvs, devicestatuses=[], profile_units="mmol")
    assert r.sample_count == 0
    assert r.devicestatus_rows_examined == 0
    assert "No devicestatus rows" in r.recommendation


def test_effective_isf_check_converts_mgdl_profile_units() -> None:
    """profile_units='mg/dL' + sens=50 (mg/dL/U) → internal 2.8 mmol/L/U."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    dss = [_ds(base - timedelta(minutes=5), sens=50)]  # 50 mg/dL/U
    r = effective_isf_check(txs, sgvs, dss, profile_units="mg/dL")
    # 50 mg/dL/U → 2.8 mmol/L/U via the conventional ÷18 factor (units.py).
    # realized = 50 mg/dL drop / 1U → also 2.8 mmol/U → ratio 1.0
    assert r.sample_count == 1
    assert r.avg_effective_isf_mmol_per_unit == 2.8
    assert r.overall_ratio_realized_over_effective == 1.0


def test_effective_isf_check_handles_mmol_profile_units() -> None:
    """profile_units='mmol' + sens=2.8 → no conversion."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, dss = _correction_with_drop(base, 140, 90, 1.0, sens_at_decision=2.8)
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 1
    assert r.avg_effective_isf_mmol_per_unit == 2.8


def test_effective_isf_check_picks_most_recent_prior_devicestatus() -> None:
    """Two rows before the bolus at -3min and -8min; uses the -3min (most recent) row."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    dss = [
        _ds(base - timedelta(minutes=8), sens=5.0),  # older, would give different ratio
        _ds(base - timedelta(minutes=3), sens=2.8),  # most recent prior
    ]
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 1
    assert r.avg_effective_isf_mmol_per_unit == 2.8


def test_effective_isf_check_skips_devicestatus_after_bolus() -> None:
    """Only post-bolus row exists → sample excluded (no prior match)."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    dss = [_ds(base + timedelta(minutes=2), sens=2.8)]  # AFTER the bolus
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 0
    assert r.samples_without_sens == 1


def test_effective_isf_check_reads_aaps_variable_sens_field() -> None:
    """AAPS Dynamic ISF publishes `variable_sens` (mg/dL/U) instead of oref0's
    `sens` field. Discovered against a personal Nightscout instance on
    2026-05-23: 5396 devicestatus rows, zero with `sens`, all with
    `variable_sens` ranging 109-110 mg/dL/U.
    """
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    # variable_sens=50 mg/dL/U → 2.8 mmol/L/U after conversion
    dss = [_ds(base - timedelta(minutes=5), variable_sens=50)]
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.sample_count == 1
    assert r.avg_effective_isf_mmol_per_unit == 2.8
    # realized drop = 50 mg/dL/U → 2.8 mmol/U → ratio 1.0
    assert r.overall_ratio_realized_over_effective == 1.0


def test_effective_isf_check_variable_sens_wins_over_sens_when_both_present() -> None:
    """If a row carries BOTH fields (unusual but possible), variable_sens wins —
    it's the AAPS-native, more-recent convention.
    """
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, _ = _correction_with_drop(base, 140, 90, 1.0)
    # variable_sens=50 mg/dL/U → 2.8 mmol/L/U; sens=5.0 mmol/L/U (would be different)
    dss = [_ds(base - timedelta(minutes=5), variable_sens=50, sens=5.0)]
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    assert r.avg_effective_isf_mmol_per_unit == 2.8  # from variable_sens, not sens


def test_effective_isf_check_band_boundary_assignment() -> None:
    """Pre-bolus BG exactly 100 → falls in in_target (half-open [100,180))."""
    base = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    sgvs, txs, dss = _correction_with_drop(base, 100, 70, 1.0, sens_at_decision=2.8)
    r = effective_isf_check(txs, sgvs, dss, profile_units="mmol")
    by = {b.band_label: b for b in r.by_bg_band}
    assert by["in_target"].sample_count == 1
    assert by["below_target"].sample_count == 0
    # And 180 should fall in above_target
    sgvs2, txs2, dss2 = _correction_with_drop(base + timedelta(days=1), 180, 130, 1.0, sens_at_decision=2.8)
    r2 = effective_isf_check(txs2, sgvs2, dss2, profile_units="mmol")
    by2 = {b.band_label: b for b in r2.by_bg_band}
    assert by2["above_target"].sample_count == 1
    assert by2["in_target"].sample_count == 0
