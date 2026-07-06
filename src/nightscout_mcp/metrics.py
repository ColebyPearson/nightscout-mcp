"""Glycemic metrics — pure functions, no HTTP, no MCP coupling.

This module implements canonical CGM analysis metrics published in the
clinical research literature, used by endocrinologists and AID researchers:

- GRI (Glycemia Risk Index)            Klonoff JDST 2023;17:1226
- LBGI / HBGI / ADRR                    Kovatchev Diabetes Care 1998;21:1870, 2006;29:2433
- MAGE (Mean Amplitude of Glycemic Excursions)  Service Diabetes 1970;19:644
- MODD (Mean of Daily Differences)      Molnar Mayo Clin Proc 1972
- J-index                               Wojcicki Horm Metab Res 1995
- M-value                               Schlichtkrull Acta Med Scand 1965
- GVP (Glucose Variability Percentage)  Peyser DTT 2018
- CONGA-n (Continuous Overall Net Glycemic Action)  McDonnell DTT 2005
- COGI (Composite Outcome of Glycemic Improvement)  Leelarathna DTT 2020
- TIR with binomial Wilson 95% CI       Battelino Diabetes Care 2019
- AGP-style percentile bands by hour    Battelino 2019, AGP consensus
- Change-point detection (CUSUM)        Page Biometrika 1954
- IOB exponential curve fit             Walsh / oref0 / AAPS Oref exponential

All functions take parsed Sgv/Treatment lists or raw mg/dL float lists and
return plain dicts or floats. Model wrapping happens in tools/metrics.py.

Design notes:
- Units: input always mg/dL (Nightscout-native). Conversion to mmol/L happens
  in the Pydantic models, not here.
- Empty input: every function tolerates [] and returns 0/None rather than
  raising — the LLM is better served by zero values + low sample_count than
  by exceptions.
- Pure stdlib: no numpy/scipy/ruptures dependency. Algorithms are textbook
  implementations, deliberately easy to audit against the cited papers.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from .models import Sgv

# --- Constants from the literature ------------------------------------------

# Kovatchev 1998 BG-symmetrization function constants
KOVATCHEV_ALPHA = 1.084
KOVATCHEV_BETA = 5.381
KOVATCHEV_GAMMA = 1.509

# LBGI risk-band thresholds (Kovatchev 1998 + later refinement)
LBGI_BAND_LOW_MAX = 1.1
LBGI_BAND_MODERATE_MAX = 2.5
LBGI_BAND_HIGH_MAX = 5.0  # >5 = "very high"

# HBGI risk-band thresholds (Kovatchev)
HBGI_BAND_LOW_MAX = 4.5
HBGI_BAND_MODERATE_MAX = 9.0
HBGI_BAND_HIGH_MAX = 15.0

# CV consensus target (Battelino 2019)
CV_TARGET_PERCENT = 36.0

# MAGE excursion threshold (multiple of SD)
MAGE_DEFAULT_THRESHOLD_SD = 1.0


# --- Helpers ----------------------------------------------------------------


def _sgv_to_mgdl_list(readings: Iterable[Sgv]) -> list[float]:
    """Extract sgv_mgdl from an iterable of Sgv records, filtering non-sgv."""
    return [float(r.sgv_mgdl) for r in readings if r.type == "sgv" and r.sgv_mgdl > 0]


def _sgv_to_pairs(readings: Iterable[Sgv]) -> list[tuple[datetime, float]]:
    """Extract (timestamp, mg/dL) pairs, sorted ascending by timestamp."""
    pairs: list[tuple[datetime, float]] = []
    for r in readings:
        if r.type != "sgv" or not r.sgv_mgdl:
            continue
        try:
            ts = datetime.fromisoformat(r.date_iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        pairs.append((ts, float(r.sgv_mgdl)))
    pairs.sort(key=lambda p: p[0])
    return pairs


CGM_READINGS_PER_DAY = 288  # 5-min cadence
AGP_MIN_DAYS = 14  # Battelino 2019 consensus minimum window
AGP_MIN_ACTIVE_PCT = 70.0  # Battelino 2019 consensus minimum sensor-active %


def data_sufficiency(readings: Iterable[Sgv], days: int) -> dict[str, float | int | bool | str | None]:
    """Assess whether a CGM window is adequate for consensus interpretation.

    Battelino 2019 requires >=14 days at >=70% sensor-active time before AGP /
    TIR-consensus metrics are interpretable. This returns the numbers a
    clinician needs to judge that — and a `meets_agp_consensus` flag — so a
    report built on 3 days of a warming-up sensor is flagged rather than
    trusted. `pct_active` is capped at 100 (1-min uploaders exceed 288/day).
    Gaps are non-random (warmup follows failure follows compression lows), so
    the longest gap is surfaced explicitly.
    """
    pairs = _sgv_to_pairs(readings)
    count = len(pairs)
    expected = max(1, days) * CGM_READINGS_PER_DAY
    pct_active = round(min(100.0, count / expected * 100), 1)

    longest_gap_h = 0.0
    for i in range(1, len(pairs)):
        gap_h = (pairs[i][0] - pairs[i - 1][0]).total_seconds() / 3600
        if gap_h > longest_gap_h:
            longest_gap_h = gap_h

    days_with_data = len({ts.strftime("%Y-%m-%d") for ts, _ in pairs})
    meets = days >= AGP_MIN_DAYS and pct_active >= AGP_MIN_ACTIVE_PCT

    note: str | None = None
    if not meets:
        reasons = []
        if days < AGP_MIN_DAYS:
            reasons.append(f"window is {days}d (<{AGP_MIN_DAYS}d)")
        if pct_active < AGP_MIN_ACTIVE_PCT:
            reasons.append(f"only {pct_active}% CGM-active (<{AGP_MIN_ACTIVE_PCT:.0f}%)")
        note = (
            "Below Battelino 2019 sufficiency for AGP/consensus metrics: "
            + " and ".join(reasons)
            + ". Treat percentiles and pass/fail verdicts as indicative only."
        )

    return {
        "days_requested": days,
        "days_with_data": days_with_data,
        "reading_count": count,
        "expected_readings": expected,
        "pct_active": pct_active,
        "longest_gap_hours": round(longest_gap_h, 1),
        "meets_agp_consensus": meets,
        "note": note,
    }


def wilson_ci_95(successes: int, total: int) -> tuple[float, float]:
    """Wilson score interval for binomial proportion at 95% confidence.

    Better small-sample behaviour than the normal approximation; returns (0, 0)
    for total=0 instead of dividing by zero.

    Source: Wilson Journal of the American Statistical Association 1927.
    """
    if total <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = successes / total
    n = total
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n)))) / denom
    lower = max(0.0, center - half)
    upper = min(1.0, center + half)
    return (round(lower * 100, 2), round(upper * 100, 2))


# --- Core glycemic metrics --------------------------------------------------


def gmi_percent(mean_mgdl: float) -> float:
    """Glucose Management Indicator. Bergenstal Diabetes Care 2018;41:2275.

    GMI(%) = 3.31 + 0.02392 × mean_mgdl
    """
    return round(3.31 + 0.02392 * mean_mgdl, 2)


def cv_percent(values: Sequence[float]) -> float:
    """Coefficient of Variation as percent. Consensus target <36%."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    sd = statistics.stdev(values)
    return round(sd / mean * 100, 2)


