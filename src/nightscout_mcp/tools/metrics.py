"""Research-driven metrics tools.

Adds Klonoff 2023 GRI, Kovatchev 1998 LBGI/HBGI, Battelino 2019 AGP, etc.
on top of the existing analytics surface. All formulas live in
src/nightscout_mcp/metrics.py — these wrappers just fetch + delegate.

Tools added (15 new MCP tools, 20 → 35 total):
- glycemia_risk_index               GRI + components (Klonoff 2023)
- bg_risk_indices                   LBGI/HBGI/ADRR (Kovatchev 1998/2006)
- glucose_variability_metrics       MAGE, MODD, J-index, M-value, GVP, CONGA-n, COGI
- time_in_range_with_ci             TIR/TBR/TAR with Wilson 95% CIs
- per_meal_period_tir               TIR by breakfast/lunch/dinner/overnight/etc.
- ambulatory_glucose_profile        AGP percentile bands by hour-of-day
- change_points_bg                  CUSUM change-point detection on hourly mean BG
- change_points_tdd                 CUSUM change-point detection on daily TDD
- bolus_event_residuals             Per-bolus residual vs AAPS Dynamic ISF + per-band
- dia_fit_estimate                  Exploratory IOB-curve fit to recommend DIA + peak
- clinic_packet                     30-day composite markdown report for endo visits
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import metrics as M
from ..analytics import hypo_episodes as _hypo_episodes
from ..client import NightscoutClient
from ..models import (
    AgpHourPoint,
    AgpMarkdownRender,
    AmbulatoryGlucoseProfile,
    BgRiskIndices,
    BolusBandAggregate,
    BolusEvent,
    BolusEventResidualsReport,
    ChangePoint,
    ChangePointReport,
    ClinicPacket,
    ConsensusTargetAudit,
    DataSufficiency,
    DiaFitResult,
    DynIsfRecommendation,
    GlucoseVariability,
    GlycemiaRiskIndex,
    MealPeriodReport,
    MealPeriodTir,
    PeriodCompareReport,
    PeriodMetrics,
    SettingChangeAttribution,
    SettingChangeAttributionReport,
    Sgv,
    TargetCheck,
    TirBands,
    TirWithCI,
    Treatment,
    parse_iso_to_utc,
)
from ..units import mgdl_to_mmol
from .analytics import (
    _extract_profile_settings,
    _fetch_devicestatus_between,
    _fetch_sgvs_between,
    _fetch_treatments_between,
    _resolve_timezone,
)


def _now_utc_midnight_plus_one() -> datetime:
    """End-of-today UTC, suitable as the right edge of a 'past N days' window."""
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _window_for_days(days: int) -> tuple[datetime, datetime]:
    days = max(1, min(days, 90))
    end = _now_utc_midnight_plus_one()
    start = end - timedelta(days=days)
    return (start, end)


def _bg_band_label(bg_mgdl: float) -> str:
    """Map a BG value to one of the per-band labels used by bolus_event_residuals."""
    if bg_mgdl < 70:
        return "below_70"
    if bg_mgdl < 100:
        return "70_100"
    if bg_mgdl < 140:
        return "100_140"
    if bg_mgdl < 180:
        return "140_180"
    if bg_mgdl < 250:
        return "180_250"
    return "over_250"


def _time_band_label(ts: datetime, tz_offset_hours: float = 0.0) -> str:
    h = int((ts.hour + tz_offset_hours) % 24)
    if 0 <= h < 6:
        return "overnight"
    if 6 <= h < 12:
        return "morning"
    if 12 <= h < 18:
        return "afternoon"
    return "evening"


def _safe_get(d: dict[str, Any], path: str, default: Any = None) -> Any:
    """Walk a dotted path through nested dicts, returning default on any miss."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


