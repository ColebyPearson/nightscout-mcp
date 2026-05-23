"""Phase 2 glucose analytics — pure functions, no HTTP, no MCP coupling.

Each function takes already-fetched SGVs and/or treatments and returns a
typed analysis. Tools in tools/analytics.py do the HTTP fetching and call
into these.

Intentionally simple algorithms — these run inside an LLM tool call where
the LLM is the higher-order pattern interpreter. We surface signals, not
diagnoses.
"""

from __future__ import annotations

import statistics
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

from .models import (
    CompressionAnalysis,
    DailyReport,
    DetectedPatterns,
    GlucoseStats,
    IsfDerivation,
    MealAnalysis,
    OvernightAnalysis,
    Pattern,
    PeriodComparison,
    Sgv,
    SuspectedCompression,
    Treatment,
    parse_iso_to_utc,
)
from .stats import compute_stats
from .units import mgdl_to_mmol

# Compression-low heuristic thresholds (tuned conservatively).
COMPRESSION_DROP_MGDL_PER_MIN = 2.0  # >2 mg/dL/min downward
COMPRESSION_RECOVERY_MGDL_PER_MIN = 2.0  # then bounces back as fast
COMPRESSION_MIN_DROP_MGDL = 30
COMPRESSION_MAX_DURATION_MIN = 30

# Dawn phenomenon: BG rising from ~03:00 to wake (~07:00) by >X mg/dL
DAWN_RISE_THRESHOLD_MGDL = 30


def daily_report(
    date_str: str,
    readings: list[Sgv],
    treatments: list[Treatment],
    tir_low: int = 70,
    tir_high: int = 180,
) -> DailyReport:
    """Roll up one calendar day into a single report.

    `notes` filters out auto-generated AAPS/Loop notes — Profile Switch
    rows that just echo the profile name (e.g. "regular (60%)") aren't
    information for an LLM, they're sync noise.
    """
    stats = compute_stats(readings, window_hours=24, tir_low=tir_low, tir_high=tir_high)
    total_insulin = sum(t.insulin for t in treatments if t.insulin)
    total_carbs = sum(t.carbs for t in treatments if t.carbs)
    notes = [
        t.notes
        for t in treatments
        if t.notes
        and t.event_type not in ("Profile Switch", "Temporary Override", "Temp Basal")
    ]
    return DailyReport(
        date=date_str,
        stats=stats,
        treatment_count=len(treatments),
        total_insulin_u=round(total_insulin, 2),
        total_carbs_g=round(total_carbs, 1),
        notes=notes,
    )


def compare_periods(
    label_a: str,
    readings_a: list[Sgv],
    label_b: str,
    readings_b: list[Sgv],
    hours_each: int,
    tir_low: int = 70,
    tir_high: int = 180,
) -> PeriodComparison:
    """Side-by-side stats with a plain-English summary."""
    a = compute_stats(readings_a, window_hours=hours_each, tir_low=tir_low, tir_high=tir_high)
    b = compute_stats(readings_b, window_hours=hours_each, tir_low=tir_low, tir_high=tir_high)
    delta_mean = round(b.mean_mgdl - a.mean_mgdl, 1)
    delta_tir = round(b.tir_percent - a.tir_percent, 1)
    delta_gmi = round(b.gmi_percent - a.gmi_percent, 2)

    parts: list[str] = []
    if delta_tir > 1:
        parts.append(f"TIR +{delta_tir}pp")
    elif delta_tir < -1:
        parts.append(f"TIR {delta_tir}pp")
    if abs(delta_gmi) >= 0.05:
        parts.append(f"GMI {delta_gmi:+.2f}pp")
    if abs(delta_mean) >= 5:
        parts.append(f"mean {delta_mean:+.0f} mg/dL")
    summary = (
        f"{label_b} vs {label_a}: " + ", ".join(parts)
        if parts
        else f"{label_b} and {label_a} are statistically similar"
    )

    return PeriodComparison(
        period_a_label=label_a,
        period_b_label=label_b,
        period_a=a,
        period_b=b,
        delta_mean_mgdl=delta_mean,
        delta_tir_pp=delta_tir,
        delta_gmi_pp=delta_gmi,
        improvement_summary=summary,
    )