def j_index(values: Sequence[float]) -> float:
    """J-index = 0.001 × (mean + SD)². mg/dL units. Wojcicki HMR 1995."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    return round(0.001 * (mean + sd) ** 2, 3)


def m_value(values: Sequence[float], target_mgdl: float = 120.0) -> float:
    """M-value. Schlichtkrull Acta Med Scand 1965.

    Mean of |10·log10(BG/target)|³. Lower = better. Target=120 mg/dL classic.
    """
    if not values:
        return 0.0
    out = []
    for v in values:
        if v <= 0:
            continue
        out.append(abs(10 * math.log10(v / target_mgdl)) ** 3)
    if not out:
        return 0.0
    return round(statistics.fmean(out), 2)


# --- Kovatchev risk indices -------------------------------------------------


def _kovatchev_f(bg_mgdl: float) -> float:
    """Symmetrization function from Kovatchev 1998.

    f(BG) = 1.509 × ((ln BG)^1.084 − 5.381)

    Maps mg/dL to a symmetric scale around 112.5 mg/dL where f(112.5)=0.
    """
    if bg_mgdl <= 0:
        return 0.0
    return KOVATCHEV_GAMMA * ((math.log(bg_mgdl) ** KOVATCHEV_ALPHA) - KOVATCHEV_BETA)


def lbgi_hbgi(values: Sequence[float]) -> tuple[float, float]:
    """Low / High Blood Glucose Index. Kovatchev Diabetes Care 1998;21:1870.

    Returns (lbgi, hbgi). Each is mean of the risk weighting (10*f²) for the
    relevant sign of f.
    """
    if not values:
        return (0.0, 0.0)
    lows: list[float] = []
    highs: list[float] = []
    for v in values:
        if v <= 0:
            continue
        f = _kovatchev_f(v)
        risk = 10 * f * f
        if f < 0:
            lows.append(risk)
            highs.append(0.0)
        elif f > 0:
            lows.append(0.0)
            highs.append(risk)
        else:
            lows.append(0.0)
            highs.append(0.0)
    lbgi = round(statistics.fmean(lows), 3) if lows else 0.0
    hbgi = round(statistics.fmean(highs), 3) if highs else 0.0
    return (lbgi, hbgi)


def lbgi_band(lbgi: float) -> str:
    """Map LBGI value to clinical risk band."""
    if lbgi < LBGI_BAND_LOW_MAX:
        return "low"
    if lbgi < LBGI_BAND_MODERATE_MAX:
        return "moderate"
    if lbgi < LBGI_BAND_HIGH_MAX:
        return "high"
    return "very_high"


def hbgi_band(hbgi: float) -> str:
    if hbgi < HBGI_BAND_LOW_MAX:
        return "low"
    if hbgi < HBGI_BAND_MODERATE_MAX:
        return "moderate"
    if hbgi < HBGI_BAND_HIGH_MAX:
        return "high"
    return "very_high"


def adrr(values_by_day: Sequence[Sequence[float]]) -> float:
    """Average Daily Risk Range. Kovatchev Diabetes Care 2006;29:2433.

    For each day: max(LR daily) + max(HR daily) where LR/HR are the
    Kovatchev-weighted low/high risks per reading. ADRR = mean across days.

    Requires the data partitioned into per-day lists. Empty days are skipped.
    """
    daily_ranges: list[float] = []
    for day_values in values_by_day:
        if not day_values:
            continue
        max_lr = 0.0
        max_hr = 0.0
        for v in day_values:
            if v <= 0:
                continue
            f = _kovatchev_f(v)
            risk = 10 * f * f
            if f < 0:
                max_lr = max(max_lr, risk)
            elif f > 0:
                max_hr = max(max_hr, risk)
        daily_ranges.append(max_lr + max_hr)
    if not daily_ranges:
        return 0.0
    return round(statistics.fmean(daily_ranges), 3)


# --- GRI (Klonoff 2023) -----------------------------------------------------


def gri(values: Sequence[float]) -> dict[str, float]:
    """Glycemia Risk Index. Klonoff JDST 2023;17:1226.

    GRI = (3.0 × %VeryLow<54) + (2.4 × %Low54-69) + (1.6 × %VeryHigh>250) + (0.8 × %High181-250)

    Returns dict with total + hypo component + hyper component + each percentage.
    Component-wise: GRI_Hypo = 3.0·VLow + 2.4·Low; GRI_Hyper = 1.6·VHigh + 0.8·High.
    """
    n = len(values)
    if n == 0:
        return {
            "gri": 0.0,
            "gri_hypo": 0.0,
            "gri_hyper": 0.0,
            "pct_very_low_lt54": 0.0,
            "pct_low_54_69": 0.0,
            "pct_high_181_250": 0.0,
            "pct_very_high_gt250": 0.0,
            "pct_in_target_70_180": 0.0,
        }
    very_low = sum(1 for v in values if v < 54)
    low = sum(1 for v in values if 54 <= v < 70)
    in_target = sum(1 for v in values if 70 <= v <= 180)
    high = sum(1 for v in values if 180 < v <= 250)
    very_high = sum(1 for v in values if v > 250)

    pct_very_low = very_low / n * 100
    pct_low = low / n * 100
    pct_high = high / n * 100
    pct_very_high = very_high / n * 100
    pct_target = in_target / n * 100

    gri_hypo = 3.0 * pct_very_low + 2.4 * pct_low
    gri_hyper = 1.6 * pct_very_high + 0.8 * pct_high
    gri_total = gri_hypo + gri_hyper
    # Klonoff GRI is clamped to [0, 100]
    gri_total = max(0.0, min(100.0, gri_total))

    return {
        "gri": round(gri_total, 2),
        "gri_hypo": round(gri_hypo, 2),
        "gri_hyper": round(gri_hyper, 2),
        "pct_very_low_lt54": round(pct_very_low, 2),
        "pct_low_54_69": round(pct_low, 2),
        "pct_in_target_70_180": round(pct_target, 2),
        "pct_high_181_250": round(pct_high, 2),
        "pct_very_high_gt250": round(pct_very_high, 2),
    }


# --- Variability metrics ----------------------------------------------------


def mage(values: Sequence[float], threshold_sd: float = MAGE_DEFAULT_THRESHOLD_SD) -> float:
    """Mean Amplitude of Glycemic Excursions. Service 1970, Fernandes 2022 iglu impl.

    Algorithm: identify all peaks and valleys; an excursion is a peak-to-valley
    or valley-to-peak amplitude. Keep excursions where amplitude exceeds
    threshold_sd × SD. Average the absolute amplitudes.

    This is the "simple" MAGE variant (Service 1970). The iglu R package
    additionally uses moving-average crossings; this simpler form is the
    classical and most-cited variant.
    """
    if len(values) < 3:
        return 0.0
    sd = statistics.stdev(values)
    if sd == 0:
        return 0.0
    threshold = threshold_sd * sd

    # Find turning points (alternating peaks and valleys)
    turning_points: list[tuple[int, float]] = []
    for i in range(1, len(values) - 1):
        prev_v, this_v, next_v = values[i - 1], values[i], values[i + 1]
        if (this_v > prev_v and this_v >= next_v) or (this_v < prev_v and this_v <= next_v):
            turning_points.append((i, this_v))
    if len(turning_points) < 2:
        return 0.0

    # Pairwise excursions
    amplitudes: list[float] = []
    for k in range(1, len(turning_points)):
        amp = abs(turning_points[k][1] - turning_points[k - 1][1])
        if amp > threshold:
            amplitudes.append(amp)
    if not amplitudes:
        return 0.0
    return round(statistics.fmean(amplitudes), 2)


def modd(pairs: Sequence[tuple[datetime, float]], window_min: int = 5) -> float:
    """Mean of Daily Differences. Molnar 1972.

    For each reading, find the closest reading from approximately 24h earlier
    (within ±window_min of exactly 24h). Compute |BG(t) - BG(t-24h)|. Mean.

    Returns 0.0 if fewer than ~10 valid pairs found.
    """
    if len(pairs) < 50:
        return 0.0
    # Build index: lookup by approximate 24h-earlier timestamp
    by_minute: dict[int, float] = {}
    for ts, v in pairs:
        key = int(ts.timestamp() // 60)
        by_minute[key] = v

    diffs: list[float] = []
    for ts, v in pairs:
        target_key = int((ts - timedelta(hours=24)).timestamp() // 60)
        # Search ±window_min around target
        match = None
        for offset in range(-window_min, window_min + 1):
            if (target_key + offset) in by_minute:
                match = by_minute[target_key + offset]
                break
        if match is not None:
            diffs.append(abs(v - match))
    if len(diffs) < 10:
        return 0.0
    return round(statistics.fmean(diffs), 2)


def conga(pairs: Sequence[tuple[datetime, float]], n_hours: int = 1) -> float:
    """Continuous Overall Net Glycemic Action over n hours. McDonnell DTT 2005.

    For each reading t, compute diff = BG(t) - BG(t - n_hours). CONGA-n =
    SD of those diffs.
    """
    if len(pairs) < 20:
        return 0.0
    by_minute: dict[int, float] = {int(ts.timestamp() // 60): v for ts, v in pairs}
    diffs: list[float] = []
    for ts, v in pairs:
        target_key = int((ts - timedelta(hours=n_hours)).timestamp() // 60)
        match = None
        # Tolerance window ±5 min for missing exact matches
        for offset in range(-5, 6):
            if (target_key + offset) in by_minute:
                match = by_minute[target_key + offset]
                break
        if match is not None:
            diffs.append(v - match)
    if len(diffs) < 10:
        return 0.0
    return round(statistics.stdev(diffs), 2)


def gvp(pairs: Sequence[tuple[datetime, float]]) -> float:
    """Glucose Variability Percentage. Peyser DTT 2018.

    GVP = (arc_length / time_length) - 1, as a percentage of "flat trace".

    Arc length sums sqrt(Δt² + ΔBG²) across consecutive samples.
    """
    if len(pairs) < 2:
        return 0.0
    arc_length = 0.0
    time_length = 0.0
    for i in range(1, len(pairs)):
        dt_min = (pairs[i][0] - pairs[i - 1][0]).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        d_bg = pairs[i][1] - pairs[i - 1][1]
        arc_length += math.sqrt(dt_min * dt_min + d_bg * d_bg)
        time_length += dt_min
    if time_length <= 0:
        return 0.0
    return round((arc_length / time_length - 1) * 100, 2)


def cogi(tir_70_180_pct: float, tbr_lt70_pct: float, cv_pct: float) -> float:
    """Composite Outcome of Glycemic Improvement. Leelarathna DTT 2020.

    COGI = 0.50 × TIR_subscore + 0.35 × TBR_subscore + 0.15 × CV_subscore

    Sub-scores:
      TIR: linear 0 (TIR=0%) to 100 (TIR=100%)
      TBR<70: 100 if 0%; 0 if ≥15%; linear in between
      CV: 100 if ≤36%; 0 if ≥108% (3× target); linear
    """
    tir_sub = max(0.0, min(100.0, tir_70_180_pct))
    if tbr_lt70_pct <= 0:
        tbr_sub = 100.0
    elif tbr_lt70_pct >= 15:
        tbr_sub = 0.0
    else:
        tbr_sub = (1.0 - tbr_lt70_pct / 15.0) * 100.0
    if cv_pct <= 36:
        cv_sub = 100.0
    elif cv_pct >= 108:
        cv_sub = 0.0
    else:
        cv_sub = (1.0 - (cv_pct - 36) / 72.0) * 100.0
    score = 0.50 * tir_sub + 0.35 * tbr_sub + 0.15 * cv_sub
    return round(score, 2)


# --- Time-stratified analytics ----------------------------------------------


# Meal windows: hour ranges (local time), upper exclusive
DEFAULT_MEAL_PERIODS: dict[str, tuple[int, int]] = {
    "overnight": (0, 6),
    "breakfast": (6, 11),
    "lunch": (11, 14),
    "afternoon": (14, 17),
    "dinner": (17, 21),
    "evening": (21, 24),
}


def partition_by_local_hour(
    pairs: Sequence[tuple[datetime, float]],
    windows: dict[str, tuple[int, int]] | None = None,
    tz_offset_hours: float = 0.0,
) -> dict[str, list[float]]:
    """Split BG readings by hour-of-day window.

    `windows` maps a window name to (hour_start_inclusive, hour_end_exclusive).
    Windows can wrap (e.g. (22, 6) for overnight) — handled by checking both
    halves.

    `tz_offset_hours` adjusts each timestamp's hour by this amount before
    bucketing — used to do "local-time" partitioning when the input pairs are
    in UTC.
    """
    if windows is None:
        windows = DEFAULT_MEAL_PERIODS
    out: dict[str, list[float]] = {k: [] for k in windows}
    for ts, v in pairs:
        adjusted_hour = (ts.hour + tz_offset_hours) % 24
        for name, (start, end) in windows.items():
            if start < end:
                if start <= adjusted_hour < end:
                    out[name].append(v)
            else:
                # wrap-around (e.g. 22-6)
                if adjusted_hour >= start or adjusted_hour < end:
                    out[name].append(v)
    return out


def percentile(values: Sequence[float], q: float) -> float:
    """Compute the q-th percentile (q in [0, 100]) via linear interpolation.

    Uses statistics.quantiles with method='exclusive' for compatibility with
    most CGM literature (which uses standard percentile, not midpoint).
    """
    if not values:
        return 0.0
    if q <= 0:
        return float(min(values))
    if q >= 100:
        return float(max(values))
    sv = sorted(values)
    k = (q / 100) * (len(sv) - 1)
    f = int(k)
    c = min(f + 1, len(sv) - 1)
    if f == c:
        return float(sv[f])
    d0 = sv[f] * (c - k)
    d1 = sv[c] * (k - f)
    return float(d0 + d1)


def agp_hourly_percentiles(
    pairs: Sequence[tuple[datetime, float]],
    percentiles: Sequence[int] = (5, 25, 50, 75, 95),
    tz_offset_hours: float = 0.0,
) -> list[dict[str, float]]:
    """Compute AGP-style percentile bands per hour-of-day.

    Returns 24 entries (hour 0..23), each with a percentile_p:value dict.
    """
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    for ts, v in pairs:
        adjusted_hour = int((ts.hour + tz_offset_hours) % 24)
        by_hour[adjusted_hour].append(v)
    out: list[dict[str, float]] = []
    for h in range(24):
        vals = by_hour[h]
        entry: dict[str, float] = {"hour": float(h), "sample_count": float(len(vals))}
        for p in percentiles:
            entry[f"p{p:02d}"] = round(percentile(vals, p), 1) if vals else 0.0
        out.append(entry)
    return out


# --- Change-point detection (CUSUM) -----------------------------------------


def hourly_aggregate(
    pairs: Sequence[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Aggregate BG readings into hourly means.

    Returns sorted list of (hour_start_utc, mean_mgdl).
    """
    by_hour: dict[datetime, list[float]] = {}
    for ts, v in pairs:
        hour_key = ts.replace(minute=0, second=0, microsecond=0)
        by_hour.setdefault(hour_key, []).append(v)
    return [(k, statistics.fmean(vs)) for k, vs in sorted(by_hour.items())]


