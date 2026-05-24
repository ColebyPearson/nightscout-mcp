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

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import metrics as M
from ..client import NightscoutClient
from ..models import (
    AgpHourPoint,
    AmbulatoryGlucoseProfile,
    BgRiskIndices,
    BolusBandAggregate,
    BolusEvent,
    BolusEventResidualsReport,
    ChangePoint,
    ChangePointReport,
    ClinicPacket,
    DiaFitResult,
    GlucoseVariability,
    GlycemiaRiskIndex,
    MealPeriodReport,
    MealPeriodTir,
    Sgv,
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
        tir_70_180 = (
            sum(1 for v in values if 70 <= v <= 180) / len(values) * 100 if values else 0.0
        )
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
    async def ambulatory_glucose_profile(
        days: int = 14, timezone: str | None = None
    ) -> AmbulatoryGlucoseProfile:
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
        with_both = [
            e
            for e in events
            if e.realized_isf_mgdl_per_u and e.aaps_effective_isf_mgdl_per_u
        ]
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
    async def change_points_bg(
        days: int = 30, threshold_sigma: float = 4.0
    ) -> ChangePointReport:
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

        profile_changes = [
            t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at
        ]
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
    async def change_points_tdd(
        days: int = 30, threshold_sigma: float = 3.0
    ) -> ChangePointReport:
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

        profile_changes = [
            t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at
        ]
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
            period_lines.append(
                f"| {name:<10} | {n_p:6d} | {mean_p:6.1f} | {tir_p:6.1f}% | {tbr_p:6.1f}% |"
            )

        # Change-points
        hourly = M.hourly_aggregate(pairs)
        hourly_vals = [v for _, v in hourly]
        cps = M.cusum_change_points(hourly_vals, threshold_sigma=4.0)
        cp_count = len(cps)

        # Profile changes in window
        profile_changes = [
            t.created_at for t in treatments if t.event_type == "Profile Switch" and t.created_at
        ]

        # Headline findings (priorities)
        findings: list[str] = []
        if tbr_54 > 1.0:
            findings.append(
                f"**TBR<54 = {tbr_54:.2f}%** exceeds the ISPAD 2022 / Battelino 2019 "
                f"consensus target of <1%. Highest clinical-safety priority."
            )
        lbgi_band_name = M.lbgi_band(lbgi)
        if lbgi_band_name in ("moderate", "high", "very_high"):
            findings.append(
                f"**LBGI {lbgi:.2f}** is in the *{lbgi_band_name}* hypoglycemia-risk band "
                f"(Kovatchev 1998)."
            )
        if cv > M.CV_TARGET_PERCENT:
            findings.append(
                f"**CV {cv:.1f}%** exceeds the consensus target of <36% (Battelino 2019)."
            )
        if tir < 70:
            findings.append(
                f"**TIR {tir:.1f}%** is below the ADA pediatric T1D target of >70%."
            )
        if not findings:
            findings.append("No headline thresholds exceeded — current settings appear well-tuned.")

        # Build markdown body
        body_parts = [
            f"# Clinic packet — {days}-day report",
            "",
            f"- Window: `{start.date()}` -> `{end.date()}`",
            f"- Total CGM readings: **{n}**",
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
                f"| TBR<70 | {tbr_70:.2f}% | <4% |",
                f"| TBR<54 | **{tbr_54:.2f}%** | <1% |",
                f"| TAR>180 | {tar_180:.1f}% | <25% |",
                f"| TAR>250 | {tar_250:.1f}% | <5% |",
                f"| Mean BG | {mean_bg:.1f} mg/dL ({mgdl_to_mmol(mean_bg):.1f} mmol/L) | — |",
                f"| GMI | {gmi:.2f}% | <7% |",
                f"| CV | {cv:.1f}% | <36% |",
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
                aaps_predicted_eventual_bg_mgdl=float(aaps_predicted)
                if aaps_predicted is not None
                else None,
                aaps_effective_isf_mgdl_per_u=float(aaps_eff_isf)
                if aaps_eff_isf is not None
                else None,
                realized_5h_min_bg_mgdl=realized_min,
                realized_5h_drop_mgdl=drop,
                realized_isf_mgdl_per_u=realized_isf,
                meal_or_correction="meal" if is_meal else "correction",
                time_band=_time_band_label(bolus_ts),
                bg_band=_bg_band_label(pre_bg) if pre_bg else "unknown",
            )
        )
    return events


def _aggregate(
    events: list[BolusEvent], key_fn: Callable[[BolusEvent], str]
) -> list[BolusBandAggregate]:
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
    above = next(
        (b for b in by_bg_band if b.band_name in ("180_250", "over_250")), None
    )
    in_target = next(
        (b for b in by_bg_band if b.band_name in ("100_140", "140_180")), None
    )
    if 0.85 <= overall <= 1.15:
        return (
            f"AAPS Dynamic ISF is well-calibrated (overall ratio {overall:.2f}, "
            f"within ±15% of 1.0)."
        )
    if overall > 1.15:
        if above and in_target and above.isf_ratio_realized_vs_effective > 1.3 and \
           0.85 <= in_target.isf_ratio_realized_vs_effective <= 1.15:
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


def _build_iob_observations(
    sgvs: list[Sgv], treatments: list[Treatment]
) -> list[tuple[float, float, float]]:
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
