"""Phase 2 glucose analytics — pure functions, no HTTP, no MCP coupling.

Each function takes already-fetched SGVs and/or treatments and returns a
typed analysis. Tools in tools/analytics.py do the HTTP fetching and call
into these.

Intentionally simple algorithms — these run inside an LLM tool call where
the LLM is the higher-order pattern interpreter. We surface signals, not
diagnoses.
"""

from __future__ import annotations

import contextlib
import statistics
from datetime import datetime, timedelta

from .models import (
    CompressionAnalysis,
    CrDerivation,
    DailyReport,
    DetectedPatterns,
    EffectiveIsfDerivation,
    HypoEpisode,
    HypoEpisodeReport,
    IsfBandSample,
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

# effective_isf_check: tolerance for matching a bolus to the most-recent prior
# devicestatus row carrying `openaps.suggested.sens`. AAPS publishes
# devicestatus every ~5 min when looping but uploaders can be flaky.
EFFECTIVE_ISF_SENS_MATCH_WINDOW_MIN = 15

# BG bands for effective_isf_check. Half-open intervals: a sample with pre-bolus
# BG exactly at `upper` falls into the NEXT band. Upper=None = open top.
ISF_BG_BANDS: list[tuple[str, int, int | None]] = [
    ("below_target", 0, 100),
    ("in_target", 100, 180),
    ("above_target", 180, None),
]

# Appended to any recommendation that proposes a settings change, matching the
# clinician-consult framing used by effective_isf_check / insulin_sensitivity_check.
_CR_SAFETY = " This is advisory only. Do not change AAPS/pump settings without consulting your healthcare provider."


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
        if t.notes and t.event_type not in ("Profile Switch", "Temporary Override", "Temp Basal")
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


def _local_hour_minute(ts: datetime, tz_offset_hours: float) -> tuple[int, int]:
    """Local wall-clock (hour, minute) for a UTC timestamp given a fixed offset."""
    local = ts + timedelta(hours=tz_offset_hours)
    return local.hour, local.minute


def _readings_at_hour(readings: list[Sgv], target_hour: int, tz_offset_hours: float = 0.0) -> Sgv | None:
    """Find the SGV closest to a given LOCAL hour from a sorted list.

    `tz_offset_hours` shifts each UTC timestamp to local wall-clock before
    matching; default 0.0 keeps the historical UTC behaviour.
    """
    if not readings:
        return None
    target_minute = target_hour * 60
    best: Sgv | None = None
    best_diff = 10**9
    for r in readings:
        ts = parse_iso_to_utc(r.date_iso)
        hour, minute = _local_hour_minute(ts, tz_offset_hours)
        diff = abs((hour * 60 + minute) - target_minute)
        if diff < best_diff:
            best_diff = diff
            best = r
    return best if best_diff <= 60 else None  # within 1 hour


def overnight_analysis(date_iso: str, readings: list[Sgv], tz_offset_hours: float = 0.0) -> OvernightAnalysis:
    """Characterize the overnight window for the given date.

    Dawn anchors (03:00 / 07:00) are matched in LOCAL wall-clock time via
    `tz_offset_hours`; default 0.0 is UTC. Zero/error SGVs (sensor warmup,
    calibration errors) are excluded so a bogus `0` never registers as a low.
    """
    readings = [r for r in readings if r.sgv_mgdl > 0]
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

    at_3 = _readings_at_hour(sorted_r, 3, tz_offset_hours)
    at_7 = _readings_at_hour(sorted_r, 7, tz_offset_hours)
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
    days: int,
    daily_groups: list[tuple[str, list[Sgv]]],
    tz_offset_hours: float = 0.0,
) -> DetectedPatterns:
    """Detect recurring patterns across multiple days.

    `daily_groups` is a list of (date_str, readings_for_that_day) tuples.
    Overnight/dawn windows are evaluated in LOCAL wall-clock time via
    `tz_offset_hours` (default 0.0 = UTC). Zero/error SGVs are excluded so a
    sensor-warmup `0` never registers as a false overnight low.
    """
    overnight_low_times: list[str] = []
    overnight_low_values: list[int] = []
    dawn_rises: list[int] = []
    dawn_rise_times: list[str] = []
    spike_times: list[str] = []
    spike_values: list[int] = []

    for date_str, day_readings in daily_groups:
        readings = [r for r in day_readings if r.sgv_mgdl > 0]
        if not readings:
            continue
        # Overnight low: any sgv < 70 between 00:00 and 06:00 LOCAL time
        for r in readings:
            ts = parse_iso_to_utc(r.date_iso)
            local_hour, _ = _local_hour_minute(ts, tz_offset_hours)
            if local_hour < 6 and r.sgv_mgdl < 70:
                overnight_low_times.append(r.date_iso)
                overnight_low_values.append(r.sgv_mgdl)
                break  # one per night
        # Dawn rise
        on = overnight_analysis(date_str, readings, tz_offset_hours)
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
                description=(
                    f"Glucose dipped to avg {round(statistics.fmean(overnight_low_values))} mg/dL "
                    f"between 00:00-06:00 local on {len(overnight_low_times)} of {days} nights."
                ),
            )
        )
    if dawn_rises:
        patterns.append(
            Pattern(
                type="dawn_phenomenon",
                occurrence_count=len(dawn_rises),
                sample_times_iso=dawn_rise_times[:5],
                avg_value_mgdl=round(statistics.fmean(dawn_rises), 1),
                description=(
                    f"Dawn rise > {DAWN_RISE_THRESHOLD_MGDL} mg/dL on {len(dawn_rises)} of {days} "
                    "mornings (03:00 → 07:00 local). Overnight/pre-dawn insulin adequacy may be "
                    "worth reviewing with your care team."
                ),
            )
        )
    if spike_times:
        patterns.append(
            Pattern(
                type="post_meal_spike",
                occurrence_count=len(spike_times),
                sample_times_iso=spike_times[:5],
                avg_value_mgdl=round(statistics.fmean(spike_values), 1),
                description=(
                    f"Rapid rises >50 mg/dL within 30 min seen on {len(spike_times)} of {days} days. "
                    "Bolus-to-eat timing may benefit from a longer pre-bolus."
                ),
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
            other.carbs
            and other.carbs > 0
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
            recommendation=(
                "Not enough isolated correction boluses to derive a real-world ISF. "
                "Need at least a handful of correction-only boluses without carbs within ±60 min."
            ),
        )

    derived_mgdl = statistics.fmean(samples_mgdl_per_u)
    derived_mmol = mgdl_to_mmol(derived_mgdl)

    if len(samples_mgdl_per_u) < 3:
        confidence = "low"
    elif len(samples_mgdl_per_u) < 8:
        confidence = "medium"
    else:
        confidence = "high"

    # ISF is mg/dL dropped per unit; a correction dose is (BG - target) / ISF.
    # A HIGHER ISF number therefore yields a SMALLER dose. So when the derived
    # (real-world) ISF exceeds the profile's, each unit is dropping BG more than
    # the profile assumes -> the profile ISF is set too low -> corrections are
    # over-dosed -> hypo. The safe correction is to RAISE the profile ISF, not
    # lower it. (See effective_isf_check, which resolves the identical signal
    # the same direction: over-dosing -> larger effective ISF.)
    safety = " This is advisory only. Do not change AAPS/pump settings without consulting your healthcare provider."
    ratio = (derived_mmol / profile_isf_mmol) if profile_isf_mmol else None
    if ratio is None:
        recommendation = "No profile ISF available to compare against."
    elif ratio > 1.15:
        recommendation = (
            "Derived ISF suggests you're MORE sensitive than your profile says "
            "(each unit drops you further), so corrections may be over-dosing "
            "and driving lows. Consider RAISING profile ISF (a higher ISF number "
            "means smaller correction doses)." + safety
        )
    elif ratio < 0.85:
        recommendation = (
            "Derived ISF suggests you're LESS sensitive than your profile says "
            "(each unit drops you less), so corrections may be under-dosing. "
            "Consider LOWERING profile ISF (a lower ISF number means larger "
            "correction doses)." + safety
        )
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


def _ds_created_at(ds: dict) -> datetime | None:
    """Parse a devicestatus row's created_at into UTC. None if absent/invalid."""
    iso = ds.get("created_at")
    if not iso:
        return None
    try:
        return parse_iso_to_utc(iso)
    except Exception:
        return None


def _ds_sens_mmol(ds: dict, profile_units: str) -> float | None:
    """Read the effective ISF from a devicestatus row, return in mmol/L/U.

    Two field conventions seen in real Nightscout data:
      - **AAPS Dynamic ISF**: `openaps.suggested.variable_sens` (mg/dL/U
        regardless of profile units — AAPS internal calculation is always mg/dL).
      - **oref0 / Loop / older AAPS**: `openaps.suggested.sens` (in profile units).

    We try `variable_sens` first (more specific, more recently added) and fall
    back to `sens`. Returns None if neither is present or both are invalid.
    """
    openaps = ds.get("openaps")
    if not isinstance(openaps, dict):
        return None
    suggested = openaps.get("suggested")
    if not isinstance(suggested, dict):
        return None
    # AAPS Dynamic ISF — always mg/dL/U
    var_sens = suggested.get("variable_sens")
    if isinstance(var_sens, (int, float)) and var_sens > 0:
        return mgdl_to_mmol(float(var_sens))
    # oref0 standard — profile units
    sens = suggested.get("sens")
    if isinstance(sens, (int, float)) and sens > 0:
        val = float(sens)
        return mgdl_to_mmol(val) if profile_units == "mg/dL" else val
    return None


def _assign_band(mgdl: int) -> tuple[str, int, int | None]:
    """Return (label, lower, upper) for the band the given BG falls into."""
    for label, lower, upper in ISF_BG_BANDS:
        if mgdl >= lower and (upper is None or mgdl < upper):
            return label, lower, upper
    # Fallback (shouldn't happen with current ISF_BG_BANDS): below 0
    return ISF_BG_BANDS[0]


def effective_isf_check(
    treatments: list[Treatment],
    sgvs: list[Sgv],
    devicestatuses: list[dict],
    profile_units: str = "mmol",
    dia_hours: float = 5.0,
) -> EffectiveIsfDerivation:
    """Compare AAPS's per-correction effective ISF to realized BG drops.

    For each isolated correction bolus (same eligibility filter as
    insulin_sensitivity_check), we look up the most-recent devicestatus row
    strictly BEFORE the bolus and within ±EFFECTIVE_ISF_SENS_MATCH_WINDOW_MIN
    minutes. We read `openaps.suggested.sens` from that row — the effective
    ISF AAPS used at decision time — and compare it to the realized drop in
    the next DIA hours, stratified by pre-bolus BG band.

    Args:
        treatments: All treatments in the lookback window.
        sgvs: All SGVs in the lookback window.
        devicestatuses: Raw devicestatus rows (we read openaps.suggested.sens).
        profile_units: "mg/dL" or "mmol". `sens` is in profile units.
        dia_hours: Duration of insulin action. Clamped to 5h max.

    Returns: EffectiveIsfDerivation with per-band breakdown and recommendation.

    Note: this tool is heavier than insulin_sensitivity_check because
    devicestatus collections carry ~288 rows/day (every ~5 min) of deeply
    nested openaps blobs. For days=30 the dataset is ~8600 rows.
    """
    sgv_sorted = sorted(sgvs, key=lambda r: parse_iso_to_utc(r.date_iso))
    # Sort devicestatuses ascending by created_at so we can do an O(log N)-ish
    # search via bisect... but actually for simplicity and N≈8600, linear scan
    # per correction is fine (typical corrections ≈ 100/14d). We pre-compute
    # the (datetime, ds) list to avoid re-parsing ISO strings repeatedly.
    ds_with_time: list[tuple[datetime, dict]] = sorted(
        ((ts, ds) for ds in devicestatuses if (ts := _ds_created_at(ds)) is not None),
        key=lambda pair: pair[0],
    )

    # Carb timestamps (any nonzero carb meal) for the "no carbs ±60 min" filter.
    carb_times: list[datetime] = []
    for t in treatments:
        if t.carbs and t.carbs > 0:
            with contextlib.suppress(Exception):
                carb_times.append(parse_iso_to_utc(t.created_at))

    # Group eligible samples by band.
    per_band_effective_mmol: dict[str, list[float]] = {label: [] for label, _, _ in ISF_BG_BANDS}
    per_band_realized_mmol: dict[str, list[float]] = {label: [] for label, _, _ in ISF_BG_BANDS}
    samples_without_sens = 0
    total_eligible = 0  # corrections that passed all OTHER filters
    match_window = timedelta(minutes=EFFECTIVE_ISF_SENS_MATCH_WINDOW_MIN)

    for tx in treatments:
        if not tx.insulin or tx.insulin <= 0:
            continue
        if tx.carbs and tx.carbs > 0:
            continue
        if "Correction" not in tx.event_type and tx.event_type != "Bolus":
            continue
        try:
            tx_time = parse_iso_to_utc(tx.created_at)
        except Exception:
            continue
        # No carb entry within ±60 min
        ate_nearby = any(abs((c - tx_time).total_seconds()) <= 3600 for c in carb_times)
        if ate_nearby:
            continue

        # Pre-bolus SGV (closest ≤ tx_time) and post-bolus min within DIA
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
        if drop <= 0 or (drop / tx.insulin) > 500:
            continue
        realized_isf_mgdl = drop / tx.insulin

        total_eligible += 1

        # Find most-recent devicestatus row STRICTLY before tx_time, within window.
        # _ds_sens_mmol handles the AAPS-vs-oref0 field-name difference and
        # returns a normalized mmol/L/U value.
        matched_sens_mmol: float | None = None
        for ts, ds in reversed(ds_with_time):
            if ts >= tx_time:
                continue
            if tx_time - ts > match_window:
                break  # too old; sorted ascending so all earlier also too old
            candidate = _ds_sens_mmol(ds, profile_units)
            if candidate is None:
                continue
            matched_sens_mmol = candidate
            break

        if matched_sens_mmol is None:
            samples_without_sens += 1
            continue

        # Assign to BG band and record
        band_label, _, _ = _assign_band(pre_sgv.sgv_mgdl)
        per_band_effective_mmol[band_label].append(matched_sens_mmol)
        per_band_realized_mmol[band_label].append(mgdl_to_mmol(realized_isf_mgdl))

    # Aggregate per-band
    by_bg_band: list[IsfBandSample] = []
    all_effective: list[float] = []
    all_realized: list[float] = []
    for label, lower, upper in ISF_BG_BANDS:
        effs = per_band_effective_mmol[label]
        reas = per_band_realized_mmol[label]
        n = len(effs)
        all_effective.extend(effs)
        all_realized.extend(reas)
        if n == 0:
            by_bg_band.append(
                IsfBandSample(
                    band_label=label,
                    band_lower_mgdl=lower,
                    band_upper_mgdl=upper,
                    sample_count=0,
                    avg_effective_isf_mmol_per_unit=None,
                    avg_realized_isf_mmol_per_unit=None,
                    ratio_realized_over_effective=None,
                    note="No eligible corrections in this band.",
                )
            )
            continue
        avg_eff = statistics.fmean(effs)
        avg_rea = statistics.fmean(reas)
        ratio = avg_rea / avg_eff if avg_eff else None
        note: str | None = None
        if n < 3:
            note = f"Only {n} sample(s) — too few for a reliable band ratio."
        by_bg_band.append(
            IsfBandSample(
                band_label=label,
                band_lower_mgdl=lower,
                band_upper_mgdl=upper,
                sample_count=n,
                avg_effective_isf_mmol_per_unit=round(avg_eff, 2),
                avg_realized_isf_mmol_per_unit=round(avg_rea, 2),
                ratio_realized_over_effective=round(ratio, 2) if ratio is not None else None,
                note=note,
            )
        )

    sample_count = len(all_effective)

    # Confidence by total samples (matches existing pattern in IsfDerivation).
    if sample_count < 3:
        confidence = "low"
    elif sample_count < 8:
        confidence = "medium"
    else:
        confidence = "high"

    overall_eff = statistics.fmean(all_effective) if all_effective else None
    overall_rea = statistics.fmean(all_realized) if all_realized else None
    overall_ratio = (overall_rea / overall_eff) if overall_eff and overall_rea else None

    # Recommendation
    safety = " These signals are advisory. Do not change AAPS settings without consulting your healthcare provider."

    if sample_count == 0:
        # Distinguish "no devicestatus" from "AAPS rows present but no sens"
        any_sens = any(_ds_sens_mmol(ds, profile_units) is not None for ds in devicestatuses)
        if len(devicestatuses) == 0:
            rec = (
                "No devicestatus rows in the lookback window. This tool requires "
                "AAPS (or another OpenAPS-style loop) publishing devicestatus to "
                "Nightscout. If you're not running an automated loop, use "
                "insulin_sensitivity_check instead." + safety
            )
        elif not any_sens:
            rec = (
                "Devicestatus rows present but none carry openaps.suggested.sens — "
                "Dynamic ISF may be disabled in your AAPS settings, or your uploader "
                "isn't syncing the full openaps blob. If you don't run Dynamic ISF, "
                "use insulin_sensitivity_check instead." + safety
            )
        else:
            rec = (
                f"Found {total_eligible} eligible correction(s) but none could be paired "
                f"with a devicestatus row within ±{EFFECTIVE_ISF_SENS_MATCH_WINDOW_MIN} min. "
                "Your uploader may be syncing devicestatus on a much slower cadence than "
                "expected." + safety
            )
        return EffectiveIsfDerivation(
            sample_count=0,
            devicestatus_rows_examined=len(devicestatuses),
            samples_without_sens=samples_without_sens,
            avg_effective_isf_mmol_per_unit=None,
            avg_realized_isf_mmol_per_unit=None,
            overall_ratio_realized_over_effective=None,
            confidence="low",
            by_bg_band=by_bg_band,
            recommendation=rec,
        )

    # Build per-band ratio map for the divergent-band check
    band_ratios: dict[str, float | None] = {b.band_label: b.ratio_realized_over_effective for b in by_bg_band}
    in_target = band_ratios.get("in_target")
    above_target = band_ratios.get("above_target")
    below_target = band_ratios.get("below_target")

    if overall_ratio is None:
        rec_core = "Insufficient data to compute an overall ratio."
    elif 0.85 <= overall_ratio <= 1.15:
        rec_core = (
            f"Dynamic ISF is well-calibrated against your real outcomes "
            f"(overall ratio {overall_ratio:.2f}, within ±15% of 1.0)."
        )
    elif overall_ratio > 1.15:
        rec_core = (
            f"AAPS appears to over-dose corrections (overall ratio "
            f"{overall_ratio:.2f}: realized BG drop averaged {overall_rea:.1f} mmol/L per U "
            f"but AAPS used effective ISF averaging {overall_eff:.1f}). The AAPS docs say "
            "to LOWER the Adjustment Factor (lower % → larger effective ISF → "
            "less aggressive corrections)."
        )
    else:
        rec_core = (
            f"AAPS appears to under-dose corrections (overall ratio "
            f"{overall_ratio:.2f}: realized BG drop averaged {overall_rea:.1f} mmol/L per U "
            f"but AAPS used effective ISF averaging {overall_eff:.1f}). The AAPS docs say "
            "to RAISE the Adjustment Factor (higher % → smaller effective ISF → "
            "more aggressive corrections)."
        )

    # Detect BG-curve divergence: in-target tracks well but a tails band runs hot/cold.
    curve_note = ""
    if in_target is not None and 0.85 <= in_target <= 1.15 and above_target is not None and above_target > 1.15:
        curve_note = (
            f" However, in-target tracks well ({in_target:.2f}) while above-target runs "
            f"hot ({above_target:.2f}) — this is a BG-curve calibration issue (the "
            "BG-dependent dampening parameter), not an Adjustment Factor issue."
        )
    elif in_target is not None and 0.85 <= in_target <= 1.15 and below_target is not None and below_target > 1.15:
        curve_note = (
            f" However, in-target tracks well ({in_target:.2f}) while below-target runs "
            f"hot ({below_target:.2f}) — note that hypo-treatment carbs may be polluting "
            "the below-target sample; treat this signal with caution."
        )

    return EffectiveIsfDerivation(
        sample_count=sample_count,
        devicestatus_rows_examined=len(devicestatuses),
        samples_without_sens=samples_without_sens,
        avg_effective_isf_mmol_per_unit=round(overall_eff, 2) if overall_eff else None,
        avg_realized_isf_mmol_per_unit=round(overall_rea, 2) if overall_rea else None,
        overall_ratio_realized_over_effective=round(overall_ratio, 2) if overall_ratio else None,
        confidence=confidence,
        by_bg_band=by_bg_band,
        recommendation=rec_core + curve_note + safety,
    )


def _pair_carbs_with_insulin(
    treatments: list[Treatment], window_minutes: int = 10
) -> list[tuple[datetime, float, float]]:
    """Pair carb treatments with nearby insulin treatments.

    AAPS and similar uploaders write carbs and insulin as separate rows.
    For each carb-bearing row (>5g), look for any insulin row (>0U) within
    ±`window_minutes`. If a single carb row pairs with multiple insulin rows,
    we sum the insulin (handles bolus + extended bolus combos).

    Returns: list of (carb_time, carbs_g, total_insulin_u) tuples.
    """
    pairs: list[tuple[datetime, float, float]] = []
    window = timedelta(minutes=window_minutes)
    for carb_tx in treatments:
        if not carb_tx.carbs or carb_tx.carbs <= 5:
            continue
        try:
            carb_time = parse_iso_to_utc(carb_tx.created_at)
        except Exception:
            continue
        total_insulin = 0.0
        # If the carb row itself also has insulin (tight-pump style), use it.
        if carb_tx.insulin and carb_tx.insulin > 0:
            total_insulin += carb_tx.insulin
        # Plus any insulin-bearing rows within ±window minutes
        for other in treatments:
            if other.created_at == carb_tx.created_at:
                continue
            if not other.insulin or other.insulin <= 0:
                continue
            try:
                other_time = parse_iso_to_utc(other.created_at)
            except Exception:
                continue
            if abs((other_time - carb_time).total_seconds()) <= window.total_seconds():
                total_insulin += other.insulin
        if total_insulin > 0:
            pairs.append((carb_time, carb_tx.carbs, total_insulin))
    return pairs


def carb_ratio_check(
    treatments: list[Treatment],
    sgvs: list[Sgv],
    profile_cr_g_per_unit: float | None,
) -> CrDerivation:
    """Derive a real-world carb-ratio signal from meal-bolus outcomes.

    Two findings combined:
      - The CR the user *actually applied* (carbs ÷ insulin), averaged across
        eligible meals. Compared to the profile CR.
      - The post-meal residual: avg of (BG 4h after meal − BG at meal).
        Negative = meals end below where they started (over-bolused).
        Positive = meals end higher (under-bolused).

    A meal is eligible when: carbs > 5 g AND insulin > 0 AND there's a
    pre-meal CGM reading AND a CGM reading ~4 h later. Meals followed by
    another meal within 4 h are excluded (contamination).
    """
    sgv_sorted = sorted(sgvs, key=lambda r: parse_iso_to_utc(r.date_iso))
    paired = _pair_carbs_with_insulin(treatments)
    ratios: list[float] = []
    residuals: list[float] = []

    # Pre-collect carb-bearing meal timestamps for forward-contamination check
    other_carb_times = [parse_iso_to_utc(t.created_at) for t in treatments if t.carbs and t.carbs > 5]

    for tx_time, carbs_g, insulin_u in paired:
        end_window = tx_time + timedelta(hours=4)
        # Forward-only contamination: exclude if another carb meal lands during
        # the residual-measurement window. Prior meals are mostly absorbed by
        # tx_time; we don't penalize for them. Symmetric exclusion was
        # empirically too strict for typical 3-4 meals/day users.
        contaminated = any(
            tx_time < other_time <= end_window for other_time in other_carb_times if other_time != tx_time
        )
        if contaminated:
            continue

        pre_sgv: Sgv | None = None
        end_sgv: Sgv | None = None
        for s in sgv_sorted:
            ts = parse_iso_to_utc(s.date_iso)
            if ts <= tx_time:
                pre_sgv = s
            elif ts <= end_window:
                end_sgv = s  # last one in window

        if not pre_sgv or not end_sgv:
            continue

        ratios.append(carbs_g / insulin_u)
        residuals.append(end_sgv.sgv_mgdl - pre_sgv.sgv_mgdl)

    if not ratios:
        return CrDerivation(
            sample_count=0,
            derived_cr_g_per_unit=None,
            profile_cr_g_per_unit=profile_cr_g_per_unit,
            ratio_derived_over_profile=None,
            avg_end_minus_pre_mgdl=None,
            confidence="low",
            recommendation=(
                "Not enough eligible meal boluses. A meal needs carbs >5g, insulin >0, "
                "pre-meal CGM reading, and a CGM reading ~4h later with no other meal "
                "in between."
            ),
        )

    derived_cr = statistics.fmean(ratios)
    avg_residual = statistics.fmean(residuals)
    ratio = (derived_cr / profile_cr_g_per_unit) if profile_cr_g_per_unit else None

    if len(ratios) < 3:
        confidence = "low"
    elif len(ratios) < 8:
        confidence = "medium"
    else:
        confidence = "high"

    # Compose recommendation from BOTH signals. When they agree the direction
    # is clear; when they disagree, surface that the user's BG is being
    # affected by something other than the meal bolus (basal, loop corrections,
    # exercise, missed carb logging) and decline to recommend a CR change.
    parts: list[str] = []
    parts.append(
        f"Average applied CR: {derived_cr:.0f} g/U"
        + (f" (profile: {profile_cr_g_per_unit:.0f} g/U)" if profile_cr_g_per_unit else "")
        + "."
    )
    parts.append(
        f"Average post-meal residual: {avg_residual:+.0f} mg/dL "
        f"({'meals tend to end higher' if avg_residual > 0 else 'meals tend to end lower'} than they started)."
    )

    # Direction-of-CR-adjustment logic
    cr_signal = 0  # -1 = CR too low, +1 = CR too high, 0 = neutral
    res_signal = 0
    if ratio is not None:
        if ratio > 1.10:
            cr_signal = +1  # applying MORE g/U than profile → fewer units per gram than profile thinks
        elif ratio < 0.90:
            cr_signal = -1
    if avg_residual >= 20:
        res_signal = +1  # ends high → CR too high (under-bolused)
    elif avg_residual <= -20:
        res_signal = -1  # ends low → CR too low (over-bolused)

    if ratio is None:
        parts.append("No profile CR available to compare against.")
    elif cr_signal == 0 and res_signal == 0:
        parts.append("Both signals are stable — CR looks well-tuned.")
    elif cr_signal == res_signal and cr_signal == +1:
        parts.append("Both signals suggest CR is too high (too few units per gram). Consider lowering CR." + _CR_SAFETY)
    elif cr_signal == res_signal and cr_signal == -1:
        parts.append("Both signals suggest CR is too low (too many units per gram). Consider raising CR." + _CR_SAFETY)
    elif cr_signal != 0 and res_signal != 0 and cr_signal != res_signal:
        parts.append(
            "Signals disagree: applied CR and post-meal residual point in opposite directions. "
            "BG is likely being moved by factors outside the meal bolus — basal rate, AAPS/Loop "
            "auto-corrections, exercise, or unlogged carbs. CR adjustment isn't indicated from "
            "this data alone."
        )
    else:
        # One signal is neutral, the other points somewhere
        active = "applied CR" if cr_signal else "post-meal residual"
        direction = "high" if (cr_signal or res_signal) > 0 else "low"
        parts.append(
            f"Only the {active} signal is notable (suggests CR may be too {direction}). "
            "Insufficient agreement to recommend a change."
        )

    return CrDerivation(
        sample_count=len(ratios),
        derived_cr_g_per_unit=round(derived_cr, 1),
        profile_cr_g_per_unit=profile_cr_g_per_unit,
        ratio_derived_over_profile=round(ratio, 2) if ratio is not None else None,
        avg_end_minus_pre_mgdl=round(avg_residual, 1),
        confidence=confidence,
        recommendation=" ".join(parts),
    )


# Consensus hypoglycemia event definition (Battelino Diabetes Care 2019;42:1593
# / ATTD): onset = BG <70 mg/dL for >=15 min; offset = >=15 min at >=70.
HYPO_THRESHOLD_MGDL = 70
HYPO_LEVEL2_MGDL = 54
HYPO_MIN_DURATION_MIN = 15
HYPO_RECOVERY_MIN = 15  # >=15 min >=70 ends an event; shorter blips don't split it
HYPO_RESCUE_GRACE_MIN = 15  # carb logged within this of the event = rescue


def hypo_episodes(
    days: int,
    sgvs: list[Sgv],
    treatments: list[Treatment] | None = None,
    tz_offset_hours: float = 0.0,
) -> HypoEpisodeReport:
    """Detect consensus hypoglycemic events from CGM.

    An event is a run of BG <70 mg/dL lasting >=15 min; a rise to >=70 for
    <15 min does not end it (blips are merged). Level 2 (<54) is set by the
    event nadir. `nocturnal` is judged in local time via `tz_offset_hours`;
    `rescue_carbs` flags a carb treatment logged during the event.
    """
    pairs = sorted(
        ((parse_iso_to_utc(s.date_iso), s.sgv_mgdl) for s in sgvs if s.sgv_mgdl > 0),
        key=lambda p: p[0],
    )
    reading_count = len(pairs)
    expected = max(1, days) * 288  # 5-min cadence
    pct_active = round(min(100.0, reading_count / expected * 100), 1)

    # Raw contiguous below-threshold segments (consecutive readings all <70).
    raw: list[list[tuple[datetime, int]]] = []
    current: list[tuple[datetime, int]] = []
    for ts, v in pairs:
        if v < HYPO_THRESHOLD_MGDL:
            current.append((ts, v))
        elif current:
            raw.append(current)
            current = []
    if current:
        raw.append(current)

    # Merge segments separated by <15 min above 70 (brief excursion mid-event).
    merged: list[list[tuple[datetime, int]]] = []
    for seg in raw:
        if merged:
            gap_min = (seg[0][0] - merged[-1][-1][0]).total_seconds() / 60
            if gap_min < HYPO_RECOVERY_MIN:
                merged[-1].extend(seg)
                continue
        merged.append(list(seg))

    carb_txs = [parse_iso_to_utc(t.created_at) for t in (treatments or []) if t.carbs and t.carbs > 0]

    episodes: list[HypoEpisode] = []
    for seg in merged:
        start_ts, end_ts = seg[0][0], seg[-1][0]
        duration = int((end_ts - start_ts).total_seconds() / 60)
        if duration < HYPO_MIN_DURATION_MIN:
            continue  # too brief to be a consensus event
        nadir = min(v for _, v in seg)
        nadir_ts = next(ts for ts, v in seg if v == nadir)
        local_hour, _ = _local_hour_minute(nadir_ts, tz_offset_hours)
        grace = timedelta(minutes=HYPO_RESCUE_GRACE_MIN)
        rescue = any(start_ts - grace <= c <= end_ts + grace for c in carb_txs)
        episodes.append(
            HypoEpisode(
                start_iso=seg[0][0].strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                end_iso=end_ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                duration_minutes=duration,
                nadir_mgdl=nadir,
                nadir_mmol=mgdl_to_mmol(nadir),
                level=2 if nadir < HYPO_LEVEL2_MGDL else 1,
                nocturnal=local_hour < 6,
                rescue_carbs=rescue,
            )
        )

    level1 = sum(1 for e in episodes if e.level == 1)
    level2 = sum(1 for e in episodes if e.level == 2)
    nocturnal = sum(1 for e in episodes if e.nocturnal)
    with_rescue = sum(1 for e in episodes if e.rescue_carbs)
    total_below = sum(e.duration_minutes for e in episodes)
    mean_dur = round(statistics.fmean([e.duration_minutes for e in episodes]), 1) if episodes else 0.0
    per_week = round(len(episodes) / max(1, days) * 7, 1)

    if not episodes:
        summary = f"No consensus hypoglycemic events (>=15 min <70 mg/dL) in {days} days."
    else:
        summary = (
            f"{len(episodes)} hypo events in {days} days ({per_week}/week): "
            f"{level2} level-2 (<54), {level1} level-1 (54-69), {nocturnal} nocturnal. "
            f"Mean duration {mean_dur} min."
        )
        if pct_active < 70:
            summary += f" NOTE: only {pct_active}% CGM-active — event counts likely undercount."

    return HypoEpisodeReport(
        days_analyzed=days,
        reading_count=reading_count,
        pct_cgm_active=pct_active,
        total_episodes=len(episodes),
        level1_episodes=level1,
        level2_episodes=level2,
        nocturnal_episodes=nocturnal,
        episodes_with_rescue_carbs=with_rescue,
        episodes_per_week=per_week,
        mean_duration_minutes=mean_dur,
        total_time_below_70_minutes=total_below,
        episodes=episodes,
        summary=summary,
    )


def compression_low_analysis(
    days: int,
    sgvs: list[Sgv],
    treatments: list[Treatment] | None = None,
) -> CompressionAnalysis:
    """Flag CGM dips that look like sensor-compression artifacts.

    Heuristic: a fast drop (≥30 mg/dL in ≤15 min) to below 70 mg/dL
    followed by an equally fast recovery (≥30 mg/dL in ≤15 min) within
    a 30-min total episode duration. Real hypos don't recover that fast
    *without intervention*.

    When `treatments` is provided, we suppress candidates where a carb
    treatment (>10g) landed within ±15 min of the minimum — that explains
    the recovery and the dip was likely a real low the user treated.
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
            int((parse_iso_to_utc(min_r.date_iso) - parse_iso_to_utc(start.date_iso)).total_seconds() // 60),
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
            int((parse_iso_to_utc(recovery_r.date_iso) - parse_iso_to_utc(min_r.date_iso)).total_seconds() // 60),
        )
        recovery_rate = rise / recovery_minutes
        total_duration = drop_minutes + recovery_minutes
        if (
            rise >= COMPRESSION_MIN_DROP_MGDL
            and recovery_rate >= COMPRESSION_RECOVERY_MGDL_PER_MIN
            and total_duration <= COMPRESSION_MAX_DURATION_MIN
        ):
            # If we have treatments to cross-check, suppress this candidate
            # when a carb treatment >10g landed within ±15 min of the minimum.
            # That explains the recovery as a real treated low, not compression.
            if treatments:
                min_dt = parse_iso_to_utc(min_r.date_iso)
                treated = any(
                    t.carbs
                    and t.carbs > 10
                    and abs((parse_iso_to_utc(t.created_at) - min_dt).total_seconds()) <= 15 * 60
                    for t in treatments
                    if t.created_at
                )
                if treated:
                    i += 1
                    continue
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