def cusum_change_points(
    values: Sequence[float],
    threshold_sigma: float = 4.0,
) -> list[dict[str, float]]:
    """Windowed mean-shift change-point detection.

    For each candidate index i, computes mean of values in [i-W, i] and
    [i, i+W], where W is a window size derived from sample count. If the
    difference exceeds threshold_sigma × SD(values), index i is a candidate
    change point. Local maxima of the difference signal are returned.

    Simpler and more robust than rolling-CUSUM for step-change detection on
    short series (n < 1000 hourly readings). Not a replacement for `ruptures`
    PELT on larger problems, but adequate for the 30-90 day analysis windows
    here.

    Returns list of dicts: {"index": i, "magnitude": diff,
                            "cumsum": diff_magnitude, "direction": 1|-1}.
    """
    n = len(values)
    if n < 30:
        return []
    sd = statistics.stdev(values) if n > 1 else 0.0
    if sd == 0:
        return []
    threshold = threshold_sigma * sd
    window = max(10, n // 12)  # ~12 segments

    # Build diff-signal: at each i, mean(after) - mean(before)
    diffs: list[float] = [0.0] * n
    for i in range(window, n - window):
        before_mean = statistics.fmean(values[i - window : i])
        after_mean = statistics.fmean(values[i : i + window])
        diffs[i] = after_mean - before_mean

    # Find local maxima of |diff| above threshold
    debounce = window
    points: list[dict[str, float]] = []
    i = window
    while i < n - window:
        if abs(diffs[i]) >= threshold:
            # Find local max-magnitude index within debounce window
            best_idx = i
            best_mag = abs(diffs[i])
            j = i + 1
            end_search = min(i + debounce, n - window)
            while j < end_search:
                if abs(diffs[j]) > best_mag:
                    best_mag = abs(diffs[j])
                    best_idx = j
                j += 1
            direction = 1.0 if diffs[best_idx] > 0 else -1.0
            points.append(
                {
                    "index": float(best_idx),
                    "magnitude": round(diffs[best_idx], 2),
                    "cumsum": round(abs(diffs[best_idx]), 2),
                    "direction": direction,
                }
            )
            i = best_idx + debounce
        else:
            i += 1
    return points


# --- IOB exponential curve (oref0 / AAPS) -----------------------------------


def exponential_iob_fraction(t_min: float, dia_hours: float, peak_min: float) -> float:
    """Fraction of bolus still in circulation at t_min minutes post-bolus.

    Port of oref0 `iob/calculate.js` exponential curve. Used by AAPS for
    ultra-rapid insulin modeling. Returns 1.0 at t=0, monotonically decreasing
    to 0 at t=dia_min.

    Math:
        td = dia_hours * 60
        tp = peak_min
        tau = tp * (1 - tp/td) / (1 - 2*tp/td)
        a = 2*tau/td
        S = 1 / (1 - a + (1+a) * exp(-td/tau))
        iob_frac(t) = 1 - S * (1-a) * ((t²/(tau*td*(1-a)) - t/tau - 1) * exp(-t/tau) + 1)
        for t >= td, iob_frac = 0

    Reference: oref0 commit history; AAPS InsulinOrefBasePlugin.kt.
    """
    td = dia_hours * 60.0
    tp = peak_min
    if t_min < 0:
        return 1.0
    if t_min >= td:
        return 0.0
    # Avoid divide-by-zero when peak is exactly half of DIA
    if abs(1 - 2 * tp / td) < 1e-9:
        tp = tp * 0.99
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    if tau <= 0:
        return 1.0
    a = 2 * tau / td
    s = 1.0 / (1 - a + (1 + a) * math.exp(-td / tau))
    iob_frac = 1.0 - s * (1 - a) * (
        (t_min * t_min / (tau * td * (1 - a)) - t_min / tau - 1) * math.exp(-t_min / tau) + 1
    )
    return max(0.0, min(1.0, iob_frac))


def fit_dia_to_residuals(
    observations: Sequence[tuple[float, float, float]],
    dia_grid: Sequence[float] | None = None,
    peak_grid: Sequence[float] | None = None,
) -> dict[str, float]:
    """Grid-search fit of exponential IOB curve parameters (DIA, peak).

    `observations` is a list of (t_min, predicted_iob_remaining_frac,
    observed_iob_remaining_frac). For each (DIA, peak) candidate we compute
    the residual sum-of-squares between predicted curve and observed
    remaining-IOB inferred from per-bolus BG behaviour.

    Returns dict with best_dia_hours, best_peak_min, rmse, sample_count.

    Note: this is an exploratory tool. The observation model (extracting
    "observed IOB fraction" from BG-change behaviour) is approximate and
    susceptible to confounding (meals, sensor noise, COB). Treat output as
    discussion fodder, not a clinical recommendation.
    """
    if not observations:
        return {
            "best_dia_hours": 0.0,
            "best_peak_min": 0.0,
            "rmse": 0.0,
            "sample_count": 0.0,
        }
    if dia_grid is None:
        dia_grid = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0]
    if peak_grid is None:
        peak_grid = [40, 45, 50, 55, 60, 65, 70, 75, 80]

    best_rmse = float("inf")
    best_dia = 0.0
    best_peak = 0.0
    for dia in dia_grid:
        for peak in peak_grid:
            if peak >= dia * 60 / 2:
                continue  # peak must be < half of DIA in minutes
            sse = 0.0
            n_ok = 0
            for t_min, _, observed in observations:
                predicted = exponential_iob_fraction(t_min, dia, peak)
                sse += (predicted - observed) ** 2
                n_ok += 1
            if n_ok == 0:
                continue
            rmse = math.sqrt(sse / n_ok)
            if rmse < best_rmse:
                best_rmse = rmse
                best_dia = float(dia)
                best_peak = float(peak)
    return {
        "best_dia_hours": best_dia,
        "best_peak_min": best_peak,
        "rmse": round(best_rmse, 4),
        "sample_count": float(len(observations)),
    }
