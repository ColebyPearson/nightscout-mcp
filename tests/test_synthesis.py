"""Tests for the pure synthesis logic in synthesis.py.

We build synthetic sub-tool outputs and assert each of the 8 rules fires
when its conditions hold and stays silent when they don't.
"""

from __future__ import annotations

from nightscout_mcp.models import (
    AlgorithmState,
    BgPredictions,
    CrDerivation,
    CurrentGlucose,
    DeviceStatusSummary,
    EffectiveIsfDerivation,
    GlucoseStats,
    IobCob,
    IsfBandSample,
    IsfDerivation,
)
from nightscout_mcp.synthesis import build_synthesis

# ---- Factory helpers --------------------------------------------------------


def _stats(
    mean_mgdl: float = 180.0,
    cv: float = 40.0,
    tbr_lt54: float = 0.5,
    tir: float = 55.0,
    tbr_lt70: float = 3.0,
    tar_gt180: float = 42.0,
    tar_gt250: float = 18.0,
) -> GlucoseStats:
    return GlucoseStats(
        window_hours=24 * 14,
        reading_count=2000,
        mean_mgdl=mean_mgdl,
        mean_mmol=round(mean_mgdl / 18, 1),
        sd_mgdl=mean_mgdl * cv / 100,
        cv_percent=cv,
        gmi_percent=round(3.31 + 0.02392 * mean_mgdl, 2),
        tir_percent=tir,
        tbr_lt70_percent=tbr_lt70,
        tbr_lt54_percent=tbr_lt54,
        tar_gt180_percent=tar_gt180,
        tar_gt250_percent=tar_gt250,
        tir_low_threshold_mgdl=70,
        tir_high_threshold_mgdl=180,
    )


def _isf(ratio: float | None = 1.05, samples: int = 50) -> IsfDerivation:
    return IsfDerivation(
        sample_count=samples,
        derived_isf_mgdl_per_unit=276.0,
        derived_isf_mmol_per_unit=15.3,
        profile_isf_mmol_per_unit=14.0,
        ratio_derived_over_profile=ratio,
        confidence="high" if samples >= 8 else "medium" if samples >= 3 else "low",
        recommendation="...",
    )


def _eff_isf(
    overall_ratio: float = 1.0,
    in_target_ratio: float | None = 1.0,
    above_target_ratio: float | None = 1.0,
    in_target_n: int = 30,
    above_target_n: int = 30,
    samples: int = 60,
) -> EffectiveIsfDerivation:
    bands = [
        IsfBandSample(
            band_label="below_target",
            band_lower_mgdl=0,
            band_upper_mgdl=100,
            sample_count=0,
            avg_effective_isf_mmol_per_unit=None,
            avg_realized_isf_mmol_per_unit=None,
            ratio_realized_over_effective=None,
            note="No eligible corrections in this band.",
        ),
        IsfBandSample(
            band_label="in_target",
            band_lower_mgdl=100,
            band_upper_mgdl=180,
            sample_count=in_target_n,
            avg_effective_isf_mmol_per_unit=12.0,
            avg_realized_isf_mmol_per_unit=12.0 * (in_target_ratio or 1.0),
            ratio_realized_over_effective=in_target_ratio,
        ),
        IsfBandSample(
            band_label="above_target",
            band_lower_mgdl=180,
            band_upper_mgdl=None,
            sample_count=above_target_n,
            avg_effective_isf_mmol_per_unit=9.0,
            avg_realized_isf_mmol_per_unit=9.0 * (above_target_ratio or 1.0),
            ratio_realized_over_effective=above_target_ratio,
        ),
    ]
    return EffectiveIsfDerivation(
        sample_count=samples,
        devicestatus_rows_examined=5000,
        samples_without_sens=1,
        avg_effective_isf_mmol_per_unit=10.5,
        avg_realized_isf_mmol_per_unit=10.5 * overall_ratio,
        overall_ratio_realized_over_effective=overall_ratio,
        confidence="high" if samples >= 8 else "medium" if samples >= 3 else "low",
        by_bg_band=bands,
        recommendation="...",
    )


def _cr(disagree: bool = False) -> CrDerivation:
    rec = (
        "Average applied CR: 20 g/U. Signals disagree: applied CR and post-meal residual ..."
        if disagree
        else "Average applied CR: 17 g/U matches profile within ±10%."
    )
    return CrDerivation(
        sample_count=15,
        derived_cr_g_per_unit=17.0,
        profile_cr_g_per_unit=17.0,
        ratio_derived_over_profile=1.0,
        avg_end_minus_pre_mgdl=-5.0,
        confidence="high",
        recommendation=rec,
    )


