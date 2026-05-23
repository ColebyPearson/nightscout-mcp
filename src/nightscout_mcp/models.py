"""Pydantic v2 response models.

We parse Nightscout's raw JSON into typed models, then convert to a
flat, LLM-friendly shape with both units, ISO timestamps, and arrow
glyphs precomputed. The LLM should never have to do unit math.

Field aliases let us accept Nightscout's mixed camelCase/snake_case
without forcing the caller to know which is which.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .units import direction_to_arrow, mgdl_to_mmol

# --- Glucose readings ---------------------------------------------------------


class Sgv(BaseModel):
    """A single CGM reading.

    Some Nightscout uploaders omit `dateString` and only write `date` (Unix
    ms). We accept both and synthesize the ISO form when missing so downstream
    code can rely on `date_iso` being present.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    sgv_mgdl: int = Field(..., alias="sgv")
    sgv_mmol: float = 0.0  # populated in model_post_init
    direction: str | None = None
    trend_arrow: str = "?"
    date_ms: int = Field(..., alias="date")
    date_iso: str = Field(default="", alias="dateString")
    type: str = "sgv"
    device: str | None = None

    def model_post_init(self, _ctx: Any) -> None:
        self.sgv_mmol = mgdl_to_mmol(self.sgv_mgdl)
        self.trend_arrow = direction_to_arrow(self.direction)
        if not self.date_iso:
            # Build ISO from the millisecond timestamp.
            dt = datetime.fromtimestamp(self.date_ms / 1000, tz=timezone.utc)
            self.date_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class CurrentGlucose(BaseModel):
    """Latest SGV with derived freshness and delta vs prior reading."""

    sgv_mgdl: int
    sgv_mmol: float
    direction: str | None
    trend_arrow: str
    date_iso: str
    minutes_ago: int
    delta_mgdl: int | None = None
    delta_mmol: float | None = None
    device: str | None = None


# --- Treatments ---------------------------------------------------------------


class Treatment(BaseModel):
    """A single treatment record (bolus, carb, basal, note, etc.)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = Field(default=None, alias="_id")
    event_type: str = Field(..., alias="eventType")
    created_at: str = Field(..., alias="created_at")
    insulin: float | None = None
    carbs: float | None = None
    duration: float | None = None  # minutes
    absolute: float | None = None  # for temp basals (U/h)
    percent: float | None = None  # for temp basals (% of profile)
    notes: str | None = None
    entered_by: str | None = Field(default=None, alias="enteredBy")
    glucose: float | None = None
    glucose_type: str | None = Field(default=None, alias="glucoseType")


# --- Profile ------------------------------------------------------------------


class ScheduleEntry(BaseModel):
    """A single time-of-day → value entry in a basal/ISF/CR schedule."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    time: str  # "HH:MM"
    value: float


class ProfileSummary(BaseModel):
    """Active profile, simplified to the bits the LLM actually needs."""

    name: str
    units: str  # "mg/dL" or "mmol"
    timezone: str
    dia_hours: float
    basal: list[ScheduleEntry]
    isf: list[ScheduleEntry]
    carb_ratio: list[ScheduleEntry]
    target_low: list[ScheduleEntry]
    target_high: list[ScheduleEntry]


# --- Device status / IOB / COB -----------------------------------------------


class DeviceStatusSummary(BaseModel):
    """One row from the devicestatus collection, flattened."""

    device: str | None = None
    created_at: str | None = None
    # Pump fields (when present)
    pump_battery_percent: int | None = None
    pump_reservoir_u: float | None = None
    pump_battery_voltage: float | None = None
    # Loop / OpenAPS / AAPS fields (when present)
    iob_u: float | None = None
    cob_g: float | None = None
    loop_enacted_rate: float | None = None
    loop_enacted_duration_min: int | None = None
    loop_temp_basal_minutes_remaining: int | None = None
    suggested_temp: str | None = None  # human-readable suggestion summary
    # Uploader battery (phone)
    uploader_battery_percent: int | None = None


class IobCob(BaseModel):
    """Latest insulin-on-board and carbs-on-board from devicestatus."""

    iob_u: float | None
    cob_g: float | None
    source: str  # "openaps" | "loop" | "pump" | "unavailable"
    as_of_iso: str | None


# --- Stats --------------------------------------------------------------------


