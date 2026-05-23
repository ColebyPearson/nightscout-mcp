"""Glucose statistics.

All math is unit-aware: thresholds and stored values are in mg/dL since
that's how Nightscout stores them, and we surface mmol/L in addition.

GMI (Glucose Management Indicator) is the modern A1C estimator:
    GMI(%) = 3.31 + 0.02392 × mean_mgdl
Source: Bergenstal RM et al., Diabetes Care 2018; 41:2275-2280.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable

from .models import GlucoseStats, Sgv
from .units import mgdl_to_mmol

DEFAULT_TIR_LOW = 70
DEFAULT_TIR_HIGH = 180


def gmi_percent(mean_mgdl: float) -> float:
    """Glucose Management Indicator, the modern A1C estimator."""
    return round(3.31 + 0.02392 * mean_mgdl, 2)


def compute_stats(
    readings: Iterable[Sgv],
    window_hours: int,
    tir_low: int = DEFAULT_TIR_LOW,
    tir_high: int = DEFAULT_TIR_HIGH,
) -> GlucoseStats:
    """Aggregate a sequence of SGVs into a GlucoseStats payload.

    Empty input returns a zero-filled stats object (NOT an error). The LLM
    is better served by an explicit "0 readings" than by a thrown exception.
    """
    values = [r.sgv_mgdl for r in readings if r.type == "sgv"]
    n = len(values)

    if n == 0:
        return GlucoseStats(
            window_hours=window_hours,
            reading_count=0,
            mean_mgdl=0.0,
            mean_mmol=0.0,
            sd_mgdl=0.0,
            cv_percent=0.0,
            gmi_percent=0.0,
            tir_percent=0.0,
            tbr_lt70_percent=0.0,
            tbr_lt54_percent=0.0,
            tar_gt180_percent=0.0,
            tar_gt250_percent=0.0,
            tir_low_threshold_mgdl=tir_low,
            tir_high_threshold_mgdl=tir_high,
        )

    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    cv = (sd / mean * 100) if mean else 0.0

    in_range = sum(1 for v in values if tir_low <= v <= tir_high)
    below_70 = sum(1 for v in values if v < 70)
    below_54 = sum(1 for v in values if v < 54)
    above_180 = sum(1 for v in values if v > 180)
    above_250 = sum(1 for v in values if v > 250)

    def pct(count: int) -> float:
        return round(count / n * 100, 1)

    return GlucoseStats(
        window_hours=window_hours,
        reading_count=n,
        mean_mgdl=round(mean, 1),
        mean_mmol=mgdl_to_mmol(mean),
        sd_mgdl=round(sd, 1),
        cv_percent=round(cv, 1),
        gmi_percent=gmi_percent(mean),
        tir_percent=pct(in_range),
        tbr_lt70_percent=pct(below_70),
        tbr_lt54_percent=pct(below_54),
        tar_gt180_percent=pct(above_180),
        tar_gt250_percent=pct(above_250),
        tir_low_threshold_mgdl=tir_low,
        tir_high_threshold_mgdl=tir_high,
    )