def _device_status(
    eventual_bg_mgdl: int | None = 140,
    carbs_required_g: float | None = None,
) -> DeviceStatusSummary:
    algo = AlgorithmState(
        algorithm="SMB",
        running_dynamic_isf=True,
        current_bg_mgdl=160,
        eventual_bg_mgdl=eventual_bg_mgdl,
        target_bg_mgdl=100,
        effective_isf_mgdl_per_u=120.0,
        sensitivity_ratio=0.95,
        carbs_required_g=carbs_required_g,
        reason="...",
    )
    return DeviceStatusSummary(
        device="openaps://Test",
        created_at="2026-05-23T12:00:00.000Z",
        algorithm=algo,
        predictions=BgPredictions(iob_minutes_ahead=60, iob_endpoint_mgdl=eventual_bg_mgdl),
    )


def _current(mgdl: int = 120) -> CurrentGlucose:
    return CurrentGlucose(
        sgv_mgdl=mgdl,
        sgv_mmol=round(mgdl / 18, 1),
        direction="Flat",
        trend_arrow="→",
        date_iso="2026-05-23T12:00:00.000Z",
        minutes_ago=2,
    )


def _iobcob(iob: float = 1.0, cob: float = 0.0) -> IobCob:
    return IobCob(iob_u=iob, cob_g=cob, source="openaps", as_of_iso="2026-05-23T12:00:00.000Z")


def _default_synthesis_kwargs(**overrides):
    base = dict(
        window_days=14,
        current_glucose=_current(),
        iob_cob=_iobcob(),
        device_status=_device_status(),
        stats_window=_stats(),
        isf_check=_isf(),
        effective_isf_check=_eff_isf(),
        cr_check=_cr(),
        yesterday_cv_percent=42.0,
        week_compare=None,
        overnight_low_count=0,
        post_meal_spike_count=0,
        dawn_phenomenon_count=0,
        compression_count=0,
        days_examined=7,
    )
    base.update(overrides)
    return base


# ---- Rule 1: Profile vs effective ISF divergence ---------------------------


def test_rule1_profile_correct_but_effective_isf_off() -> None:
    """Profile ISF ratio within ±15% AND effective_isf overall ratio > 1.15 →
    surface as a cross-tool insight."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            isf_check=_isf(ratio=1.05),  # within ±15%
            effective_isf_check=_eff_isf(overall_ratio=1.45),
        )
    )
    insights = [i for i in s.cross_tool_insights if "Profile ISF" in i.headline]
    assert len(insights) == 1
    assert insights[0].confidence == "high"
    assert "Adjustment Factor" in insights[0].suggested_question


def test_rule1_does_not_fire_when_profile_isf_also_off() -> None:
    """If profile ISF is itself off, the divergence story doesn't hold."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            isf_check=_isf(ratio=1.5),  # NOT within ±15%
            effective_isf_check=_eff_isf(overall_ratio=1.45),
        )
    )
    insights = [i for i in s.cross_tool_insights if "Profile ISF" in i.headline]
    assert len(insights) == 0


# ---- Rule 2: BG-curve issue ------------------------------------------------


def test_rule2_in_target_ok_but_above_target_hot() -> None:
    """in_target ratio ≈ 1.0 + above_target > 1.30 → BG-curve insight."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            effective_isf_check=_eff_isf(
                overall_ratio=1.20,
                in_target_ratio=1.0,
                above_target_ratio=1.5,
                above_target_n=20,
            ),
        )
    )
    insights = [i for i in s.cross_tool_insights if "BG-curve" in i.headline]
    assert len(insights) == 1


def test_rule2_does_not_fire_with_insufficient_above_target_samples() -> None:
    """Below 5 samples in above_target, we don't make the call."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            effective_isf_check=_eff_isf(
                in_target_ratio=1.0,
                above_target_ratio=1.5,
                above_target_n=3,
            ),
        )
    )
    insights = [i for i in s.cross_tool_insights if "BG-curve" in i.headline]
    assert len(insights) == 0


# ---- Rule 3: Severe-hypo cluster -------------------------------------------