class GlucoseStats(BaseModel):
    """Aggregate stats over a window of SGVs."""

    window_hours: int
    reading_count: int
    mean_mgdl: float
    mean_mmol: float
    sd_mgdl: float
    cv_percent: float
    gmi_percent: float
    tir_percent: float
    tbr_lt70_percent: float
    tbr_lt54_percent: float
    tar_gt180_percent: float
    tar_gt250_percent: float
    tir_low_threshold_mgdl: int
    tir_high_threshold_mgdl: int


# --- Server status -----------------------------------------------------------


# --- Phase 2 analytics models ------------------------------------------------


class DailyReport(BaseModel):
    """One-day rollup: stats + treatments + key events."""

    date: str  # YYYY-MM-DD
    stats: "GlucoseStats"
    treatment_count: int
    total_insulin_u: float
    total_carbs_g: float
    notes: list[str]


class PeriodComparison(BaseModel):
    """Side-by-side stats over two arbitrary date ranges."""

    period_a_label: str
    period_b_label: str
    period_a: "GlucoseStats"
    period_b: "GlucoseStats"
    delta_mean_mgdl: float
    delta_tir_pp: float  # percentage points
    delta_gmi_pp: float
    improvement_summary: str  # human-readable


class MealAnalysis(BaseModel):
    """Glucose response in a window after a meal/carb entry."""

    meal_time_iso: str
    window_hours: int
    carbs_g: float | None
    insulin_u: float | None
    pre_meal_mgdl: int | None
    pre_meal_mmol: float | None
    peak_mgdl: int
    peak_mmol: float
    peak_at_iso: str
    time_to_peak_minutes: int
    rise_mgdl: int  # peak - pre_meal
    end_mgdl: int  # value at end of window
    end_mmol: float
    in_range_at_end: bool
    notes: list[str]


class OvernightAnalysis(BaseModel):
    """Stability + drift + dawn-effect characterization for a single night.

    "Night" = 00:00–07:00 local-equivalent UTC for the requested date.
    """

    night_date: str  # YYYY-MM-DD
    reading_count: int
    start_mgdl: int | None
    end_mgdl: int | None
    drift_mgdl: int | None  # end - start
    min_mgdl: int | None
    max_mgdl: int | None
    time_below_70_minutes: int
    time_below_54_minutes: int
    dawn_rise_mgdl: int | None  # value at 07:00 - value at 03:00
    flat_pct: float  # share of intervals where |delta| ≤ 5 mg/dL


class Pattern(BaseModel):
    """A single recurring glucose pattern detected over multiple days."""

    type: str  # e.g. "overnight_low", "dawn_phenomenon", "post_meal_spike"
    occurrence_count: int
    sample_times_iso: list[str]
    avg_value_mgdl: float
    description: str


class DetectedPatterns(BaseModel):
    """Output of detect_patterns — grouped by pattern type."""

    days_analyzed: int
    patterns: list[Pattern]


class IsfDerivation(BaseModel):
    """Real-world ISF derived from correction-bolus outcomes."""

    sample_count: int  # eligible correction boluses
    derived_isf_mgdl_per_unit: float | None
    derived_isf_mmol_per_unit: float | None
    profile_isf_mmol_per_unit: float | None
    ratio_derived_over_profile: float | None  # >1 means LESS sensitive than profile
    confidence: str  # "low" | "medium" | "high"
    recommendation: str


class SuspectedCompression(BaseModel):
    """One CGM dip that looks like a sensor-compression artifact."""

    start_iso: str
    min_iso: str
    min_mgdl: int
    recovery_iso: str
    drop_rate_mgdl_per_min: float
    recovery_rate_mgdl_per_min: float
    duration_minutes: int


class CompressionAnalysis(BaseModel):
    """Output of compression_low_analysis."""

    days_analyzed: int
    suspected: list[SuspectedCompression]
    note: str


class ServerStatus(BaseModel):
    """A subset of /api/v1/status.json that's actually useful."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None
    status: str | None = None
    name: str | None = None
    server_units: str | None = None  # "mg/dL" or "mmol"
    api_enabled: bool | None = None


# --- Helpers ----------------------------------------------------------------


def parse_iso_to_utc(iso: str) -> datetime:
    """Parse a Nightscout ISO timestamp (Z or +00:00) to a UTC datetime."""
    # Nightscout sends both 'Z' and '+00:00' variants; fromisoformat handles
    # the latter natively. Normalize Z to +00:00.
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