def analyze_meal(
    meal_time: datetime,
    meal: Treatment | None,
    readings_window: list[Sgv],
    window_hours: int,
    tir_low: int = 70,
    tir_high: int = 180,
) -> MealAnalysis:
    """Glucose response in the N hours after a meal.

    `meal` is the matched Treatment (carb correction or meal bolus), if found.
    `readings_window` is SGVs from (meal_time - 30min) through
    (meal_time + window_hours). We pick pre_meal as the closest reading
    ≤ meal_time and peak as the max within the post-meal window.
    """
    notes: list[str] = []
    pre_meal: Sgv | None = None
    post_meal: list[Sgv] = []
    for r in readings_window:
        ts = parse_iso_to_utc(r.date_iso)
        if ts <= meal_time:
            if pre_meal is None or ts > parse_iso_to_utc(pre_meal.date_iso):
                pre_meal = r
        else:
            post_meal.append(r)

    if not post_meal:
        # Caller will see an empty/degenerate result rather than an error.
        notes.append("No CGM readings in the post-meal window.")
        return MealAnalysis(
            meal_time_iso=meal_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            window_hours=window_hours,
            carbs_g=meal.carbs if meal else None,
            insulin_u=meal.insulin if meal else None,
            pre_meal_mgdl=pre_meal.sgv_mgdl if pre_meal else None,
            pre_meal_mmol=pre_meal.sgv_mmol if pre_meal else None,
            peak_mgdl=pre_meal.sgv_mgdl if pre_meal else 0,
            peak_mmol=pre_meal.sgv_mmol if pre_meal else 0.0,
            peak_at_iso=pre_meal.date_iso if pre_meal else meal_time.isoformat(),
            time_to_peak_minutes=0,
            rise_mgdl=0,
            end_mgdl=pre_meal.sgv_mgdl if pre_meal else 0,
            end_mmol=pre_meal.sgv_mmol if pre_meal else 0.0,
            in_range_at_end=False,
            notes=notes,
        )

    peak = max(post_meal, key=lambda r: r.sgv_mgdl)
    end = post_meal[-1]
    peak_ts = parse_iso_to_utc(peak.date_iso)
    ttp_minutes = max(0, int((peak_ts - meal_time).total_seconds() // 60))
    rise = peak.sgv_mgdl - (pre_meal.sgv_mgdl if pre_meal else peak.sgv_mgdl)

    if rise > 80:
        notes.append("Large post-meal rise (>80 mg/dL) — bolus timing/dose may be undermatched.")
    if peak.sgv_mgdl > 250:
        notes.append("Peak above 250 mg/dL.")
    if end.sgv_mgdl < tir_low:
        notes.append("Ended below target range — possible over-correction.")
    if pre_meal and pre_meal.sgv_mgdl < tir_low:
        notes.append("Pre-meal BG was already below range.")

    return MealAnalysis(
        meal_time_iso=meal_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        window_hours=window_hours,
        carbs_g=meal.carbs if meal else None,
        insulin_u=meal.insulin if meal else None,
        pre_meal_mgdl=pre_meal.sgv_mgdl if pre_meal else None,
        pre_meal_mmol=pre_meal.sgv_mmol if pre_meal else None,
        peak_mgdl=peak.sgv_mgdl,
        peak_mmol=peak.sgv_mmol,
        peak_at_iso=peak.date_iso,
        time_to_peak_minutes=ttp_minutes,
        rise_mgdl=rise,
        end_mgdl=end.sgv_mgdl,
        end_mmol=end.sgv_mmol,
        in_range_at_end=tir_low <= end.sgv_mgdl <= tir_high,
        notes=notes,
    )


def _readings_at_hour(readings: list[Sgv], target_hour: int) -> Sgv | None:
    """Find the SGV closest to a given UTC hour from a sorted list."""
    if not readings:
        return None
    target_minute = target_hour * 60
    best: Sgv | None = None
    best_diff = 10**9
    for r in readings:
        ts = parse_iso_to_utc(r.date_iso)
        minute = ts.hour * 60 + ts.minute
        diff = abs(minute - target_minute)
        if diff < best_diff:
            best_diff = diff
            best = r
    return best if best_diff <= 60 else None  # within 1 hour


def overnight_analysis(date_iso: str, readings: list[Sgv]) -> OvernightAnalysis:
    """Characterize the 00:00–07:00 UTC window for the given date."""
    if not readings:
        return OvernightAnalysis(
            night_date=date_iso,
            reading_count=0,
            start_mgdl=None,
            end_mgdl=None,
            drift_mgdl=None,
            min_mgdl=None,
            max_mgdl=None,
            time_below_70_minutes=0,
            time_below_54_minutes=0,
            dawn_rise_mgdl=None,
            flat_pct=0.0,
        )

    sorted_r = sorted(readings, key=lambda r: parse_iso_to_utc(r.date_iso))
    start_v = sorted_r[0].sgv_mgdl
    end_v = sorted_r[-1].sgv_mgdl
    values = [r.sgv_mgdl for r in sorted_r]

    # Approximate "time below" as 5min × number-of-readings below threshold.
    # CGM cadence is typically 5min so this is close enough for an LLM signal.
    below_70 = sum(1 for v in values if v < 70) * 5
    below_54 = sum(1 for v in values if v < 54) * 5

    at_3 = _readings_at_hour(sorted_r, 3)
    at_7 = _readings_at_hour(sorted_r, 7)
    dawn_rise = (at_7.sgv_mgdl - at_3.sgv_mgdl) if (at_3 and at_7) else None

    deltas = [abs(values[i + 1] - values[i]) for i in range(len(values) - 1)]
    flat_pct = (sum(1 for d in deltas if d <= 5) / len(deltas) * 100) if deltas else 0.0

    return OvernightAnalysis(
        night_date=date_iso,
        reading_count=len(sorted_r),
        start_mgdl=start_v,
        end_mgdl=end_v,
        drift_mgdl=end_v - start_v,
        min_mgdl=min(values),
        max_mgdl=max(values),
        time_below_70_minutes=below_70,
        time_below_54_minutes=below_54,
        dawn_rise_mgdl=dawn_rise,
        flat_pct=round(flat_pct, 1),
    )


def detect_patterns(
    days: int, daily_groups: list[tuple[str, list[Sgv]]]
) -> DetectedPatterns:
    """Detect recurring patterns across multiple days.

    `daily_groups` is a list of (date_str, readings_for_that_day) tuples.
    """
    overnight_low_times: list[str] = []
    overnight_low_values: list[int] = []
    dawn_rises: list[int] = []
    dawn_rise_times: list[str] = []
    spike_times: list[str] = []
    spike_values: list[int] = []

    for date_str, readings in daily_groups:
        if not readings:
            continue
        # Overnight low: any sgv < 70 between 00:00 and 06:00 UTC
        for r in readings:
            ts = parse_iso_to_utc(r.date_iso)
            if ts.hour < 6 and r.sgv_mgdl < 70:
                overnight_low_times.append(r.date_iso)
                overnight_low_values.append(r.sgv_mgdl)
                break  # one per night
        # Dawn rise
        on = overnight_analysis(date_str, readings)
        if on.dawn_rise_mgdl is not None and on.dawn_rise_mgdl > DAWN_RISE_THRESHOLD_MGDL:
            dawn_rises.append(on.dawn_rise_mgdl)
            dawn_rise_times.append(date_str)
        # Post-meal spikes: ANY rapid rise > 50 mg/dL in any 30-min window
        sorted_r = sorted(readings, key=lambda r: parse_iso_to_utc(r.date_iso))
        for i in range(len(sorted_r) - 6):  # ~30 min at 5-min cadence
            window = sorted_r[i : i + 7]
            rise = window[-1].sgv_mgdl - window[0].sgv_mgdl
            if rise > 50:
                spike_times.append(window[0].date_iso)
                spike_values.append(rise)
                break  # one per day per pattern

    patterns: list[Pattern] = []
    if overnight_low_times:
        patterns.append(
            Pattern(
                type="overnight_low",
                occurrence_count=len(overnight_low_times),
                sample_times_iso=overnight_low_times[:5],
                avg_value_mgdl=round(statistics.fmean(overnight_low_values), 1),
                description=f"Glucose dipped to avg {round(statistics.fmean(overnight_low_values))} mg/dL between 00:00-06:00 on {len(overnight_low_times)} of {days} nights.",
            )
        )
    if dawn_rises:
        patterns.append(
            Pattern(
                type="dawn_phenomenon",
                occurrence_count=len(dawn_rises),
                sample_times_iso=dawn_rise_times[:5],
                avg_value_mgdl=round(statistics.fmean(dawn_rises), 1),
                description=f"Dawn rise > {DAWN_RISE_THRESHOLD_MGDL} mg/dL on {len(dawn_rises)} of {days} mornings (03:00 → 07:00). Consider increasing pre-dawn basal or reviewing overnight insulin needs.",
            )
        )
    if spike_times:
        patterns.append(
            Pattern(
                type="post_meal_spike",
                occurrence_count=len(spike_times),
                sample_times_iso=spike_times[:5],
                avg_value_mgdl=round(statistics.fmean(spike_values), 1),
                description=f"Rapid rises >50 mg/dL within 30 min seen on {len(spike_times)} of {days} days. Bolus-to-eat timing may benefit from a longer pre-bolus.",
            )
        )

    return DetectedPatterns(days_analyzed=days, patterns=patterns)


def insulin_sensitivity_check(
    treatments: list[Treatment],
    sgvs: list[Sgv],
    profile_isf_mmol: float | None,
    dia_hours: float = 5.0,
) -> IsfDerivation:
    """Derive *real* ISF from correction-bolus outcomes.

    For each isolated correction bolus (no carbs within ±60 min):
      1. Find pre-bolus BG (closest SGV ≤ bolus time).
      2. Find post-bolus minimum BG within DIA hours, capped at 5h.
      3. ISF_sample = (pre - min) / insulin_units, in mg/dL per U.
    Average all eligible samples.

    Filters out: samples where the user ate within DIA, where BG was rising
    (likely missed carbs), or where the drop is implausibly large (>500
    mg/dL per U — almost certainly a calc error).
    """
    sgv_sorted = sorted(sgvs, key=lambda r: parse_iso_to_utc(r.date_iso))
    samples_mgdl_per_u: list[float] = []

    for tx in treatments:
        if not tx.insulin or tx.insulin <= 0:
            continue
        # Require correction-type (no carbs) OR explicitly named Correction Bolus
        if tx.carbs and tx.carbs > 0:
            continue
        if "Correction" not in tx.event_type and tx.event_type != "Bolus":
            continue

        try:
            tx_time = parse_iso_to_utc(tx.created_at)
        except Exception:
            continue

        # Exclude if any carb entry within ±60 min
        ate_nearby = any(
            other.carbs and other.carbs > 0
            and abs((parse_iso_to_utc(other.created_at) - tx_time).total_seconds()) <= 3600
            for other in treatments
            if other.created_at != tx.created_at
        )
        if ate_nearby:
            continue

        # Find pre-bolus reading and post-bolus minimum
        pre_sgv: Sgv | None = None
        post_window_end = tx_time + timedelta(hours=min(dia_hours, 5.0))
        post_sgvs: list[Sgv] = []
        for s in sgv_sorted:
            ts = parse_iso_to_utc(s.date_iso)
            if ts <= tx_time:
                pre_sgv = s
            elif ts <= post_window_end:
                post_sgvs.append(s)

        if not pre_sgv or not post_sgvs:
            continue

        min_post = min(post_sgvs, key=lambda r: r.sgv_mgdl)
        drop = pre_sgv.sgv_mgdl - min_post.sgv_mgdl
        if drop <= 0:
            continue  # BG rose — likely missed-carb contamination
        per_unit = drop / tx.insulin
        if per_unit > 500:
            continue  # implausibly large

        samples_mgdl_per_u.append(per_unit)

    if not samples_mgdl_per_u:
        return IsfDerivation(
            sample_count=0,
            derived_isf_mgdl_per_unit=None,
            derived_isf_mmol_per_unit=None,
            profile_isf_mmol_per_unit=profile_isf_mmol,
            ratio_derived_over_profile=None,
            confidence="low",
            recommendation="Not enough isolated correction boluses to derive a real-world ISF. Need at least a handful of correction-only boluses without carbs within ±60 min.",
        )

    derived_mgdl = statistics.fmean(samples_mgdl_per_u)
    derived_mmol = mgdl_to_mmol(derived_mgdl)

    if len(samples_mgdl_per_u) < 3:
        confidence = "low"
    elif len(samples_mgdl_per_u) < 8:
        confidence = "medium"
    else:
        confidence = "high"

    ratio = (derived_mmol / profile_isf_mmol) if profile_isf_mmol else None
    if ratio is None:
        recommendation = "No profile ISF available to compare against."
    elif ratio > 1.15:
        recommendation = "Derived ISF suggests you're MORE sensitive than your profile says (each unit drops you further). Consider lowering profile ISF or reviewing for overcorrections."
    elif ratio < 0.85:
        recommendation = "Derived ISF suggests you're LESS sensitive than your profile says (each unit drops you less). Consider raising profile ISF."
    else:
        recommendation = "Derived ISF is consistent with your profile (within ±15%)."

    return IsfDerivation(
        sample_count=len(samples_mgdl_per_u),
        derived_isf_mgdl_per_unit=round(derived_mgdl, 1),
        derived_isf_mmol_per_unit=round(derived_mmol, 1),
        profile_isf_mmol_per_unit=profile_isf_mmol,
        ratio_derived_over_profile=round(ratio, 2) if ratio is not None else None,
        confidence=confidence,
        recommendation=recommendation,
    )


def compression_low_analysis(
    days: int, sgvs: list[Sgv]
) -> CompressionAnalysis:
    """Flag CGM dips that look like sensor-compression artifacts.

    Heuristic: a fast drop (≥30 mg/dL in ≤15 min) to below 70 mg/dL
    followed by an equally fast recovery (≥30 mg/dL in ≤15 min) within
    a 30-min total episode duration. Real hypos don't recover that fast.
    """
    sorted_r = sorted(sgvs, key=lambda r: parse_iso_to_utc(r.date_iso))
    suspected: list[SuspectedCompression] = []

    i = 0
    # Need at least: start row + min candidate + recovery candidate = 3 rows.
    while i < len(sorted_r) - 2:
        # Look for fast drop
        start = sorted_r[i]
        # Scan forward to find min within 15 min
        min_idx = i
        for j in range(i + 1, min(i + 4, len(sorted_r))):  # next ~15 min
            if sorted_r[j].sgv_mgdl < sorted_r[min_idx].sgv_mgdl:
                min_idx = j
        min_r = sorted_r[min_idx]
        drop = start.sgv_mgdl - min_r.sgv_mgdl
        if drop < COMPRESSION_MIN_DROP_MGDL or min_r.sgv_mgdl >= 70:
            i += 1
            continue
        drop_minutes = max(
            1,
            int(
                (parse_iso_to_utc(min_r.date_iso) - parse_iso_to_utc(start.date_iso)).total_seconds()
                // 60
            ),
        )
        drop_rate = drop / drop_minutes
        if drop_rate < COMPRESSION_DROP_MGDL_PER_MIN:
            i += 1
            continue
        # Look for recovery within 15 min after min
        recovery_idx = min_idx
        for j in range(min_idx + 1, min(min_idx + 4, len(sorted_r))):
            if sorted_r[j].sgv_mgdl > sorted_r[recovery_idx].sgv_mgdl:
                recovery_idx = j
        recovery_r = sorted_r[recovery_idx]
        rise = recovery_r.sgv_mgdl - min_r.sgv_mgdl
        recovery_minutes = max(
            1,
            int(
                (parse_iso_to_utc(recovery_r.date_iso) - parse_iso_to_utc(min_r.date_iso)).total_seconds()
                // 60
            ),
        )
        recovery_rate = rise / recovery_minutes
        total_duration = drop_minutes + recovery_minutes
        if (
            rise >= COMPRESSION_MIN_DROP_MGDL
            and recovery_rate >= COMPRESSION_RECOVERY_MGDL_PER_MIN
            and total_duration <= COMPRESSION_MAX_DURATION_MIN
        ):
            suspected.append(
                SuspectedCompression(
                    start_iso=start.date_iso,
                    min_iso=min_r.date_iso,
                    min_mgdl=min_r.sgv_mgdl,
                    recovery_iso=recovery_r.date_iso,
                    drop_rate_mgdl_per_min=round(drop_rate, 1),
                    recovery_rate_mgdl_per_min=round(recovery_rate, 1),
                    duration_minutes=total_duration,
                )
            )
            i = recovery_idx + 1
        else:
            i += 1

    return CompressionAnalysis(
        days_analyzed=days,
        suspected=suspected,
        note=(
            "Heuristic only — flagged dips MAY be sensor-compression artifacts "
            "(common at night from rolling onto the sensor). Verify against "
            "a fingerstick before treating as false. Real hypos don't usually "
            "recover by >30 mg/dL within 15 min without intervention."
        ),
    )
