"""Pydantic v2 response models.

We parse Nightscout's raw JSON into typed models, then convert to a
flat, LLM-friendly shape with both units, ISO timestamps, and arrow
glyphs precomputed. The LLM should never have to do unit math.

Field aliases let us accept Nightscout's mixed camelCase/snake_case
without forcing the caller to know which is which.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
            dt = datetime.fromtimestamp(self.date_ms / 1000, tz=UTC)
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


class AlgorithmState(BaseModel):
    """Per-cycle AAPS/openaps algorithm state from devicestatus.openaps.suggested.

    All glucose-valued and ISF-valued fields are surfaced in both mg/dL and
    mmol/L (computed in model_post_init). AAPS internal arithmetic is always
    mg/dL — the variable_sens and isfMgdlForCarbs fields are mg/dL/U regardless
    of profile units.
    """

    model_config = ConfigDict(populate_by_name=True)

    algorithm: str | None = None  # e.g. "SMB", "AMA"
    running_dynamic_isf: bool | None = None
    current_bg_mgdl: int | None = None  # what AAPS sees as current BG
    current_bg_mmol: float | None = None
    eventual_bg_mgdl: int | None = None  # predicted BG several hours out
    eventual_bg_mmol: float | None = None
    target_bg_mgdl: int | None = None  # current target
    target_bg_mmol: float | None = None
    bg_tick: str | None = None  # AAPS-format like "+3" or "-9" (delta from prior tick)
    effective_isf_mgdl_per_u: float | None = None  # variable_sens, mg/dL/U
    effective_isf_mmol_per_u: float | None = None
    isf_for_carbs_mgdl_per_u: float | None = None  # isfMgdlForCarbs, mg/dL/U
    isf_for_carbs_mmol_per_u: float | None = None
    sensitivity_ratio: float | None = None  # AAPS Autosens multiplier, 1.0 = neutral
    insulin_required_u: float | None = None  # AAPS-calculated correction need
    carbs_required_g: float | None = None  # AAPS-calculated rescue carbs need
    smb_units: float | None = None  # SMB proposed/delivered this cycle
    reason: str | None = None  # full algorithm reason text (no truncation)

    def model_post_init(self, _ctx: Any) -> None:
        if self.current_bg_mgdl is not None:
            self.current_bg_mmol = mgdl_to_mmol(self.current_bg_mgdl)
        if self.eventual_bg_mgdl is not None:
            self.eventual_bg_mmol = mgdl_to_mmol(self.eventual_bg_mgdl)
        if self.target_bg_mgdl is not None:
            self.target_bg_mmol = mgdl_to_mmol(self.target_bg_mgdl)
        if self.effective_isf_mgdl_per_u is not None:
            self.effective_isf_mmol_per_u = mgdl_to_mmol(self.effective_isf_mgdl_per_u)
        if self.isf_for_carbs_mgdl_per_u is not None:
            self.isf_for_carbs_mmol_per_u = mgdl_to_mmol(self.isf_for_carbs_mgdl_per_u)


class BgPredictions(BaseModel):
    """Summarized AAPS BG-prediction trajectories from openaps.suggested.predBGs.

    Each trajectory is a list of predicted BG values at 5-min cadence. We drop
    the array bodies (~30 values per trajectory × 4 trajectories = LLM noise)
    and surface only the trajectory length and final-value endpoint per type.
    """

    model_config = ConfigDict(populate_by_name=True)

    iob_minutes_ahead: int | None = None  # predBGs.IOB length × 5 min
    iob_endpoint_mgdl: int | None = None  # last value of predBGs.IOB
    iob_endpoint_mmol: float | None = None
    cob_minutes_ahead: int | None = None
    cob_endpoint_mgdl: int | None = None
    cob_endpoint_mmol: float | None = None
    uam_minutes_ahead: int | None = None  # Unannounced Meal trajectory
    uam_endpoint_mgdl: int | None = None
    uam_endpoint_mmol: float | None = None
    zt_minutes_ahead: int | None = None  # Zero-Temp prediction
    zt_endpoint_mgdl: int | None = None
    zt_endpoint_mmol: float | None = None

    def model_post_init(self, _ctx: Any) -> None:
        for prefix in ("iob", "cob", "uam", "zt"):
            mgdl = getattr(self, f"{prefix}_endpoint_mgdl")
            if mgdl is not None:
                setattr(self, f"{prefix}_endpoint_mmol", mgdl_to_mmol(mgdl))


class DeviceStatusSummary(BaseModel):
    """One row from the devicestatus collection, flattened.

    AAPS publishes a deeply nested algorithm state per loop cycle (~5 min).
    This model surfaces every clinically-useful field: pump basics, IOB/COB
    (rich detail), pump.extended profile/version/temp-basal metadata, AAPS
    per-cycle algorithm state including Dynamic ISF effective ISF, and
    summarized BG prediction trajectories.
    """

    # Identity
    device: str | None = None
    created_at: str | None = None
    phone_charging: bool | None = None  # top-level isCharging
    uploader_battery_percent: int | None = None

    # Pump basics
    pump_battery_percent: int | None = None
    pump_reservoir_u: float | None = None
    pump_battery_voltage: float | None = None

    # IOB / COB (top-level for backwards compat with existing tools/tests)
    iob_u: float | None = None
    cob_g: float | None = None
    basal_iob_u: float | None = None  # IOB attributable to basal vs bolus
    insulin_activity: float | None = None  # rate of insulin action

    # Loop enacted (existing fields preserved)
    loop_enacted_rate: float | None = None
    loop_enacted_duration_min: int | None = None
    loop_temp_basal_minutes_remaining: int | None = None
    suggested_temp: str | None = None  # truncated reason for back-compat

    # Pump extended
    active_profile: str | None = None
    base_basal_rate_uph: float | None = None
    last_bolus_at: str | None = None  # AAPS-format string, e.g. "5/22/26 11:41 PM"
    last_bolus_units: float | None = None
    temp_basal_absolute_rate_uph: float | None = None
    temp_basal_started_at: str | None = None  # AAPS-format string
    aaps_version: str | None = None  # e.g. "3.4.0.0-796d36ef0d-2026.01.04"
    pump_status_text: str | None = None  # "Closed Loop" / "Open Loop" / "LGS" / ...

    # Rich nested sub-views
    algorithm: AlgorithmState | None = None
    predictions: BgPredictions | None = None


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
    stats: GlucoseStats
    treatment_count: int
    total_insulin_u: float
    total_carbs_g: float
    notes: list[str]


class PeriodComparison(BaseModel):
    """Side-by-side stats over two arbitrary date ranges."""

    period_a_label: str
    period_b_label: str
    period_a: GlucoseStats
    period_b: GlucoseStats
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


class IsfBandSample(BaseModel):
    """Per-BG-band breakdown for effective_isf_check.

    Boundaries are half-open: a sample with pre-bolus BG exactly at `band_upper`
    falls into the NEXT band, not this one. `band_upper` is None for the
    open-ended top band.
    """

    band_label: str  # "below_target" | "in_target" | "above_target"
    band_lower_mgdl: int
    band_upper_mgdl: int | None  # None = open upper bound
    sample_count: int
    avg_effective_isf_mmol_per_unit: float | None
    avg_realized_isf_mmol_per_unit: float | None
    ratio_realized_over_effective: float | None
    note: str | None = None  # populated when sample_count < 3 (insufficient signal)


class EffectiveIsfDerivation(BaseModel):
    """Real-world ISF analysis vs AAPS's per-correction effective ISF.

    For AAPS Dynamic ISF users, this is the right comparison (vs. the
    profile-ISF comparison in IsfDerivation): we read what AAPS *actually
    used* at each correction moment from devicestatus.openaps.suggested.sens
    and compare it to the realized BG drop. Stratified by pre-bolus BG band
    so users can see whether the Dynamic ISF formula's BG curve is
    mis-calibrated independently from the global Adjustment Factor.
    """

    sample_count: int  # eligible corrections with a matched sens value
    devicestatus_rows_examined: int  # total dev-status rows fetched in the window
    samples_without_sens: int  # corrections lacking a sens match within tolerance
    avg_effective_isf_mmol_per_unit: float | None  # what AAPS used, averaged
    avg_realized_isf_mmol_per_unit: float | None  # what actually happened, averaged
    overall_ratio_realized_over_effective: float | None  # >1 = AAPS over-doses
    confidence: str  # "low" | "medium" | "high"
    by_bg_band: list[IsfBandSample]
    recommendation: str


class GlucoseAtTime(BaseModel):
    """The CGM reading closest to a queried timestamp."""

    requested_iso: str
    sgv_mgdl: int | None
    sgv_mmol: float | None
    direction: str | None
    trend_arrow: str
    actual_iso: str | None
    minutes_from_requested: int | None  # signed: negative = before requested, positive = after
    within_tolerance: bool  # True if closest reading is within +/- 15 min


class CrDerivation(BaseModel):
    """Real-world carb-ratio analysis derived from meal-bolus outcomes.

    Two signals:
      1. derived_cr_g_per_unit = average of (carbs / insulin) across meals,
         i.e. the ratio the user actually applied. Compared to profile CR.
      2. avg_end_minus_pre_mgdl = how meals tend to end relative to where
         they started. Negative = consistently dropping (over-bolused);
         positive = consistently rising (under-bolused); near zero = right.
    """

    sample_count: int
    derived_cr_g_per_unit: float | None
    profile_cr_g_per_unit: float | None
    ratio_derived_over_profile: float | None
    avg_end_minus_pre_mgdl: float | None  # post-meal residual signal
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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