# Consensus glycemic-target profiles by population (Battelino Diabetes Care
# 2019;42:1593 Table + ISPAD 2022). TIR range and the TBR/TAR cut-points and
# limits vary by population; the risk indices (GRI/LBGI/HBGI/CV/GMI) do not.
# `tar_vhigh_cut=None` means that population has no separate very-high band.
TARGET_POPULATIONS: dict[str, dict[str, Any]] = {
    "standard": {
        "label": "T1D / T2D, including children & adolescents",
        "tir_low": 70,
        "tir_high": 180,
        "tir_min_pct": 70.0,
        "tbr_low_cut": 70,
        "tbr_low_max_pct": 4.0,
        "tbr_vlow_cut": 54,
        "tbr_vlow_max_pct": 1.0,
        "tar_vhigh_cut": 250,
        "tar_high_max_pct": 25.0,
        "tar_vhigh_max_pct": 5.0,
        "citation": "Battelino 2019; ISPAD 2022",
    },
    "older_high_risk": {
        "label": "older / high-risk (hypoglycemia-avoidant)",
        "tir_low": 70,
        "tir_high": 180,
        "tir_min_pct": 50.0,
        "tbr_low_cut": 70,
        "tbr_low_max_pct": 1.0,  # stricter on lows
        "tbr_vlow_cut": 54,
        "tbr_vlow_max_pct": 1.0,
        "tar_vhigh_cut": 250,
        "tar_high_max_pct": 50.0,
        "tar_vhigh_max_pct": 10.0,
        "citation": "Battelino 2019 (older / high-risk)",
    },
    "pregnancy_t1d": {
        "label": "pregnancy (T1D)",
        "tir_low": 63,
        "tir_high": 140,
        "tir_min_pct": 70.0,
        "tbr_low_cut": 63,
        "tbr_low_max_pct": 4.0,
        "tbr_vlow_cut": 54,
        "tbr_vlow_max_pct": 1.0,
        "tar_vhigh_cut": None,
        "tar_high_max_pct": 25.0,
        "tar_vhigh_max_pct": None,
        "citation": "Battelino 2019 (pregnancy T1D)",
    },
}


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach the research-metrics tools to a FastMCP instance."""

    # ------------------------------------------------------------------------
    # PR #1 — Glycemic risk + variability suite
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def glycemia_risk_index(days: int = 14) -> GlycemiaRiskIndex:
        """Compute the Glycemia Risk Index (Klonoff JDST 2023;17:1226).

        GRI = 3.0·%VLow<54 + 2.4·%Low54-69 + 1.6·%VHigh>250 + 0.8·%High181-250

        Returns total GRI plus hypo/hyper component decomposition and the
        underlying per-band CGM percentages. Window of N days (max 90).
        """
        client = get_client()
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        values = [float(s.sgv_mgdl) for s in sgvs if s.type == "sgv" and s.sgv_mgdl > 0]
        result = M.gri(values)
        return GlycemiaRiskIndex(
            gri=result["gri"],
            gri_hypo_component=result["gri_hypo"],
            gri_hyper_component=result["gri_hyper"],
            pct_very_low_lt54=result["pct_very_low_lt54"],
            pct_low_54_69=result["pct_low_54_69"],
            pct_in_target_70_180=result["pct_in_target_70_180"],
            pct_high_181_250=result["pct_high_181_250"],
            pct_very_high_gt250=result["pct_very_high_gt250"],
            sample_count=len(values),
            days=max(1, min(days, 90)),
        )

    @mcp.tool()
    async def data_sufficiency_report(days: int = 14) -> DataSufficiency:
        """Is there enough CGM data for the metrics to be trustworthy?

        Battelino 2019 requires >=14 days at >=70% sensor-active time before
        AGP / TIR-consensus metrics are interpretable. Returns days-with-data,
        % CGM-active, longest gap, and a `meets_agp_consensus` flag — call this
        first when a report looks surprising, since sensor gaps are non-random
        (warmup/failure) and bias metrics low.
        """
        client = get_client()
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        return DataSufficiency(**M.data_sufficiency(sgvs, max(1, min(days, 90))))

    @mcp.tool()
    async def bg_risk_indices(days: int = 14) -> BgRiskIndices:
        """LBGI / HBGI / ADRR per Kovatchev *Diabetes Care* 1998;21:1870 and 2006;29:2433.

        LBGI bands: <1.1 low, 1.1–2.5 moderate, 2.5–5.0 high, >5.0 very high.
        HBGI bands: <4.5 low, 4.5–9.0 moderate, 9.0–15.0 high, >15.0 very high.
        """
        client = get_client()
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        values = [float(s.sgv_mgdl) for s in sgvs if s.type == "sgv" and s.sgv_mgdl > 0]
        lbgi, hbgi = M.lbgi_hbgi(values)

        # ADRR needs per-day partitioning
        pairs = M._sgv_to_pairs(sgvs)
        by_day: dict[str, list[float]] = {}
        for ts, v in pairs:
            key = ts.strftime("%Y-%m-%d")
            by_day.setdefault(key, []).append(v)
        adrr_value = M.adrr(list(by_day.values()))

        return BgRiskIndices(
            lbgi=lbgi,
            hbgi=hbgi,
            adrr=adrr_value,
            lbgi_band=M.lbgi_band(lbgi),
            hbgi_band=M.hbgi_band(hbgi),
            sample_count=len(values),
            days=max(1, min(days, 90)),
        )

    @mcp.tool()
    async def glucose_variability_metrics(days: int = 14) -> GlucoseVariability:
        """Comprehensive glucose-variability metrics not covered by basic CV/GMI.

        - MAGE: Service 1970 — mean excursion amplitude exceeding 1 SD
        - MODD: Molnar 1972 — mean |BG(t) − BG(t−24h)|
        - J-index: Wojcicki 1995 — 0.001·(mean+SD)²
        - M-value: Schlichtkrull 1965 — mean |10·log10(BG/120)|³
        - GVP: Peyser 2018 — arc-length per unit time minus 1
        - CONGA-{1h,2h,4h}: McDonnell 2005 — SD of n-hour differences
        - COGI: Leelarathna 2020 — weighted composite of TIR + TBR<70 + CV
        - CV percent: SD/mean × 100, target <36% (Battelino 2019)
        """
        client = get_client()
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        values = M._sgv_to_mgdl_list(sgvs)
        pairs = M._sgv_to_pairs(sgvs)

        cv = M.cv_percent(values)
        tir_70_180 = sum(1 for v in values if 70 <= v <= 180) / len(values) * 100 if values else 0.0
        tbr_lt70 = sum(1 for v in values if v < 70) / len(values) * 100 if values else 0.0

        return GlucoseVariability(
            cv_percent=cv,
            mage=M.mage(values),
            modd=M.modd(pairs),
            j_index=M.j_index(values),
            m_value=M.m_value(values),
            gvp=M.gvp(pairs),
            conga_1h=M.conga(pairs, n_hours=1),
            conga_2h=M.conga(pairs, n_hours=2),
            conga_4h=M.conga(pairs, n_hours=4),
            cogi=M.cogi(tir_70_180, tbr_lt70, cv),
            sample_count=len(values),
            days=max(1, min(days, 90)),
        )

    @mcp.tool()
    async def time_in_range_with_ci(days: int = 14) -> TirWithCI:
        """Time-in-range bands with Wilson-score 95% confidence intervals.

        Reference: Battelino 2019 *Diabetes Care* 42:1593 (TIR consensus). CIs
        derived from the binomial Wilson interval (better small-sample behaviour
        than the normal approximation), useful for stating "is my TBR<54
        STATISTICALLY above 1%" with appropriate humility.
        """
        client = get_client()
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        values = M._sgv_to_mgdl_list(sgvs)
        n = len(values)

        very_low = sum(1 for v in values if v < 54)
        low = sum(1 for v in values if 54 <= v < 70)
        in_target = sum(1 for v in values if 70 <= v <= 180)
        high = sum(1 for v in values if 180 < v <= 250)
        very_high = sum(1 for v in values if v > 250)
        below_70 = very_low + low
        above_180 = high + very_high

        def _pct(c: int) -> float:
            return round(c / n * 100, 2) if n else 0.0

        bands = TirBands(
            pct_very_low_lt54=_pct(very_low),
            pct_very_low_lt54_ci=M.wilson_ci_95(very_low, n),
            pct_low_54_69=_pct(low),
            pct_low_54_69_ci=M.wilson_ci_95(low, n),
            pct_in_target_70_180=_pct(in_target),
            pct_in_target_70_180_ci=M.wilson_ci_95(in_target, n),
            pct_high_181_250=_pct(high),
            pct_high_181_250_ci=M.wilson_ci_95(high, n),
            pct_very_high_gt250=_pct(very_high),
            pct_very_high_gt250_ci=M.wilson_ci_95(very_high, n),
            pct_tbr_lt70_combined=_pct(below_70),
            pct_tar_gt180_combined=_pct(above_180),
            sample_count=n,
        )
        cv = M.cv_percent(values)
        return TirWithCI(
            days=max(1, min(days, 90)),
            bands=bands,
            cv_percent_value=cv,
            cv_percent_target_36=cv <= M.CV_TARGET_PERCENT and cv > 0,
        )

    # ------------------------------------------------------------------------
    # PR #2 — Time-stratified analytics
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def per_meal_period_tir(days: int = 14, timezone: str | None = None) -> MealPeriodReport:
        """TIR broken out by meal period (overnight/breakfast/lunch/afternoon/dinner/evening).

        Hour windows (local time):
          overnight: 00-06, breakfast: 06-11, lunch: 11-14,
          afternoon: 14-17, dinner: 17-21, evening: 21-24

        Args:
            days: analysis window in days (max 90)
            timezone: Olson zone name. Auto-detect from profile if None.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        tz_offset_hours = _tz_offset_for(tz_name)

        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        pairs = M._sgv_to_pairs(sgvs)
        partitions = M.partition_by_local_hour(pairs, tz_offset_hours=tz_offset_hours)

        periods: list[MealPeriodTir] = []
        for name, (h_start, h_end) in M.DEFAULT_MEAL_PERIODS.items():
            vals = partitions.get(name, [])
            n = len(vals)
            mean = sum(vals) / n if n else 0.0
            in_target = sum(1 for v in vals if 70 <= v <= 180)
            below = sum(1 for v in vals if v < 70)
            above = sum(1 for v in vals if v > 180)
            periods.append(
                MealPeriodTir(
                    period_name=name,
                    hour_start=h_start,
                    hour_end=h_end,
                    sample_count=n,
                    mean_mgdl=round(mean, 1),
                    mean_mmol=mgdl_to_mmol(mean) if mean else 0.0,
                    pct_in_target_70_180=round(in_target / n * 100, 2) if n else 0.0,
                    pct_tbr_lt70=round(below / n * 100, 2) if n else 0.0,
                    pct_tar_gt180=round(above / n * 100, 2) if n else 0.0,
                )
            )
        return MealPeriodReport(
            days=max(1, min(days, 90)),
            timezone=tz_name,
            periods=periods,
        )

    @mcp.tool()
    async def ambulatory_glucose_profile(days: int = 14, timezone: str | None = None) -> AmbulatoryGlucoseProfile:
        """AGP-style 5/25/50/75/95th percentile bands by hour-of-day.

        Reference: Battelino 2019 *Diabetes Care* 42:1593 AGP consensus.
        Each of 24 hours gets percentile values from BG samples falling in
        that local-time hour over the analysis window.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        tz_offset_hours = _tz_offset_for(tz_name)

        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        pairs = M._sgv_to_pairs(sgvs)
        hourly = M.agp_hourly_percentiles(pairs, tz_offset_hours=tz_offset_hours)

        points: list[AgpHourPoint] = []
        for h_entry in hourly:
            p05 = h_entry.get("p05", 0.0)
            p25 = h_entry.get("p25", 0.0)
            p50 = h_entry.get("p50", 0.0)
            p75 = h_entry.get("p75", 0.0)
            p95 = h_entry.get("p95", 0.0)
            points.append(
                AgpHourPoint(
                    hour=int(h_entry["hour"]),
                    sample_count=int(h_entry["sample_count"]),
                    p05_mgdl=p05,
                    p25_mgdl=p25,
                    p50_mgdl=p50,
                    p75_mgdl=p75,
                    p95_mgdl=p95,
                    p05_mmol=mgdl_to_mmol(p05) if p05 else 0.0,
                    p25_mmol=mgdl_to_mmol(p25) if p25 else 0.0,
                    p50_mmol=mgdl_to_mmol(p50) if p50 else 0.0,
                    p75_mmol=mgdl_to_mmol(p75) if p75 else 0.0,
                    p95_mmol=mgdl_to_mmol(p95) if p95 else 0.0,
                )
            )
        return AmbulatoryGlucoseProfile(
            days=max(1, min(days, 90)),
            timezone=tz_name,
            hours=points,
        )

    # ------------------------------------------------------------------------
    # PR #3 — Bolus event residuals (rich per-bolus extractor)
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def bolus_event_residuals(days: int = 14) -> BolusEventResidualsReport:
        """Per-bolus residual analysis stratified by BG band and time of day.

        For each correction bolus in the window:
          - find pre-bolus BG (closest CGM reading ≤ bolus time)
          - find min BG in [bolus_time, bolus_time + min(DIA, 5h)]
          - find closest devicestatus row before bolus (±15min) — read IOB,
            COB, AAPS predicted eventual BG, AAPS effective ISF (variable_sens)
          - compute realized drop and realized ISF
          - assign BG band (per pre-BG) and time band (per local hour)

        Aggregates report per-band mean of realized vs AAPS-effective ISF.
        Overall ratio of realized/effective is the headline number — values
        meaningfully >1.0 indicate AAPS is over-dosing (realized drop exceeds
        prediction); values <1.0 indicate under-dosing.

        Args:
            days: analysis window (max 90).
        """
        client = get_client()
        days = max(1, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        treatments = await _fetch_treatments_between(client, start, end)
        devicestatus = await _fetch_devicestatus_between(client, start, end)
        _, _, dia_hours, _ = await _extract_profile_settings(client)

        events = _build_bolus_events(sgvs, treatments, devicestatus, dia_hours)

        by_bg_band = _aggregate(events, lambda e: e.bg_band)
        by_time_band = _aggregate(events, lambda e: e.time_band)

        # Overall ratio across ALL events with both fields populated
        with_both = [e for e in events if e.realized_isf_mgdl_per_u and e.aaps_effective_isf_mgdl_per_u]
        if with_both:
            ratios = [
                e.realized_isf_mgdl_per_u / e.aaps_effective_isf_mgdl_per_u
                for e in with_both
                if e.aaps_effective_isf_mgdl_per_u
            ]
            overall = round(sum(ratios) / len(ratios), 3) if ratios else 0.0
        else:
            overall = 0.0

        interpretation = _interpret_isf_ratio(overall, by_bg_band)
        return BolusEventResidualsReport(
            days=days,
            total_events=len(events),
            events_with_aaps_isf_match=len(with_both),
            aggregates_by_bg_band=by_bg_band,
            aggregates_by_time_band=by_time_band,
            overall_ratio=overall,
            interpretation=interpretation,
        )

    # ------------------------------------------------------------------------
    # PR #4 — Change-point detection
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def change_points_bg(days: int = 30, threshold_sigma: float = 4.0) -> ChangePointReport:
        """Detect change-points in hourly mean BG via CUSUM.

        A change-point is flagged where the cumulative deviation from the
        overall mean crosses threshold_sigma × SD. Useful for "when did
        something change?" — pair the output with the profile-change events
        in the same window (returned automatically) to attribute shifts.

        Args:
            days: analysis window in days (max 90, default 30).
            threshold_sigma: sensitivity. Lower = more change-points flagged.
                Default 4.0 is conservative.
        """
        client = get_client()
        days = max(7, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        treatments = await _fetch_treatments_between(client, start, end)

        pairs = M._sgv_to_pairs(sgvs)
        hourly = M.hourly_aggregate(pairs)
        values_only = [v for _, v in hourly]
        timestamps_only = [ts for ts, _ in hourly]

        raw_points = M.cusum_change_points(values_only, threshold_sigma=threshold_sigma)
        change_points: list[ChangePoint] = []
        for p in raw_points:
            idx = int(p["index"])
            if 0 <= idx < len(timestamps_only):
                change_points.append(
                    ChangePoint(
                        timestamp_iso=timestamps_only[idx].isoformat(),
                        index=idx,
                        direction="up" if p["direction"] > 0 else "down",
                        magnitude=p["magnitude"],
                        cumsum=p["cumsum"],
                    )
                )

        profile_changes = [t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at]
        return ChangePointReport(
            signal="hourly_mean_bg",
            method="cusum",
            threshold_sigma=threshold_sigma,
            days=days,
            sample_count=len(values_only),
            change_points=change_points,
            profile_change_events=profile_changes,
        )

    @mcp.tool()
    async def change_points_tdd(days: int = 30, threshold_sigma: float = 3.0) -> ChangePointReport:
        """Detect change-points in daily total daily dose (TDD) via CUSUM.

        TDD shifts can signal puberty/growth, illness, pump-site issues,
        Autosens drift. Default sigma 3.0 (more sensitive than BG) because
        daily TDD has smaller sample count and tighter SD.

        Args:
            days: analysis window (max 90, default 30).
            threshold_sigma: detection sensitivity.
        """
        client = get_client()
        days = max(14, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        treatments = await _fetch_treatments_between(client, start, end)

        # Daily TDD: insulin sum per local date
        by_day: dict[str, float] = {}
        for t in treatments:
            if not t.insulin or t.insulin <= 0:
                continue
            try:
                ts = parse_iso_to_utc(t.created_at)
            except Exception:
                continue
            key = ts.strftime("%Y-%m-%d")
            by_day[key] = by_day.get(key, 0.0) + float(t.insulin)
        days_sorted = sorted(by_day.keys())
        values = [by_day[d] for d in days_sorted]

        raw_points = M.cusum_change_points(values, threshold_sigma=threshold_sigma)
        change_points: list[ChangePoint] = []
        for p in raw_points:
            idx = int(p["index"])
            if 0 <= idx < len(days_sorted):
                change_points.append(
                    ChangePoint(
                        timestamp_iso=days_sorted[idx] + "T00:00:00Z",
                        index=idx,
                        direction="up" if p["direction"] > 0 else "down",
                        magnitude=p["magnitude"],
                        cumsum=p["cumsum"],
                    )
                )

        profile_changes = [t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at]
        return ChangePointReport(
            signal="daily_tdd",
            method="cusum",
            threshold_sigma=threshold_sigma,
            days=days,
            sample_count=len(values),
            change_points=change_points,
            profile_change_events=profile_changes,
        )

    # ------------------------------------------------------------------------
    # PR #6 — DIA fitter (exploratory)
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def dia_fit_estimate(days: int = 30) -> DiaFitResult:
        """Exploratory: fit the AAPS exponential IOB curve to observed bolus residuals.

        Grid-searches DIA in {3.0, 3.5, ..., 8.0} hours and peak in {40, 45,
        ..., 80} minutes to find the (DIA, peak) pair that best explains the
        relationship between time-since-bolus and observed remaining-BG-effect.

        **Research / exploratory only.** The observation model is approximate
        and confounded by meals, sensor noise, COB. Treat output as discussion
        substrate, NOT a clinical recommendation. Per AAPS Objectives, all DIA
        changes should be reviewed with the care team.

        Args:
            days: analysis window (max 90, default 30).
        """
        client = get_client()
        days = max(7, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        treatments = await _fetch_treatments_between(client, start, end)
        _, _, profile_dia_hours, _ = await _extract_profile_settings(client)

        # Build observations: for each isolated correction bolus, sample the
        # remaining-effect ratio at t = 30/60/120/180/240/300 min post-bolus
        observations = _build_iob_observations(sgvs, treatments)
        fit = M.fit_dia_to_residuals(observations)

        if fit["sample_count"] < 30:
            recommendation = (
                "Not enough isolated correction events (<30) to estimate DIA reliably. "
                "Re-run after accumulating more data, ideally 30+ correction boluses with "
                "no carbs ±60 min."
            )
        else:
            diff = fit["best_dia_hours"] - profile_dia_hours
            if abs(diff) < 1.0:
                recommendation = (
                    f"Fitted DIA {fit['best_dia_hours']:.1f}h is within ~1h of profile "
                    f"DIA {profile_dia_hours:.1f}h. No strong evidence for change."
                )
            else:
                direction = "shorter" if diff < 0 else "longer"
                recommendation = (
                    f"Fitted DIA {fit['best_dia_hours']:.1f}h is meaningfully "
                    f"{direction} than profile DIA {profile_dia_hours:.1f}h. "
                    f"Discuss with care team before adjusting (AAPS hard floor 5h). "
                    f"Fiasp PK literature supports 5-7h (Heise/Hövelmann 2017)."
                )

        return DiaFitResult(
            sample_count=int(fit["sample_count"]),
            best_dia_hours=fit["best_dia_hours"],
            best_peak_min=fit["best_peak_min"],
            rmse=fit["rmse"],
            profile_dia_hours=profile_dia_hours,
            recommendation_text=recommendation,
            caveat_text=(
                "Exploratory research tool. Observation model approximates per-bolus "
                "remaining-effect from BG behaviour and is susceptible to confounding "
                "(unannounced carbs, sensor noise, COB carryover). NOT a clinical "
                "recommendation; review with diabetes care team before any setting change."
            ),
        )

    # ------------------------------------------------------------------------
    # PR #5 — Clinic packet composite report
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def clinic_packet(days: int = 30, timezone: str | None = None) -> ClinicPacket:
        """30-day clinic-ready composite report (markdown).

        Pulls TIR + GRI + LBGI/HBGI + per-meal-period TIR + variability metrics
        + change-points, renders to a single markdown document suitable for
        sharing at the next endo visit. Headline findings flagged at the top.
        """
        client = get_client()
        days = max(7, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        tz_name = timezone or await _resolve_timezone(client)
        tz_offset_hours = _tz_offset_for(tz_name)

        sgvs = await _fetch_sgvs_between(client, start, end)
        treatments = await _fetch_treatments_between(client, start, end)
        values = M._sgv_to_mgdl_list(sgvs)
        pairs = M._sgv_to_pairs(sgvs)
        n = len(values)

        # Headline metrics
        gri_data = M.gri(values)
        lbgi, hbgi = M.lbgi_hbgi(values)
        cv = M.cv_percent(values)
        mean_bg = sum(values) / n if n else 0.0
        gmi = M.gmi_percent(mean_bg)
        tir = sum(1 for v in values if 70 <= v <= 180) / n * 100 if n else 0.0
        tbr_54 = sum(1 for v in values if v < 54) / n * 100 if n else 0.0
        tbr_70 = sum(1 for v in values if v < 70) / n * 100 if n else 0.0
        tar_180 = sum(1 for v in values if v > 180) / n * 100 if n else 0.0
        tar_250 = sum(1 for v in values if v > 250) / n * 100 if n else 0.0

        # Per-meal-period
        partitions = M.partition_by_local_hour(pairs, tz_offset_hours=tz_offset_hours)
        period_lines = []
        for name, vals in partitions.items():
            n_p = len(vals)
            tir_p = sum(1 for v in vals if 70 <= v <= 180) / n_p * 100 if n_p else 0.0
            tbr_p = sum(1 for v in vals if v < 70) / n_p * 100 if n_p else 0.0
            mean_p = sum(vals) / n_p if n_p else 0.0
            period_lines.append(f"| {name:<10} | {n_p:6d} | {mean_p:6.1f} | {tir_p:6.1f}% | {tbr_p:6.1f}% |")

        # Change-points
        hourly = M.hourly_aggregate(pairs)
        hourly_vals = [v for _, v in hourly]
        cps = M.cusum_change_points(hourly_vals, threshold_sigma=4.0)
        cp_count = len(cps)

        # Profile changes in window
        profile_changes = [t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at]

        # Data sufficiency — flag before any consensus-branded metric is trusted.
        suff = M.data_sufficiency(sgvs, days)

        # Time in Tight Range (70-140) — increasingly requested at pediatric visits.
        titr = sum(1 for v in values if 70 <= v <= 140) / n * 100 if n else 0.0

        # Insulin & carb summary (bolus + carbs are what treatments record reliably;
        # basal delivery isn't a discrete treatment, so this is labelled as bolus,
        # not full TDD, to avoid over-claiming).
        bolus_txs = [t for t in treatments if t.insulin and t.insulin > 0]
        carb_txs = [t for t in treatments if t.carbs and t.carbs > 0]
        total_bolus_u = sum(t.insulin for t in bolus_txs if t.insulin)
        total_carbs_g = sum(t.carbs for t in carb_txs if t.carbs)
        mean_daily_bolus = total_bolus_u / days
        mean_daily_carbs = total_carbs_g / days
        boluses_per_day = len(bolus_txs) / days

        # Consensus hypo-event counts (Battelino 2019 / ATTD >=15-min events).
        hypo = _hypo_episodes(days, sgvs, treatments, tz_offset_hours=tz_offset_hours)

        # AGP percentile ribbon (median + IQR band) embedded directly.
        agp_hourly = M.agp_hourly_percentiles(pairs, tz_offset_hours=tz_offset_hours)
        agp_lines: list[str] = []
        for h in agp_hourly:
            hn = int(h.get("sample_count", 0))
            p25 = h.get("p25", 0.0)
            p50 = h.get("p50", 0.0)
            p75 = h.get("p75", 0.0)
            iqr_start = int(min(40, max(0, p25 / 10)))
            iqr_end = int(min(40, max(0, p75 / 10)))
            bar = " " * iqr_start + "=" * max(1, iqr_end - iqr_start) + " " * max(0, 40 - iqr_end)
            p50_pos = int(min(39, max(0, p50 / 10)))
            bar = bar[:p50_pos] + "*" + bar[p50_pos + 1 :]
            agp_lines.append(f"| {int(h['hour']):02d} | {hn:4d} | {p50:5.0f} | `{bar}` |")

        # Per-day thumbnails (local calendar day) — what a clinician scans for
        # overnight lows. A day whose nadir is <54 is flagged.
        by_day: dict[str, list[float]] = {}
        for ts, v in pairs:
            key = (ts + timedelta(hours=tz_offset_hours)).strftime("%Y-%m-%d")
            by_day.setdefault(key, []).append(v)
        day_lines: list[str] = []
        for day in sorted(by_day):
            dv = by_day[day]
            dmean = sum(dv) / len(dv)
            dtir = sum(1 for x in dv if 70 <= x <= 180) / len(dv) * 100
            dmin = min(dv)
            flag = " ⚠️<54" if dmin < 54 else (" ⚠️<70" if dmin < 70 else "")
            day_lines.append(f"| {day} | {len(dv):4d} | {dmean:5.0f} | {dtir:5.0f}% | {dmin:4.0f}{flag} |")

        # Headline findings (priorities)
        findings: list[str] = []
        if not suff["meets_agp_consensus"]:
            findings.append(f"**Data sufficiency:** {suff['note']}")
        if tbr_54 > 1.0:
            findings.append(
                f"**TBR<54 = {tbr_54:.2f}%** exceeds the ISPAD 2022 / Battelino 2019 "
                f"consensus target of <1%. Highest clinical-safety priority."
            )
        lbgi_band_name = M.lbgi_band(lbgi)
        if lbgi_band_name in ("moderate", "high", "very_high"):
            findings.append(
                f"**LBGI {lbgi:.2f}** is in the *{lbgi_band_name}* hypoglycemia-risk band (Kovatchev 1998)."
            )
        if cv > M.CV_TARGET_PERCENT:
            findings.append(f"**CV {cv:.1f}%** exceeds the consensus target of <36% (Battelino 2019).")
        if tir < 70:
            findings.append(f"**TIR {tir:.1f}%** is below the ADA pediatric T1D target of >70%.")
        if not findings:
            findings.append("No headline thresholds exceeded — current settings appear well-tuned.")

        # Build markdown body
        body_parts = [
            f"# Clinic packet — {days}-day report",
            "",
            f"- Window: `{start.date()}` -> `{end.date()}`",
            (
                f"- CGM coverage: **{suff['days_with_data']}** days with data, "
                f"**{suff['pct_active']}%** active ({n} readings); "
                f"longest gap {suff['longest_gap_hours']}h"
            ),
            f"- Generated: `{datetime.now(UTC).isoformat()}`",
            "",
            "## Headline findings",
            "",
        ]
        for i, f in enumerate(findings, 1):
            body_parts.append(f"{i}. {f}")
        body_parts.extend(
            [
                "",
                "## Glycemic summary",
                "",
                "| Metric | Value | Target |",
                "|---|---|---|",
                f"| TIR (70-180) | **{tir:.1f}%** | >70% (pediatric ADA) |",
                f"| TITR (70-140) | {titr:.1f}% | (tight-range reference) |",
                f"| TBR<70 | {tbr_70:.2f}% | <4% |",
                f"| TBR<54 | **{tbr_54:.2f}%** | <1% |",
                f"| TAR>180 | {tar_180:.1f}% | <25% |",
                f"| TAR>250 | {tar_250:.1f}% | <5% |",
                f"| Mean BG | {mean_bg:.1f} mg/dL ({mgdl_to_mmol(mean_bg):.1f} mmol/L) | — |",
                f"| GMI | {gmi:.2f}% | <7% (individualize) |",
                f"| CV | {cv:.1f}% | <36% |",
                "",
                "## Hypoglycemia events (consensus ≥15-min)",
                "",
                (
                    f"- **{hypo.total_episodes}** events "
                    f"({hypo.episodes_per_week}/week): "
                    f"**{hypo.level2_episodes}** level-2 (<54), "
                    f"{hypo.level1_episodes} level-1 (54-69), "
                    f"{hypo.nocturnal_episodes} nocturnal"
                ),
                (
                    f"- Mean duration {hypo.mean_duration_minutes} min; "
                    f"{hypo.episodes_with_rescue_carbs} had a rescue carb logged"
                ),
                "",
                "## Insulin & carbohydrate summary",
                "",
                f"- Mean daily **bolus** insulin: **{mean_daily_bolus:.1f} U** "
                f"(basal not included — not a discrete treatment record)",
                f"- Mean daily carbs: **{mean_daily_carbs:.0f} g** over {boluses_per_day:.1f} boluses/day",
                "",
                "## Ambulatory Glucose Profile (median + IQR band by hour)",
                "",
                "| Hour | n | p50 | Visual (p25-p75 IQR, 0-400 mg/dL) |",
                "|---|---|---|---|",
                *agp_lines,
                "",
                "`=` = IQR (p25-p75), `*` = median (p50); 10 mg/dL per char.",
                "",
                "## Daily profiles (local calendar day)",
                "",
                "| Date | n | Mean | TIR | Nadir |",
                "|---|---|---|---|---|",
                *day_lines,
                "",
                "## Risk indices",
                "",
                (
                    f"- **GRI**: {gri_data['gri']:.1f} "
                    f"(Hypo {gri_data['gri_hypo']:.1f} + "
                    f"Hyper {gri_data['gri_hyper']:.1f})"
                ),
                f"- **LBGI**: {lbgi:.2f} *({M.lbgi_band(lbgi)})*",
                f"- **HBGI**: {hbgi:.2f} *({M.hbgi_band(hbgi)})*",
                "",
                "## Per-meal-period TIR",
                "",
                "| Period | Samples | Mean | TIR | TBR<70 |",
                "|---|---|---|---|---|",
                *period_lines,
                "",
                "## Change-points & profile changes",
                "",
                f"- Detected change-points in hourly mean BG: **{cp_count}**",
                f"- Profile switches in window: **{len(profile_changes)}**",
                "",
                "## Notes for the care team",
                "",
                "- Data sourced from Nightscout + AAPS via nightscout-mcp",
                "- All metrics computed locally; no PHI transmitted to third-party services",
                "- This packet is a discussion aid; clinical decisions remain with the care team",
            ]
        )
        markdown_body = "\n".join(body_parts)

        return ClinicPacket(
            days=days,
            generated_at=datetime.now(UTC).isoformat(),
            period_start_iso=start.isoformat(),
            period_end_iso=end.isoformat(),
            markdown_body=markdown_body,
            headline_findings=findings,
            data_sufficiency=DataSufficiency(**suff),
        )

    # ------------------------------------------------------------------------
    # Section C — Composition tools on top of the metrics suite
    # ------------------------------------------------------------------------

    @mcp.tool()
    async def dynisf_adjustment_recommender(days: int = 14) -> DynIsfRecommendation:
        """Recommend a Dynamic ISF Adjustment Factor based on observed bolus residuals.

        Reads the same data as bolus_event_residuals and applies the decision
        tree from the 2026-05-24 deep research report (Topic 7 / Key Finding #1):

          - Overall ratio within +/-15% of 1.0  -> HOLD current AF
          - Ratio > 1.15 with monotone-with-BG (over_250 > 180_250 > 140_180 ≈ 1.0)
              -> "BG-curve dampening" signature; lower AF further OR raise target.
          - Ratio > 1.15 with flat-across-bands -> uniform over-aggression; lower AF.
          - Ratio < 0.85 -> raise AF.
          - Sample count < 20 -> insufficient data.

        Returns the current AAPS DynISFAdjust, a recommended value (or None for
        hold), a confidence band, and reasoning text suitable for sharing with
        the care team.

        **Discussion aid only — clinical changes must be reviewed with the
        diabetes care team.** AAPS Objectives discipline applies: change one
        lever at a time, hold 7-14 days, re-evaluate.
        """
        client = get_client()
        days = max(7, min(days, 90))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        treatments = await _fetch_treatments_between(client, start, end)
        devicestatus = await _fetch_devicestatus_between(client, start, end)
        _, _, dia_hours, _ = await _extract_profile_settings(client)
        current_af = await _read_current_dynisf_adjust(client)

        events = _build_bolus_events(sgvs, treatments, devicestatus, dia_hours)
        by_bg_band = _aggregate(events, lambda e: e.bg_band)
        with_isf = [e for e in events if e.realized_isf_mgdl_per_u and e.aaps_effective_isf_mgdl_per_u]
        n = len(with_isf)
        if n > 0:
            ratios = [e.realized_isf_mgdl_per_u / e.aaps_effective_isf_mgdl_per_u for e in with_isf]
            overall = sum(ratios) / len(ratios)
        else:
            overall = 0.0

        def _band_ratio(*names: str) -> float | None:
            band = next((b for b in by_bg_band if b.band_name in names), None)
            return band.isf_ratio_realized_vs_effective if band and band.sample_count >= 3 else None

        in_target_ratio = _band_ratio("100_140", "140_180")
        above_target_ratio = _band_ratio("180_250", "over_250")

        # Decision tree
        if n < 20:
            recommendation_type = "insufficient_data"
            recommended_af = None
            confidence = "low"
            reasoning = (
                f"Only {n} bolus events matched with AAPS effective ISF in the {days}-day "
                f"window. Need ≥20 for a meaningful recommendation. Accumulate more data."
            )
        elif 0.85 <= overall <= 1.15:
            recommendation_type = "hold"
            recommended_af = current_af
            confidence = "high" if n >= 30 else "medium"
            reasoning = (
                f"Overall ratio {overall:.2f} is within +/-15% of 1.0 across {n} events. "
                f"AAPS Dynamic ISF is well-calibrated against realized outcomes. "
                f"Hold current AF={current_af}."
            )
        elif overall > 1.15:
            # Detect curve-dampening signature
            curve_signature = (
                above_target_ratio is not None
                and above_target_ratio > 1.3
                and in_target_ratio is not None
                and 0.85 <= in_target_ratio <= 1.25
            )
            if curve_signature:
                recommendation_type = "dampen_curve"
                # Recommend a SLIGHT additional AF reduction; if already low, prefer target adjustment
                if current_af <= 25:
                    recommended_af = current_af  # signal "consider target raise instead"
                    reasoning = (
                        f"BG-curve dampening signature detected: in-target ratio "
                        f"{in_target_ratio:.2f} (~1.0), above-target ratio "
                        f"{above_target_ratio:.2f} (>1.3). Current AF={current_af} is "
                        f"already low; further reduction risks under-dosing in-target. "
                        f"Consider a small TARGET RAISE (e.g. +0.3 mmol/L) or longer DIA "
                        f"reduction trial INSTEAD of further AF lowering. Discuss with care team."
                    )
                else:
                    recommended_af = max(20, current_af - 5)
                    reasoning = (
                        f"BG-curve dampening signature detected: in-target ratio "
                        f"{in_target_ratio:.2f} (~1.0), above-target ratio "
                        f"{above_target_ratio:.2f} (>1.3). AAPS is over-dosing at high BG "
                        f"specifically. Trial AF={recommended_af} (was {current_af}) for "
                        f"7-14 days; expect above-target ratio to move toward 1.0 while "
                        f"in-target ratio stays close to 1.0."
                    )
                confidence = "high" if n >= 40 else "medium"
            else:
                recommendation_type = "lower_af"
                recommended_af = max(20, current_af - 5)
                confidence = "medium"
                reasoning = (
                    f"Overall ratio {overall:.2f} > 1.15 across {n} events, with flatter "
                    f"pattern across bands (in-target {in_target_ratio}, above-target "
                    f"{above_target_ratio}). Consider lowering AF from {current_af} to "
                    f"{recommended_af}. Re-evaluate after 7-14 days."
                )
        else:  # overall < 0.85
            recommendation_type = "raise_af"
            recommended_af = min(100, current_af + 5)
            confidence = "medium"
            reasoning = (
                f"Overall ratio {overall:.2f} < 0.85 across {n} events — AAPS appears to be "
                f"UNDER-dosing (realized drop smaller than predicted). Consider raising AF "
                f"from {current_af} to {recommended_af}. Re-evaluate after 7-14 days."
            )

        return DynIsfRecommendation(
            current_af=current_af,
            recommended_af=recommended_af,
            recommendation_type=recommendation_type,
            overall_ratio=round(overall, 3),
            in_target_ratio=round(in_target_ratio, 3) if in_target_ratio else None,
            above_target_ratio=round(above_target_ratio, 3) if above_target_ratio else None,
            confidence=confidence,
            sample_count=n,
            reasoning=reasoning,
            caveat_text=(
                "Discussion aid only. Do not change AAPS settings without consulting the "
                "diabetes care team. AAPS Objectives recommends one lever at a time, hold "
                "7-14 days, re-evaluate. The AAPS DynISFAdjust is stored in the encrypted "
                "settings export, not in Nightscout; current_af here is read from the AAPS "
                "Drive export if available, else defaults to the cysSETTINGS-tracked value."
            ),
        )

    @mcp.tool()
    async def consensus_target_audit(days: int = 14, population: str = "standard") -> ConsensusTargetAudit:
        """One-shot audit of metrics vs. published consensus targets.

        Compares the patient's TIR, TBR<54, TAR>180, CV, GRI, LBGI, HBGI, and
        GMI against the Battelino 2019, ISPAD 2022, Klonoff 2023, and Kovatchev
        thresholds. Returns a per-metric pass/fail table + summary.

        The TIR range and TBR/TAR limits are population-specific:
          - `standard` (default): T1D/T2D incl. children/adolescents —
            TIR 70-180 >70%, TBR<70 <4%, TBR<54 <1%.
          - `older_high_risk`: TIR 70-180 >50%, TBR<70 <1% (hypo-avoidant).
          - `pregnancy_t1d`: TIR **63-140** >70%, TBR<63 <4%, TBR<54 <1%.
        Risk indices (GRI/LBGI/HBGI/CV/GMI) are population-independent.

        Useful for "in 30 seconds, which of my numbers are off-target?"
        """
        client = get_client()
        days = max(7, min(days, 90))
        profile = TARGET_POPULATIONS.get(population) or TARGET_POPULATIONS["standard"]
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        values = M._sgv_to_mgdl_list(sgvs)
        M._sgv_to_pairs(sgvs)
        n = len(values)

        if n == 0:
            return ConsensusTargetAudit(
                days=days,
                checks=[],
                summary_pass_count=0,
                summary_fail_count=0,
                headline="No CGM data in window — cannot audit.",
            )

        # Compute metrics against the selected population's TIR range.
        tl, th = profile["tir_low"], profile["tir_high"]
        tbr_low_cut, tbr_vlow_cut = profile["tbr_low_cut"], profile["tbr_vlow_cut"]
        tar_vhigh_cut = profile["tar_vhigh_cut"]
        tir = sum(1 for v in values if tl <= v <= th) / n * 100
        tbr_vlow = sum(1 for v in values if v < tbr_vlow_cut) / n * 100
        tbr_low = sum(1 for v in values if v < tbr_low_cut) / n * 100
        tar_high = sum(1 for v in values if v > th) / n * 100
        tar_vhigh = sum(1 for v in values if v > tar_vhigh_cut) / n * 100 if tar_vhigh_cut is not None else None
        cv = M.cv_percent(values)
        mean_bg = sum(values) / n
        gmi = M.gmi_percent(mean_bg)
        gri_data = M.gri(values)
        lbgi, hbgi = M.lbgi_hbgi(values)

        checks: list[TargetCheck] = []

        # Helper
        def _make_check(
            name: str, value: float, target_desc: str, in_target: bool, direction: str, severity: str, citation: str
        ) -> TargetCheck:
            return TargetCheck(
                metric_name=name,
                metric_value=round(value, 3),
                target_description=target_desc,
                in_target=in_target,
                direction=direction,
                severity=severity,
                citation=citation,
            )

        pop_cite = profile["citation"]
        tir_min = profile["tir_min_pct"]
        # TIR for the population's range
        checks.append(
            _make_check(
                f"TIR ({tl}-{th} mg/dL)",
                tir,
                f">{tir_min:.0f}% ({profile['label']})",
                tir >= tir_min,
                "above_target" if tir >= tir_min else "below_target",
                "ok" if tir >= tir_min else ("borderline" if tir >= tir_min - 10 else "over"),
                pop_cite,
            )
        )
        # TBR very-low (<54): <1% for every population
        vlow_max = profile["tbr_vlow_max_pct"]
        checks.append(
            _make_check(
                f"TBR <{tbr_vlow_cut} mg/dL",
                tbr_vlow,
                f"<{vlow_max:.0f}% (international consensus)",
                tbr_vlow < vlow_max,
                "in_target" if tbr_vlow < vlow_max else "above_target",
                "ok" if tbr_vlow < vlow_max else ("borderline" if tbr_vlow < vlow_max + 1 else "severe"),
                pop_cite,
            )
        )
        # TBR low (<70 / <63 for pregnancy)
        low_max = profile["tbr_low_max_pct"]
        checks.append(
            _make_check(
                f"TBR <{tbr_low_cut} mg/dL",
                tbr_low,
                f"<{low_max:.0f}% ({profile['label']})",
                tbr_low < low_max,
                "in_target" if tbr_low < low_max else "above_target",
                "ok" if tbr_low < low_max else ("borderline" if tbr_low < low_max + 3 else "over"),
                pop_cite,
            )
        )
        # TAR high (>180 / >140 for pregnancy)
        high_max = profile["tar_high_max_pct"]
        checks.append(
            _make_check(
                f"TAR >{th} mg/dL",
                tar_high,
                f"<{high_max:.0f}% ({profile['label']})",
                tar_high < high_max,
                "in_target" if tar_high < high_max else "above_target",
                "ok" if tar_high < high_max else ("borderline" if tar_high < high_max + 15 else "over"),
                pop_cite,
            )
        )
        # TAR very-high (>250) — populations without a separate band skip this
        if tar_vhigh is not None and profile["tar_vhigh_max_pct"] is not None:
            vhigh_max = profile["tar_vhigh_max_pct"]
            checks.append(
                _make_check(
                    f"TAR >{tar_vhigh_cut} mg/dL",
                    tar_vhigh,
                    f"<{vhigh_max:.0f}% ({profile['label']})",
                    tar_vhigh < vhigh_max,
                    "in_target" if tar_vhigh < vhigh_max else "above_target",
                    "ok" if tar_vhigh < vhigh_max else ("borderline" if tar_vhigh < vhigh_max + 5 else "over"),
                    pop_cite,
                )
            )
        # CV (<36 percent)
        checks.append(
            _make_check(
                "CV",
                cv,
                "<36% (Battelino 2019)",
                cv < 36.0,
                "in_target" if cv < 36.0 else "above_target",
                "ok" if cv < 36.0 else ("borderline" if cv < 42.0 else "over"),
                "Battelino 2019; Monnier 2008",
            )
        )
        # GMI (no fixed target — surfaced for reference vs. HbA1c)
        checks.append(
            _make_check(
                "GMI",
                gmi,
                "<7% (target proxy)",
                gmi < 7.0,
                "in_target" if gmi < 7.0 else "above_target",
                "ok" if gmi < 7.0 else ("borderline" if gmi < 8.0 else "over"),
                "Bergenstal 2018 Diabetes Care 41:2275",
            )
        )
        # LBGI (<2.5 = low/moderate)
        checks.append(
            _make_check(
                "LBGI",
                lbgi,
                "<2.5 (low/moderate hypo risk)",
                lbgi < 2.5,
                "in_target" if lbgi < 2.5 else "above_target",
                "ok" if lbgi < 2.5 else ("borderline" if lbgi < 5.0 else "severe"),
                "Kovatchev 1998 Diabetes Care 21:1870",
            )
        )
        # HBGI (<9 = low/moderate)
        checks.append(
            _make_check(
                "HBGI",
                hbgi,
                "<9.0 (low/moderate hyper risk)",
                hbgi < 9.0,
                "in_target" if hbgi < 9.0 else "above_target",
                "ok" if hbgi < 9.0 else ("borderline" if hbgi < 15.0 else "severe"),
                "Kovatchev 1998 Diabetes Care 21:1870",
            )
        )
        # GRI (no formal target threshold; Klonoff suggests <50 = good control)
        checks.append(
            _make_check(
                "GRI",
                gri_data["gri"],
                "<50 (suggested good control)",
                gri_data["gri"] < 50.0,
                "in_target" if gri_data["gri"] < 50.0 else "above_target",
                "ok" if gri_data["gri"] < 50.0 else ("borderline" if gri_data["gri"] < 70.0 else "over"),
                "Klonoff 2023 JDST 17:1226",
            )
        )

        passed = sum(1 for c in checks if c.in_target)
        failed = sum(1 for c in checks if not c.in_target)
        worst = [c.metric_name for c in checks if c.severity == "severe"]
        pop_prefix = f"[{profile['label']}] "
        if not failed:
            headline = f"{pop_prefix}All consensus targets met. Current settings appear well-tuned."
        elif worst:
            headline = (
                f"{pop_prefix}{failed}/{len(checks)} consensus targets unmet. "
                f"Severe-band metrics requiring attention: {', '.join(worst)}."
            )
        else:
            headline = f"{pop_prefix}{failed}/{len(checks)} consensus targets unmet (none severe)."

        return ConsensusTargetAudit(
            days=days,
            checks=checks,
            summary_pass_count=passed,
            summary_fail_count=failed,
            headline=headline,
        )

    @mcp.tool()
    async def settings_change_attribution(days: int = 30, pre_post_days: int = 7) -> SettingChangeAttributionReport:
        """For each profile-switch event in the window, compute pre vs. post outcome shift.

        Methodology:
          1. Find all profile-switch events in the analysis window.
          2. For each event, compute TIR / TBR<54 / GRI for the `pre_post_days` window
             BEFORE and AFTER.
          3. Apply a binomial proportion test (Wilson-derived approx p-value) to
             pre/post TIR difference.
          4. Apply Benjamini-Hochberg FDR correction across all events (q=0.10) to
             control false discovery rate when scanning multiple changes.

        Useful for "which of my recent settings changes actually moved the needle?"
        without falling into the classic multiple-comparison trap.

        Args:
            days: total scan window (max 90, default 30)
            pre_post_days: window size on each side of each change (default 7)
        """
        client = get_client()
        days = max(14, min(days, 90))
        pre_post_days = max(3, min(pre_post_days, 14))
        end = _now_utc_midnight_plus_one()
        start = end - timedelta(days=days)
        treatments = await _fetch_treatments_between(client, start, end)

        # Find profile switch events
        switches: list[Treatment] = [t for t in treatments if t.event_type == "Profile Switch" and t.created_at]
        if not switches:
            return SettingChangeAttributionReport(
                window_days=days,
                change_window_pre_days=pre_post_days,
                change_window_post_days=pre_post_days,
                fdr_q=0.10,
                change_events=[],
            )

        # Fetch SGVs for the whole window (one shot)
        sgvs = await _fetch_sgvs_between(client, start, end)
        pairs = M._sgv_to_pairs(sgvs)

        # For each switch, slice the pre/post windows
        raw_events: list[tuple[Treatment, dict[str, Any]]] = []
        for sw in switches:
            try:
                sw_ts = parse_iso_to_utc(sw.created_at)
            except Exception:
                continue
            pre_start = sw_ts - timedelta(days=pre_post_days)
            post_end = sw_ts + timedelta(days=pre_post_days)
            pre_vals = [v for ts, v in pairs if pre_start <= ts < sw_ts]
            post_vals = [v for ts, v in pairs if sw_ts <= ts < post_end]
            if len(pre_vals) < 50 or len(post_vals) < 50:
                continue
            pre_metrics = _compute_change_window_metrics(pre_vals)
            post_metrics = _compute_change_window_metrics(post_vals)
            # Binomial proportion p (two-proportion z-test approximation)
            p = _two_proportion_p_value(
                int(pre_metrics["in_range_count"]),
                len(pre_vals),
                int(post_metrics["in_range_count"]),
                len(post_vals),
            )
            raw_events.append(
                (
                    sw,
                    {
                        "pre": pre_metrics,
                        "post": post_metrics,
                        "p_value": p,
                    },
                )
            )

        # FDR correction (Benjamini-Hochberg)
        p_values = [data["p_value"] for _, data in raw_events]
        fdr_q = 0.10
        fdr_corrected = _bh_correct(p_values, fdr_q)

        change_events: list[SettingChangeAttribution] = []
        for (sw, data), p_corr in zip(raw_events, fdr_corrected, strict=False):
            pre = data["pre"]
            post = data["post"]
            if p_corr < 0.10:
                sig_band = "significant"
            elif p_corr < 0.20:
                sig_band = "borderline"
            else:
                sig_band = "not_significant"
            change_events.append(
                SettingChangeAttribution(
                    change_timestamp_iso=sw.created_at,
                    profile_name=getattr(sw, "profile", None),
                    percentage=int(sw.percent) if sw.percent else None,
                    pre_window_days=pre_post_days,
                    post_window_days=pre_post_days,
                    pre_tir_pct=round(pre["tir_pct"], 2),
                    post_tir_pct=round(post["tir_pct"], 2),
                    delta_tir_pct=round(post["tir_pct"] - pre["tir_pct"], 2),
                    pre_tbr_lt54_pct=round(pre["tbr_lt54_pct"], 2),
                    post_tbr_lt54_pct=round(post["tbr_lt54_pct"], 2),
                    delta_tbr_lt54_pct=round(post["tbr_lt54_pct"] - pre["tbr_lt54_pct"], 2),
                    pre_gri=round(pre["gri"], 2),
                    post_gri=round(post["gri"], 2),
                    delta_gri=round(post["gri"] - pre["gri"], 2),
                    binomial_p_value_uncorrected=round(data["p_value"], 4),
                    binomial_p_value_fdr_corrected=round(p_corr, 4),
                    significance_band=sig_band,
                )
            )

        return SettingChangeAttributionReport(
            window_days=days,
            change_window_pre_days=pre_post_days,
            change_window_post_days=pre_post_days,
            fdr_q=fdr_q,
            change_events=change_events,
        )

    @mcp.tool()
    async def agp_markdown_render(days: int = 14, timezone: str | None = None) -> AgpMarkdownRender:
        """Render the AGP percentile bands as a markdown table with ASCII visualization.

        Pairs with `ambulatory_glucose_profile` (which returns raw data) — this
        tool produces a human-readable rendering suitable for pasting into a
        clinic note or shared with the care team.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        tz_offset_hours = _tz_offset_for(tz_name)
        start, end = _window_for_days(days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        pairs = M._sgv_to_pairs(sgvs)
        suff = M.data_sufficiency(sgvs, days)
        hourly = M.agp_hourly_percentiles(pairs, tz_offset_hours=tz_offset_hours)

        # Determine p50 min/max for the headline
        p50_values = [h["p50"] for h in hourly if h["sample_count"] > 0]
        if p50_values:
            p50_min = min(p50_values)
            p50_max = max(p50_values)
            p50_min_hour = next(int(h["hour"]) for h in hourly if h.get("p50") == p50_min)
            p50_max_hour = next(int(h["hour"]) for h in hourly if h.get("p50") == p50_max)
        else:
            p50_min = p50_max = 0.0
            p50_min_hour = p50_max_hour = 0

        # Build markdown table
        lines = [
            f"# Ambulatory Glucose Profile — {days}-day window",
            "",
        ]
        if not suff["meets_agp_consensus"]:
            lines.append(f"> ⚠️ **{suff['note']}**")
            lines.append("")
        lines += [
            f"- Timezone: `{tz_name or 'UTC'}`",
            f"- Median (p50) ranges from **{p50_min:.0f}** mg/dL (hour {p50_min_hour:02d}) "
            f"to **{p50_max:.0f}** mg/dL (hour {p50_max_hour:02d})",
            "",
            "| Hour | n | p05 | p25 | **p50** | p75 | p95 | Visual (p25-p75 IQR band) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        # ASCII bars — IQR (p25 to p75) on a 0-400 scale, 40 chars wide
        for h in hourly:
            n = int(h["sample_count"])
            p05 = h.get("p05", 0.0)
            p25 = h.get("p25", 0.0)
            p50 = h.get("p50", 0.0)
            p75 = h.get("p75", 0.0)
            p95 = h.get("p95", 0.0)
            # Scale: 0-400 mg/dL maps to 40 chars
            iqr_start = int(min(40, max(0, p25 / 10)))
            iqr_end = int(min(40, max(0, p75 / 10)))
            bar = " " * iqr_start + "=" * max(1, iqr_end - iqr_start) + " " * max(0, 40 - iqr_end)
            # Mark p50 position with `*` if visible
            p50_pos = int(min(40, max(0, p50 / 10)))
            bar = bar[:p50_pos] + "*" + bar[p50_pos + 1 :]
            lines.append(
                f"| {int(h['hour']):02d} | {n:4d} | {p05:5.0f} | {p25:5.0f} | "
                f"**{p50:5.0f}** | {p75:5.0f} | {p95:5.0f} | `{bar}` |"
            )
        lines.extend(
            [
                "",
                "Scale: each visual is 0-400 mg/dL across 40 chars (10 mg/dL per char). "
                "`=` = IQR (p25 to p75), `*` = median (p50).",
            ]
        )
        markdown_body = "\n".join(lines)

        return AgpMarkdownRender(
            days=max(1, min(days, 90)),
            timezone=tz_name,
            markdown_body=markdown_body,
            p50_min_mgdl=round(p50_min, 1),
            p50_max_mgdl=round(p50_max, 1),
            p50_min_hour=p50_min_hour,
            p50_max_hour=p50_max_hour,
            data_sufficiency=DataSufficiency(**suff),
        )

    @mcp.tool()
    async def time_period_compare(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
        period_a_label: str = "pre",
        period_b_label: str = "post",
        timezone: str | None = None,
    ) -> PeriodCompareReport:
        """Side-by-side TIR + GRI + LBGI + HBGI + CV + GMI comparison across two windows.

        Designed for outcome verification: "did the setting change I made on
        date X actually move the needle?" Reports both raw deltas AND whether
        the 95% confidence intervals on TIR + TBR<54 overlap (CI overlap = the
        change is NOT statistically distinguishable from noise).

        Args:
            period_a_start, period_a_end: YYYY-MM-DD format, e.g. "2026-05-09"
                inclusive start, exclusive end
            period_b_start, period_b_end: same for the "post" window
            period_a_label, period_b_label: human-readable labels (default
                "pre"/"post")
            timezone: Olson timezone name (e.g. "America/Toronto"). Auto-detect
                from profile if None.

        Both windows MUST be the same length for fair comparison.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        a_start = _parse_iso_date_in_tz(period_a_start, tz_name)
        a_end = _parse_iso_date_in_tz(period_a_end, tz_name)
        b_start = _parse_iso_date_in_tz(period_b_start, tz_name)
        b_end = _parse_iso_date_in_tz(period_b_end, tz_name)

        if (a_end - a_start) != (b_end - b_start):
            raise ValueError(
                f"time_period_compare requires equal-length windows; got A={a_end - a_start}, B={b_end - b_start}."
            )

        sgvs_a = await _fetch_sgvs_between(client, a_start, a_end)
        sgvs_b = await _fetch_sgvs_between(client, b_start, b_end)
        period_a = _build_period_metrics(period_a_label, a_start, a_end, M._sgv_to_mgdl_list(sgvs_a))
        period_b = _build_period_metrics(period_b_label, b_start, b_end, M._sgv_to_mgdl_list(sgvs_b))

        # Check CI overlap
        tir_overlap = _ci_overlap(period_a.tir_70_180_ci, period_b.tir_70_180_ci)
        tbr_overlap = _ci_overlap(period_a.tbr_lt54_ci, period_b.tbr_lt54_ci)

        delta_tir = period_b.tir_70_180_pct - period_a.tir_70_180_pct
        delta_tbr_54 = period_b.tbr_lt54_pct - period_a.tbr_lt54_pct
        delta_gri = period_b.gri - period_a.gri

        interp = _interpret_period_compare(delta_tir, delta_tbr_54, tir_overlap, tbr_overlap)

        return PeriodCompareReport(
            period_a=period_a,
            period_b=period_b,
            delta_tir_pct=round(delta_tir, 2),
            delta_tbr_lt54_pct=round(delta_tbr_54, 3),
            delta_gri=round(delta_gri, 2),
            delta_lbgi=round(period_b.lbgi - period_a.lbgi, 3),
            delta_hbgi=round(period_b.hbgi - period_a.hbgi, 3),
            delta_cv_percent=round(period_b.cv_percent - period_a.cv_percent, 2),
            tir_ci_overlap=tir_overlap,
            tbr_lt54_ci_overlap=tbr_overlap,
            interpretation=interp,
        )


# ---------------------------------------------------------------------------
# Helpers used by the tool implementations above
# ---------------------------------------------------------------------------


def _tz_offset_for(tz_name: str | None) -> float:
    """Return the UTC offset in hours for a timezone name. Falls back to 0."""
    if not tz_name:
        return 0.0
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return 0.0
        offset = datetime.now(tz).utcoffset()
        if offset is None:
            return 0.0
        return offset.total_seconds() / 3600.0
    except ImportError:
        return 0.0


def _build_bolus_events(
    sgvs: list[Sgv],
    treatments: list[Treatment],
    devicestatus: list[dict[str, Any]],
    dia_hours: float,
) -> list[BolusEvent]:
    """Construct BolusEvent records from raw fetched data."""
    # Pre-process SGVs
    sgv_pairs = M._sgv_to_pairs(sgvs)
    if not sgv_pairs:
        return []

    # Pre-process devicestatus into sorted timestamped entries
    ds_entries: list[tuple[datetime, dict[str, Any]]] = []
    for d in devicestatus:
        created = d.get("created_at")
        if not created:
            continue
        try:
            ts = parse_iso_to_utc(created)
        except Exception:
            continue
        ds_entries.append((ts, d))
    ds_entries.sort(key=lambda x: x[0])

    # Pre-process treatments: pull out boluses
    # IMPORTANT: filter must match the realized-ISF use case. Including SMBs
    # (loop-delivered micro-boluses, 0.05-0.4 U) catastrophically inflates the
    # realized-ISF computation because a 50 mg/dL post-SMB drift becomes
    # 1000 mg/dL/U "realized ISF". Require:
    #   1. event_type "Correction Bolus" or "Bolus" (excludes "Meal Bolus",
    #      "Snack Bolus" — those have known carbs we'd need to model)
    #   2. notes does NOT contain "SMB" (defensive; AAPS sometimes tags SMBs
    #      with eventType "Correction Bolus")
    #   3. insulin >= 0.3 U (excludes SMBs by size; real correction boluses
    #      for school-age kids are typically 0.5-3 U)
    bolus_treatments: list[Treatment] = []
    carb_treatments: list[Treatment] = []
    for t in treatments:
        if t.carbs and t.carbs > 0:
            carb_treatments.append(t)
        if not (t.insulin and t.insulin >= 0.3):
            continue
        if t.event_type not in ("Bolus", "Correction Bolus"):
            continue
        if t.notes and "SMB" in t.notes.upper():
            continue
        bolus_treatments.append(t)

    events: list[BolusEvent] = []
    dia_window_min = min(dia_hours, 5.0) * 60.0  # clamp per literature

    for bolus in bolus_treatments:
        try:
            bolus_ts = parse_iso_to_utc(bolus.created_at)
        except Exception:
            continue

        # Was there a carb entry within ±60 min? If so this is a meal bolus
        is_meal = False
        for c in carb_treatments:
            try:
                c_ts = parse_iso_to_utc(c.created_at)
            except Exception:
                continue
            if abs((c_ts - bolus_ts).total_seconds()) <= 60 * 60:
                is_meal = True
                break

        # Pre-bolus BG (closest reading ≤ bolus time)
        pre_bg = None
        for ts, v in reversed(sgv_pairs):
            if ts <= bolus_ts and (bolus_ts - ts).total_seconds() <= 15 * 60:
                pre_bg = v
                break
            if ts < bolus_ts - timedelta(minutes=20):
                break

        # Realized: min BG in [bolus_ts, bolus_ts + dia_window]
        realized_min = None
        for ts, v in sgv_pairs:
            if ts < bolus_ts:
                continue
            if (ts - bolus_ts).total_seconds() > dia_window_min * 60:
                break
            if realized_min is None or v < realized_min:
                realized_min = v

        # Realized drop + ISF
        drop = (pre_bg - realized_min) if (pre_bg and realized_min) else None
        realized_isf = (drop / bolus.insulin) if (drop and drop > 0) else None

        # Find prior devicestatus row within ±15min
        ds_match = None
        for ts, d in reversed(ds_entries):
            if ts <= bolus_ts and (bolus_ts - ts).total_seconds() <= 15 * 60:
                ds_match = d
                break
            if ts < bolus_ts - timedelta(minutes=20):
                break

        # Extract from devicestatus
        iob = None
        cob = None
        aaps_predicted = None
        aaps_eff_isf = None
        if ds_match:
            iob = _safe_get(ds_match, "openaps.iob.iob")
            suggested = _safe_get(ds_match, "openaps.suggested") or {}
            if isinstance(suggested, dict):
                cob = suggested.get("COB")
                aaps_predicted = suggested.get("eventualBG")
                aaps_eff_isf = suggested.get("variable_sens") or suggested.get("sens")

        events.append(
            BolusEvent(
                timestamp_iso=bolus.created_at,
                insulin_units=float(bolus.insulin),
                pre_bg_mgdl=pre_bg,
                pre_bg_mmol=mgdl_to_mmol(pre_bg) if pre_bg else None,
                iob_at_bolus=float(iob) if iob is not None else None,
                cob_at_bolus=float(cob) if cob is not None else None,
                aaps_predicted_eventual_bg_mgdl=float(aaps_predicted) if aaps_predicted is not None else None,
                aaps_effective_isf_mgdl_per_u=float(aaps_eff_isf) if aaps_eff_isf is not None else None,
                realized_5h_min_bg_mgdl=realized_min,
                realized_5h_drop_mgdl=drop,
                realized_isf_mgdl_per_u=realized_isf,
                meal_or_correction="meal" if is_meal else "correction",
                time_band=_time_band_label(bolus_ts),
                bg_band=_bg_band_label(pre_bg) if pre_bg else "unknown",
            )
        )
    return events


def _aggregate(events: list[BolusEvent], key_fn: Callable[[BolusEvent], str]) -> list[BolusBandAggregate]:
    """Aggregate bolus events into per-band statistics."""
    by_band: dict[str, list[BolusEvent]] = {}
    for e in events:
        by_band.setdefault(key_fn(e), []).append(e)
    aggs: list[BolusBandAggregate] = []
    for band, group in by_band.items():
        if not group:
            continue
        with_isf = [g for g in group if g.realized_isf_mgdl_per_u and g.aaps_effective_isf_mgdl_per_u]
        mean_insulin = sum(g.insulin_units for g in group) / len(group)
        pre_bgs = [g.pre_bg_mgdl for g in group if g.pre_bg_mgdl]
        mean_pre_bg = sum(pre_bgs) / len(pre_bgs) if pre_bgs else 0.0
        drops = [g.realized_5h_drop_mgdl for g in group if g.realized_5h_drop_mgdl]
        mean_drop = sum(drops) / len(drops) if drops else 0.0
        if with_isf:
            mean_eff = sum(g.aaps_effective_isf_mgdl_per_u for g in with_isf) / len(with_isf)
            mean_real = sum(g.realized_isf_mgdl_per_u for g in with_isf) / len(with_isf)
            ratio = mean_real / mean_eff if mean_eff else 0.0
        else:
            mean_eff = mean_real = 0.0
            ratio = 0.0
        aggs.append(
            BolusBandAggregate(
                band_name=band,
                sample_count=len(group),
                mean_insulin_units=round(mean_insulin, 3),
                mean_pre_bg_mgdl=round(mean_pre_bg, 1),
                mean_realized_drop_mgdl=round(mean_drop, 1),
                mean_aaps_effective_isf=round(mean_eff, 1),
                mean_realized_isf=round(mean_real, 1),
                isf_ratio_realized_vs_effective=round(ratio, 3),
            )
        )
    return aggs


def _interpret_isf_ratio(overall: float, by_bg_band: list[BolusBandAggregate]) -> str:
    """Map an overall ISF ratio + per-band breakdown to a short interpretation string."""
    if overall == 0:
        return "Not enough matched events to compute a meaningful ratio."
    # Get above-target vs in-target ratios for curve-vs-uniform distinction
    above = next((b for b in by_bg_band if b.band_name in ("180_250", "over_250")), None)
    in_target = next((b for b in by_bg_band if b.band_name in ("100_140", "140_180")), None)
    if 0.85 <= overall <= 1.15:
        return f"AAPS Dynamic ISF is well-calibrated (overall ratio {overall:.2f}, within +/-15% of 1.0)."
    if overall > 1.15:
        if (
            above
            and in_target
            and above.isf_ratio_realized_vs_effective > 1.3
            and 0.85 <= in_target.isf_ratio_realized_vs_effective <= 1.15
        ):
            return (
                f"AAPS over-dosing above target (above-target ratio "
                f"{above.isf_ratio_realized_vs_effective:.2f} vs in-target "
                f"{in_target.isf_ratio_realized_vs_effective:.2f}). This is the "
                f"BG-curve dampening signature, not uniform AF error. Consider "
                f"discussing DynISF BG-divisor with care team."
            )
        return (
            f"AAPS appears over-dosing (overall ratio {overall:.2f} > 1.15). "
            f"Discuss lowering DynISFAdjust with care team."
        )
    if overall < 0.85:
        return (
            f"AAPS appears under-dosing (overall ratio {overall:.2f} < 0.85). "
            f"Discuss raising DynISFAdjust with care team."
        )
    return f"Overall ratio {overall:.2f}; review per-band breakdown."


def _build_iob_observations(sgvs: list[Sgv], treatments: list[Treatment]) -> list[tuple[float, float, float]]:
    """Build (t_min, predicted_iob_fraction, observed_iob_fraction) tuples for fitting.

    For each isolated correction bolus, sample remaining-effect at multiple
    time-points post-bolus. observed_iob_fraction is approximated as the
    fraction of total BG drop yet to occur at time t (i.e., 1 - (drop_at_t / total_drop)).

    This is an APPROXIMATE inverse map of IOB → BG; it's confounded by
    meals, sensor noise, COB. Caller should treat results as exploratory.
    """
    sgv_pairs = M._sgv_to_pairs(sgvs)
    if not sgv_pairs:
        return []

    # Build carb-bearing-treatment-timestamp set for "isolated" filter
    carb_ts: list[datetime] = []
    for t in treatments:
        if t.carbs and t.carbs > 0:
            try:
                carb_ts.append(parse_iso_to_utc(t.created_at))
            except Exception:
                continue

    observations: list[tuple[float, float, float]] = []
    sample_times_min = [30, 60, 120, 180, 240, 300]

    for tx in treatments:
        if not tx.insulin or tx.insulin <= 0:
            continue
        if tx.event_type not in ("Correction Bolus", "Bolus"):
            continue
        try:
            bolus_ts = parse_iso_to_utc(tx.created_at)
        except Exception:
            continue
        # Isolated: no carbs within ±60min
        if any(abs((c - bolus_ts).total_seconds()) <= 60 * 60 for c in carb_ts):
            continue

        # Pre-BG
        pre_bg = None
        for ts, v in reversed(sgv_pairs):
            if ts <= bolus_ts and (bolus_ts - ts).total_seconds() <= 15 * 60:
                pre_bg = v
                break
            if ts < bolus_ts - timedelta(minutes=20):
                break
        if pre_bg is None:
            continue

        # Sample BG at each time
        bg_at_t: dict[int, float] = {}
        for sample_t in sample_times_min:
            target_ts = bolus_ts + timedelta(minutes=sample_t)
            closest = None
            closest_delta = float("inf")
            for ts, v in sgv_pairs:
                delta = abs((ts - target_ts).total_seconds())
                if delta < closest_delta and delta <= 7.5 * 60:
                    closest = v
                    closest_delta = delta
            if closest is not None:
                bg_at_t[sample_t] = closest

        # Need at least full range
        if 30 not in bg_at_t or 300 not in bg_at_t:
            continue
        total_drop = pre_bg - bg_at_t[300]
        if total_drop <= 5:
            continue  # noise floor

        for sample_t, bg in bg_at_t.items():
            drop_at_t = pre_bg - bg
            observed_remaining_frac = max(0.0, min(1.0, 1.0 - (drop_at_t / total_drop)))
            # Predicted is computed during the grid search; we just provide the
            # (t, observed) pair. The predicted column is unused at observation
            # build time — placeholder 0.0.
            observations.append((float(sample_t), 0.0, observed_remaining_frac))

    return observations


# ---------------------------------------------------------------------------
# Section C helpers
# ---------------------------------------------------------------------------


async def _read_current_dynisf_adjust(client: NightscoutClient) -> int:
    """Try to read the current DynISFAdjust value.

    AAPS does NOT publish this preference to Nightscout, so we can't read it
    from the REST API. Fallback: default to 100 (AAPS stock default). Per the
    cysSETTINGS audit trail the actual value can be 30 (the user's current
    setting as of 2026-05-23). For now we read from a settings cache or
    environment variable; absent that, return 100.

    Future: integrate with the Phase 2 settings-history pipeline once that
    ships.
    """
    import os

    try:
        return int(os.environ.get("AAPS_DYNISF_ADJUST", "100"))
    except (ValueError, TypeError):
        return 100


def _compute_change_window_metrics(values: list[float]) -> dict[str, float]:
    """Compute headline metrics for a pre/post window."""
    n = len(values)
    if n == 0:
        return {"tir_pct": 0.0, "tbr_lt54_pct": 0.0, "in_range_count": 0.0, "gri": 0.0}
    in_range = sum(1 for v in values if 70 <= v <= 180)
    below_54 = sum(1 for v in values if v < 54)
    gri_data = M.gri(values)
    return {
        "tir_pct": in_range / n * 100,
        "tbr_lt54_pct": below_54 / n * 100,
        "in_range_count": float(in_range),
        "gri": gri_data["gri"],
    }


def _two_proportion_p_value(s1: int, n1: int, s2: int, n2: int) -> float:
    """Two-proportion z-test approximation of the p-value.

    H0: p1 == p2. Returns approximate two-tailed p-value via the normal
    distribution. Pure-stdlib implementation of the standard formula.
    """
    if n1 == 0 or n2 == 0:
        return 1.0
    p1 = s1 / n1
    p2 = s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return 1.0
    z = abs(p1 - p2) / se
    # Two-tailed p from z using the standard normal CDF approximation
    p = 2 * (1 - _phi(abs(z)))
    return max(0.0, min(1.0, p))


def _phi(z: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun 26.2.17)."""
    if z < 0:
        return 1 - _phi(-z)
    # Constants
    b1 = 0.31938153
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429
    t = 1.0 / (1.0 + 0.2316419 * z)
    poly = b1 * t + b2 * t**2 + b3 * t**3 + b4 * t**4 + b5 * t**5
    pdf = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    return 1 - pdf * poly


def _bh_correct(p_values: list[float], q: float) -> list[float]:
    """Benjamini-Hochberg FDR correction.

    Returns adjusted p-values such that controlling them at q controls FDR
    at q. Same order as input.
    """
    n = len(p_values)
    if n == 0:
        return []
    # Sort with original indices
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * n
    # Standard BH adjusted p: min(p[k]*n/k, prev_adjusted)
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        original_idx, p = indexed[rank]
        adj = p * n / (rank + 1)
        adj = min(adj, prev, 1.0)
        adjusted[original_idx] = adj
        prev = adj
    return adjusted


def _build_period_metrics(
    label: str,
    start: datetime,
    end: datetime,
    values: list[float],
) -> PeriodMetrics:  # noqa: F821 - forward reference resolved by importer
    """Build a PeriodMetrics record from a list of mg/dL values + window bounds."""
    # Local import to avoid circular reference at module-load
    from ..models import PeriodMetrics as _PM

    n = len(values)
    if n == 0:
        return _PM(
            label=label,
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
            sample_count=0,
            mean_mgdl=0.0,
            mean_mmol=0.0,
            tir_70_180_pct=0.0,
            tir_70_180_ci=(0.0, 0.0),
            tbr_lt54_pct=0.0,
            tbr_lt54_ci=(0.0, 0.0),
            tbr_lt70_pct=0.0,
            tar_gt180_pct=0.0,
            gri=0.0,
            lbgi=0.0,
            hbgi=0.0,
            cv_percent=0.0,
            gmi_percent=0.0,
        )
    in_range = sum(1 for v in values if 70 <= v <= 180)
    below_54 = sum(1 for v in values if v < 54)
    below_70 = sum(1 for v in values if v < 70)
    above_180 = sum(1 for v in values if v > 180)
    mean_bg = sum(values) / n
    cv = M.cv_percent(values)
    gri_data = M.gri(values)
    lbgi, hbgi = M.lbgi_hbgi(values)
    return _PM(
        label=label,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        sample_count=n,
        mean_mgdl=round(mean_bg, 1),
        mean_mmol=mgdl_to_mmol(mean_bg),
        tir_70_180_pct=round(in_range / n * 100, 2),
        tir_70_180_ci=M.wilson_ci_95(in_range, n),
        tbr_lt54_pct=round(below_54 / n * 100, 3),
        tbr_lt54_ci=M.wilson_ci_95(below_54, n),
        tbr_lt70_pct=round(below_70 / n * 100, 2),
        tar_gt180_pct=round(above_180 / n * 100, 2),
        gri=round(gri_data["gri"], 2),
        lbgi=round(lbgi, 3),
        hbgi=round(hbgi, 3),
        cv_percent=cv,
        gmi_percent=M.gmi_percent(mean_bg),
    )


def _ci_overlap(ci_a: tuple[float, float], ci_b: tuple[float, float]) -> bool:
    """Check whether two 95% CIs overlap."""
    return not (ci_a[1] < ci_b[0] or ci_b[1] < ci_a[0])


def _interpret_period_compare(
    delta_tir: float,
    delta_tbr_54: float,
    tir_overlap: bool,
    tbr_overlap: bool,
) -> str:
    """Map a pre/post delta + CI-overlap status to an interpretation string."""
    parts = []
    if abs(delta_tir) < 1.0:
        parts.append("TIR change is negligible (<1 percentage point).")
    elif delta_tir > 0:
        parts.append(f"TIR improved by {delta_tir:+.1f} pp.")
        if tir_overlap:
            parts.append("CIs overlap — change not yet statistically distinguishable from noise.")
        else:
            parts.append("CIs do not overlap — change is statistically meaningful.")
    else:
        parts.append(f"TIR worsened by {delta_tir:+.1f} pp.")
        if tir_overlap:
            parts.append("CIs overlap — within noise range.")
        else:
            parts.append("CIs do not overlap — change is statistically meaningful.")

    if abs(delta_tbr_54) < 0.1:
        parts.append("TBR<54 essentially unchanged.")
    elif delta_tbr_54 > 0:
        parts.append(f"TBR<54 increased by {delta_tbr_54:+.2f} pp (worsening hypo exposure).")
        if not tbr_overlap:
            parts.append("Hypo CIs do not overlap — increase is meaningful.")
    else:
        parts.append(f"TBR<54 decreased by {delta_tbr_54:+.2f} pp (improvement).")
        if not tbr_overlap:
            parts.append("Hypo CIs do not overlap — improvement is meaningful.")
    return " ".join(parts)


def _parse_iso_date_in_tz(date_str: str, tz_name: str | None) -> datetime:
    """Parse YYYY-MM-DD as local-midnight in the given timezone, return UTC datetime."""
    if not tz_name:
        return datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    except ImportError:
        return datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    naive = datetime.fromisoformat(date_str)
    local_midnight = naive.replace(tzinfo=tz)
    return local_midnight.astimezone(UTC)
