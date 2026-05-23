"""Pure synthesis logic for the daily_synthesis tool.

Takes already-computed outputs from the read + analytics surface and applies
8 rule-based detections to surface cross-tool patterns. No HTTP, no MCP
coupling — the tool wrapper in tools/synthesis.py does the fetching and
delegates here.

Each rule corresponds to an item in issue #15.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    Alert,
    CrDerivation,
    CrossToolInsight,
    CurrentGlucose,
    DailySynthesis,
    DeviceStatusSummary,
    EffectiveIsfDerivation,
    GlucoseStats,
    IobCob,
    IsfDerivation,
    PeriodComparison,
)


def _severity_rank(s: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(s, 3)


def build_synthesis(
    *,
    window_days: int,
    current_glucose: CurrentGlucose | None,
    iob_cob: IobCob | None,
    device_status: DeviceStatusSummary | None,
    stats_window: GlucoseStats,
    isf_check: IsfDerivation,
    effective_isf_check: EffectiveIsfDerivation,
    cr_check: CrDerivation,
    yesterday_cv_percent: float | None,
    week_compare: PeriodComparison | None,
    overnight_low_count: int,
    post_meal_spike_count: int,
    dawn_phenomenon_count: int,
    compression_count: int,
    days_examined: int = 7,
) -> DailySynthesis:
    """Build a DailySynthesis from already-computed sub-tool outputs.

    `days_examined` is the lookback window used by `detect_patterns`
    (typically 7), used in alert text. `window_days` is the broader
    stats window (typically 14).
    """
    alerts: list[Alert] = []
    insights: list[CrossToolInsight] = []
    questions: list[str] = []

    # ---- Snapshot extraction -----------------------------------------------
    algo = device_status.algorithm if device_status else None

    # ---- Rule 4: Active rescue-carb request (CRITICAL) ---------------------
    if algo and algo.carbs_required_g is not None and algo.carbs_required_g > 0:
        eventual_mgdl = algo.eventual_bg_mgdl
        eventual_str = (
            f" (predicted to reach {eventual_mgdl} mg/dL"
            f"/{algo.eventual_bg_mmol} mmol/L without intervention)"
            if eventual_mgdl is not None
            else ""
        )
        alerts.append(
            Alert(
                severity="critical",
                category="rescue_carbs_requested",
                summary=f"AAPS is requesting {algo.carbs_required_g:.0f}g of rescue carbs right now",
                detail=(
                    f"AAPS's current loop cycle has carbsReq={algo.carbs_required_g:.0f}g{eventual_str}. "
                    "Check the AAPS app for the active alert and confirm against a fingerstick "
                    "before acting."
                ),
                source_tools=["get_device_status"],
            )
        )

    # ---- Rule 5: Active pending low (WARNING) ------------------------------
    if (
        algo
        and algo.eventual_bg_mgdl is not None
        and algo.eventual_bg_mgdl < 70
        and iob_cob
        and iob_cob.iob_u is not None
        and iob_cob.iob_u > 0
    ):
        alerts.append(
            Alert(
                severity="warning",
                category="predicted_low",
                summary=(
                    f"AAPS predicts BG dropping to {algo.eventual_bg_mgdl} mg/dL"
                    f"/{algo.eventual_bg_mmol} mmol/L"
                ),
                detail=(
                    f"Current IOB {iob_cob.iob_u:.2f}U is still active and AAPS's eventual-BG "
                    f"prediction is below 70 mg/dL. Monitor closely; AAPS may already be "
                    "zero-temping or requesting carbs."
                ),
                source_tools=["get_device_status", "get_iob_cob"],
            )
        )

    # ---- Rule 3: Severe-hypo cluster (CRITICAL) ----------------------------
    if overnight_low_count >= 3 and stats_window.tbr_lt54_percent > 1.0:
        # detect_patterns groups by UTC date — when the window spans midnight,
        # the count can exceed days_examined by 1. Clamp for display.
        displayed_overnight = min(overnight_low_count, days_examined)
        alerts.append(
            Alert(
                severity="critical",
                category="severe_hypo_cluster",
                summary=(
                    f"Severe overnight lows on {displayed_overnight} of {days_examined} nights, "
                    f"TBR<54 = {stats_window.tbr_lt54_percent:.1f}%"
                ),
                detail=(
                    f"International consensus targets <1% time-below-54-mg/dL; you are at "
                    f"{stats_window.tbr_lt54_percent:.1f}% over the last {window_days} days. "
                    "Cross-check against the compression-low analysis to separate real treated "
                    "hypos from sensor-compression artifacts."
                ),
                source_tools=["detect_patterns", "get_glucose_stats", "overnight_analysis"],
            )
        )
        questions.append(
            f"TBR<54 mg/dL is {stats_window.tbr_lt54_percent:.1f}% over {window_days} days "
            "(above the 1% target). The severe lows cluster overnight. What's the safest first lever?"
        )

    # ---- Rule 1: Profile vs effective ISF divergence ----------------------
    eff_overall = effective_isf_check.overall_ratio_realized_over_effective
    isf_ratio = isf_check.ratio_derived_over_profile
    if (
        isf_ratio is not None
        and 0.85 <= isf_ratio <= 1.15
        and eff_overall is not None
        and eff_overall > 1.15
        and effective_isf_check.confidence in ("medium", "high")
    ):
        insights.append(
            CrossToolInsight(
                headline="Profile ISF is correct, but Dynamic ISF is over-aggressive",
                detail=(
                    f"insulin_sensitivity_check shows ratio {isf_ratio:.2f} vs profile ISF — "
                    "within ±15%, so the profile is calibrated. But effective_isf_check shows "
                    f"AAPS's *computed* effective ISF averaging "
                    f"{effective_isf_check.avg_effective_isf_mmol_per_unit:.1f} mmol/L/U while "
                    f"realized response averaged "
                    f"{effective_isf_check.avg_realized_isf_mmol_per_unit:.1f} mmol/L/U "
                    f"(ratio {eff_overall:.2f}). The Dynamic ISF Adjustment Factor — not the "
                    "profile ISF — is the lever."
                ),
                confidence=effective_isf_check.confidence,
                relevant_tools=["insulin_sensitivity_check", "effective_isf_check"],
                suggested_question=(
                    "My profile ISF is within ±15% of my real-world correction outcomes, but "
                    f"Dynamic ISF effective is averaging {eff_overall:.2f}× my real "
                    "responsiveness. Should I lower the Adjustment Factor?"
                ),
            )
        )

    # ---- Rule 2: Dynamic ISF BG-curve issue --------------------------------
    bands = {b.band_label: b for b in effective_isf_check.by_bg_band}
    in_target = bands.get("in_target")
    above_target = bands.get("above_target")
    if (
        in_target
        and in_target.ratio_realized_over_effective is not None
        and 0.85 <= in_target.ratio_realized_over_effective <= 1.15
        and above_target
        and above_target.ratio_realized_over_effective is not None
        and above_target.ratio_realized_over_effective > 1.30
        and above_target.sample_count >= 5
    ):
        insights.append(
            CrossToolInsight(
                headline="Dynamic ISF BG-curve is mis-calibrated at high BG",
                detail=(
                    f"In-target band tracks well "
                    f"(ratio {in_target.ratio_realized_over_effective:.2f}, "
                    f"{in_target.sample_count} samples), but above-target band runs hot "
                    f"(ratio {above_target.ratio_realized_over_effective:.2f}, "
                    f"{above_target.sample_count} samples). This is a BG-curve dampening "
                    "issue, separate from the global Adjustment Factor."
                ),
                confidence="high" if above_target.sample_count >= 10 else "medium",
                relevant_tools=["effective_isf_check"],
                suggested_question=(
                    f"My above-target-band ratio is {above_target.ratio_realized_over_effective:.2f} "
                    f"vs in-target {in_target.ratio_realized_over_effective:.2f}. Is the BG-curve "
                    "dampening in Dynamic ISF the right lever rather than the Adjustment Factor?"
                ),
            )
        )

    # ---- Rule 6: High-CV yesterday vs window ------------------------------
    if (
        yesterday_cv_percent is not None
        and stats_window.cv_percent > 0
        and yesterday_cv_percent - stats_window.cv_percent >= 5.0
    ):
        insights.append(
            CrossToolInsight(
                headline="Yesterday's variability notably above your baseline",
                detail=(
                    f"Yesterday's CV was {yesterday_cv_percent:.1f}% vs your "
                    f"{window_days}-day window of {stats_window.cv_percent:.1f}%. "
                    "Consider whether yesterday had unusual factors (illness, exercise, "
                    "stress, meal timing) before reading the day as a pattern."
                ),
                confidence="medium",
                relevant_tools=["get_daily_report", "get_glucose_stats"],
                suggested_question=None,
            )
        )

    # ---- Rule 7: CR signal contamination ----------------------------------
    if cr_check.sample_count > 0 and "Signals disagree" in cr_check.recommendation:
        insights.append(
            CrossToolInsight(
                headline="Carb-ratio signal can't be isolated — AAPS auto-corrections are dominant",
                detail=(
                    f"carb_ratio_check found {cr_check.sample_count} eligible meals but the "
                    "applied-CR and post-meal-residual signals point in opposite directions. "
                    "Meal-by-meal investigation via analyze_meal would be more informative "
                    "than a global CR change."
                ),
                confidence="high",
                relevant_tools=["carb_ratio_check", "analyze_meal"],
                suggested_question=None,
            )
        )

    # ---- Rule 8: Post-meal spike persistence ------------------------------
    if post_meal_spike_count >= 5:
        # Clamp display: detect_patterns groups by UTC date and can produce
        # counts > days_examined when the window straddles midnight.
        displayed_spikes = min(post_meal_spike_count, days_examined)
        insights.append(
            CrossToolInsight(
                headline=f"Post-meal spikes happening on {displayed_spikes} of {days_examined} days",
                detail=(
                    "Rapid BG rises >50 mg/dL within 30 min suggest pre-bolus timing or "
                    "carb-cover insufficient. Common levers: bolus 15-20 min before eating "
                    "for routine meals, or trial a slightly higher carb-cover ratio."
                ),
                confidence="high",
                relevant_tools=["detect_patterns"],
                suggested_question=(
                    f"Post-meal spikes >50 mg/dL on {displayed_spikes} of {days_examined} days. "
                    "Worth trialing a longer pre-bolus on routine meals?"
                ),
            )
        )

    # ---- Sort alerts by severity ------------------------------------------
    alerts.sort(key=lambda a: _severity_rank(a.severity))

    return DailySynthesis(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        window_days=window_days,
        current_glucose_mgdl=current_glucose.sgv_mgdl if current_glucose else None,
        current_glucose_mmol=current_glucose.sgv_mmol if current_glucose else None,
        current_trend_arrow=current_glucose.trend_arrow if current_glucose else None,
        minutes_since_last_reading=current_glucose.minutes_ago if current_glucose else None,
        iob_u=iob_cob.iob_u if iob_cob else None,
        cob_g=iob_cob.cob_g if iob_cob else None,
        aaps_predicted_eventual_bg_mgdl=algo.eventual_bg_mgdl if algo else None,
        aaps_predicted_eventual_bg_mmol=algo.eventual_bg_mmol if algo else None,
        aaps_running_dynamic_isf=algo.running_dynamic_isf if algo else None,
        aaps_effective_isf_mmol_per_u=algo.effective_isf_mmol_per_u if algo else None,
        aaps_target_bg_mgdl=algo.target_bg_mgdl if algo else None,
        aaps_target_bg_mmol=algo.target_bg_mmol if algo else None,
        alerts=alerts,
        stats_window=stats_window,
        yesterday_cv_percent=yesterday_cv_percent,
        week_over_week_summary=week_compare.improvement_summary if week_compare else None,
        recurring_overnight_lows=overnight_low_count,
        recurring_post_meal_spikes=post_meal_spike_count,
        recurring_dawn_phenomenon=dawn_phenomenon_count,
        suspected_compression_count=compression_count,
        cross_tool_insights=insights,
        suggested_questions=questions
        + [i.suggested_question for i in insights if i.suggested_question],
        raw_isf_check=isf_check,
        raw_effective_isf_check=effective_isf_check,
        raw_carb_ratio_check=cr_check,
    )