def test_rule3_severe_hypo_cluster_critical_alert() -> None:
    """4+ overnight lows AND TBR<54 > 1% → critical alert + suggested question."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            overnight_low_count=4,
            stats_window=_stats(tbr_lt54=1.4),
        )
    )
    alerts = [a for a in s.alerts if a.category == "severe_hypo_cluster"]
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert any("TBR<54" in q for q in s.suggested_questions)


def test_rule3_does_not_fire_with_low_tbr_lt54() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            overnight_low_count=4,
            stats_window=_stats(tbr_lt54=0.5),  # below 1% threshold
        )
    )
    assert not any(a.category == "severe_hypo_cluster" for a in s.alerts)


# ---- Rule 4: Active rescue-carb request ------------------------------------


def test_rule4_carbs_required_critical_alert() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            device_status=_device_status(eventual_bg_mgdl=45, carbs_required_g=15),
        )
    )
    alerts = [a for a in s.alerts if a.category == "rescue_carbs_requested"]
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert "15g" in alerts[0].summary


def test_rule4_does_not_fire_when_no_carbs_requested() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            device_status=_device_status(carbs_required_g=None),
        )
    )
    assert not any(a.category == "rescue_carbs_requested" for a in s.alerts)


# ---- Rule 5: Active pending low --------------------------------------------


def test_rule5_pending_low_warning() -> None:
    """eventual_bg < 70 AND iob > 0 → warning alert."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            device_status=_device_status(eventual_bg_mgdl=50),
            iob_cob=_iobcob(iob=0.5),
        )
    )
    alerts = [a for a in s.alerts if a.category == "predicted_low"]
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"


def test_rule5_does_not_fire_with_zero_iob() -> None:
    """Without active IOB, the prediction has no insulin to act on."""
    s = build_synthesis(
        **_default_synthesis_kwargs(
            device_status=_device_status(eventual_bg_mgdl=50),
            iob_cob=_iobcob(iob=0),
        )
    )
    assert not any(a.category == "predicted_low" for a in s.alerts)


# ---- Rule 6: High-CV yesterday vs window -----------------------------------


def test_rule6_yesterday_high_cv_insight() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            yesterday_cv_percent=52.0,
            stats_window=_stats(cv=42.0),
        )
    )
    assert any("variability" in i.headline.lower() for i in s.cross_tool_insights)


def test_rule6_does_not_fire_when_yesterday_in_line() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            yesterday_cv_percent=44.0,
            stats_window=_stats(cv=42.0),
        )
    )
    assert not any("variability" in i.headline.lower() for i in s.cross_tool_insights)


# ---- Rule 7: CR signal contamination ---------------------------------------


def test_rule7_cr_signal_disagree_insight() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(cr_check=_cr(disagree=True))
    )
    assert any("Carb-ratio signal" in i.headline for i in s.cross_tool_insights)


def test_rule7_quiet_when_cr_signals_agree() -> None:
    s = build_synthesis(**_default_synthesis_kwargs(cr_check=_cr(disagree=False)))
    assert not any("Carb-ratio signal" in i.headline for i in s.cross_tool_insights)


# ---- Rule 8: Post-meal spike persistence -----------------------------------


def test_rule8_post_meal_spike_persistent() -> None:
    s = build_synthesis(**_default_synthesis_kwargs(post_meal_spike_count=7))
    insights = [i for i in s.cross_tool_insights if "Post-meal spikes" in i.headline]
    assert len(insights) == 1
    assert "pre-bolus" in insights[0].detail.lower()


def test_rule8_does_not_fire_with_occasional_spikes() -> None:
    s = build_synthesis(**_default_synthesis_kwargs(post_meal_spike_count=2))
    assert not any("Post-meal spikes" in i.headline for i in s.cross_tool_insights)


# ---- Alert ordering --------------------------------------------------------


def test_alerts_sorted_critical_first() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            device_status=_device_status(eventual_bg_mgdl=45, carbs_required_g=15),  # critical
            iob_cob=_iobcob(iob=0.5),  # rule 5 also triggers (warning)
        )
    )
    # First alert should be critical (rescue_carbs)
    assert s.alerts[0].severity == "critical"
    # warning(s) come after
    assert any(a.severity == "warning" for a in s.alerts[1:])


# ---- Snapshot fields propagation ------------------------------------------


def test_snapshot_fields_populated_from_inputs() -> None:
    s = build_synthesis(
        **_default_synthesis_kwargs(
            current_glucose=_current(mgdl=110),
            iob_cob=_iobcob(iob=1.5, cob=20.0),
        )
    )
    assert s.current_glucose_mgdl == 110
    assert s.iob_u == 1.5
    assert s.cob_g == 20.0
    # Algorithm state passes through too
    assert s.aaps_running_dynamic_isf is True


def test_safety_disclaimer_always_present() -> None:
    s = build_synthesis(**_default_synthesis_kwargs())
    assert "NOT medical advice" in s.safety_disclaimer
