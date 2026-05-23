"""daily_synthesis MCP tool — cross-tool clinical roll-up.

Orchestrates ~11 sub-tool calls in parallel, then delegates to the pure
synthesis.build_synthesis() function. Heavier than other tools (fans out
to many HTTP calls); should be explicitly requested by the LLM/user, not
auto-fired on every turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..analytics import (
    carb_ratio_check as _carb_ratio_check,
)
from ..analytics import (
    compression_low_analysis as _compression_low_analysis,
)
from ..analytics import (
    detect_patterns as _detect_patterns,
)
from ..analytics import (
    effective_isf_check as _effective_isf_check,
)
from ..analytics import (
    insulin_sensitivity_check as _isf_check,
)
from ..client import NightscoutClient
from ..models import (
    CurrentGlucose,
    DailySynthesis,
    IobCob,
    Sgv,
    parse_iso_to_utc,
)
from ..stats import compute_stats
from ..synthesis import build_synthesis
from .analytics import (
    _extract_profile_settings,
    _fetch_devicestatus_between,
    _fetch_sgvs_between,
    _fetch_treatments_between,
)
from .read import _flatten_device_status


def _current_glucose_from_rows(rows: list[dict[str, Any]]) -> CurrentGlucose | None:
    """Build a CurrentGlucose from the last 2 SGV rows (most recent first)."""
    if not rows:
        return None
    latest = Sgv.model_validate(rows[0])
    prior = Sgv.model_validate(rows[1]) if len(rows) > 1 else None
    delta_mgdl = (latest.sgv_mgdl - prior.sgv_mgdl) if prior else None
    delta_mmol = round(latest.sgv_mmol - prior.sgv_mmol, 1) if prior else None
    latest_dt = parse_iso_to_utc(latest.date_iso)
    minutes_ago = max(0, int((datetime.now(UTC) - latest_dt).total_seconds() // 60))
    return CurrentGlucose(
        sgv_mgdl=latest.sgv_mgdl,
        sgv_mmol=latest.sgv_mmol,
        direction=latest.direction,
        trend_arrow=latest.trend_arrow,
        date_iso=latest.date_iso,
        minutes_ago=minutes_ago,
        delta_mgdl=delta_mgdl,
        delta_mmol=delta_mmol,
        device=latest.device,
    )


def _iob_cob_from_rows(rows: list[dict[str, Any]]) -> IobCob | None:
    """Walk recent devicestatus rows for IOB/COB — mirrors get_iob_cob."""
    for row in rows:
        summary = _flatten_device_status(row)
        if summary.iob_u is not None or summary.cob_g is not None:
            source = (
                "openaps"
                if (row.get("openaps") or {})
                else "loop"
                if (row.get("loop") or {})
                else "pump"
            )
            return IobCob(
                iob_u=round(summary.iob_u, 2) if summary.iob_u is not None else None,
                cob_g=round(summary.cob_g, 1) if summary.cob_g is not None else None,
                source=source,
                as_of_iso=summary.created_at,
            )
    return IobCob(iob_u=None, cob_g=None, source="unavailable", as_of_iso=None)


def _pick_latest_richest_devicestatus(rows: list[dict[str, Any]]) -> Any:
    """Same tier-priority logic as get_device_status(latest=True): prefer rows
    carrying loop/algorithm data over pump-only data over the literal latest.
    """
    flat = [_flatten_device_status(r) for r in rows]
    if not flat:
        return None

    def has_loop(ds: Any) -> bool:
        if any(
            v is not None
            for v in (ds.iob_u, ds.cob_g, ds.loop_enacted_rate, ds.suggested_temp)
        ):
            return True
        return ds.algorithm is not None and any(
            v is not None
            for v in (
                ds.algorithm.effective_isf_mgdl_per_u,
                ds.algorithm.target_bg_mgdl,
                ds.algorithm.algorithm,
            )
        )

    def has_pump(ds: Any) -> bool:
        return any(v is not None for v in (ds.pump_reservoir_u, ds.pump_battery_percent))

    for ds in flat:
        if has_loop(ds):
            return ds
    for ds in flat:
        if has_pump(ds):
            return ds
    return flat[0]


def _detect_patterns_from_sgvs(days: int, sgvs: list[Sgv]) -> Any:
    """Group SGVs by day and run detect_patterns. Mirrors the tool wrapper."""
    grouped: dict[str, list[Sgv]] = {}
    for s in sgvs:
        date_key = parse_iso_to_utc(s.date_iso).strftime("%Y-%m-%d")
        grouped.setdefault(date_key, []).append(s)
    daily_groups = sorted(grouped.items())
    return _detect_patterns(days, daily_groups)


def _yesterday_cv(sgvs_all: list[Sgv], yesterday_utc_midnight: datetime) -> float | None:
    """Compute CV% over a 24h window starting at yesterday's UTC midnight."""
    end = yesterday_utc_midnight + timedelta(days=1)
    in_window = [
        s for s in sgvs_all if yesterday_utc_midnight <= parse_iso_to_utc(s.date_iso) < end
    ]
    if len(in_window) < 10:  # too few readings for a meaningful CV
        return None
    stats = compute_stats(in_window, window_hours=24)
    return stats.cv_percent


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach daily_synthesis to the FastMCP instance."""

    @mcp.tool()
    async def daily_synthesis(days_back: int = 14) -> DailySynthesis:
        """Combined cross-tool clinical roll-up. Heavy — issues ~11 HTTP calls
        and fetches up to ~5400 devicestatus rows over a 14-day window. Expose
        as an explicit request, not a routine call.

        Args:
            days_back: lookback window for trend analytics. Default 14, max 30.

        Returns a DailySynthesis with: current snapshot, severity-sorted
        alerts (predicted lows, severe-hypo clusters, active rescue-carb
        requests), trend summary, pattern counts, and — most usefully —
        cross-tool insights that surface patterns only visible when multiple
        tool outputs are combined.

        Output is strictly observational. Recommendation text suggests
        questions for a care team, never direct setting changes.
        """
        client = get_client()
        days = max(1, min(days_back, 30))
        now = datetime.now(UTC)
        start = now - timedelta(days=days)

        # Pattern detection uses a fixed 7-day window for "recurring" labels.
        pattern_window_days = min(days, 7)
        pattern_start = now - timedelta(days=pattern_window_days)

        # Parallel fetches — independent HTTP calls
        sgvs_all, treatments_all, devicestatuses_all, latest_sgv_rows, latest_ds_rows = (
            await asyncio.gather(
                _fetch_sgvs_between(client, start, now),
                _fetch_treatments_between(client, start, now),
                _fetch_devicestatus_between(client, start, now),
                client.get("/api/v1/entries/sgv.json", {"count": 2}),
                client.get("/api/v1/devicestatus.json", {"count": 10}),
            )
        )

        profile_isf_mmol, profile_cr, dia, units = await _extract_profile_settings(client)

        # Snapshot from the latest few rows
        current_glucose = _current_glucose_from_rows(latest_sgv_rows)
        iob_cob = _iob_cob_from_rows(latest_ds_rows)
        device_status = _pick_latest_richest_devicestatus(latest_ds_rows)

        # Stats over the full window
        stats_window = compute_stats(sgvs_all, window_hours=days * 24)

        # Analytics
        isf_check = _isf_check(treatments_all, sgvs_all, profile_isf_mmol, dia_hours=dia)
        effective_isf = _effective_isf_check(
            treatments_all, sgvs_all, devicestatuses_all, profile_units=units, dia_hours=dia
        )
        cr_check = _carb_ratio_check(treatments_all, sgvs_all, profile_cr)

        # Pattern detection: 7-day window scoped from the bulk arrays
        sgvs_pattern_window = [
            s for s in sgvs_all if parse_iso_to_utc(s.date_iso) >= pattern_start
        ]
        treatments_pattern_window = [
            t for t in treatments_all if parse_iso_to_utc(t.created_at) >= pattern_start
        ]
        patterns = _detect_patterns_from_sgvs(pattern_window_days, sgvs_pattern_window)
        compression = _compression_low_analysis(
            pattern_window_days, sgvs_pattern_window, treatments=treatments_pattern_window
        )

        # Map pattern counts
        overnight_low = 0
        post_meal_spike = 0
        dawn_phenomenon = 0
        for p in patterns.patterns:
            if p.type == "overnight_low":
                overnight_low = p.occurrence_count
            elif p.type == "post_meal_spike":
                post_meal_spike = p.occurrence_count
            elif p.type == "dawn_phenomenon":
                dawn_phenomenon = p.occurrence_count

        # Yesterday CV (UTC midnight to UTC midnight — accept the limitation)
        yesterday_midnight = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        y_cv = _yesterday_cv(sgvs_all, yesterday_midnight)

        return build_synthesis(
            window_days=days,
            current_glucose=current_glucose,
            iob_cob=iob_cob,
            device_status=device_status,
            stats_window=stats_window,
            isf_check=isf_check,
            effective_isf_check=effective_isf,
            cr_check=cr_check,
            yesterday_cv_percent=y_cv,
            week_compare=None,  # optional follow-up; skip for now to keep fetches reasonable
            overnight_low_count=overnight_low,
            post_meal_spike_count=post_meal_spike,
            dawn_phenomenon_count=dawn_phenomenon,
            compression_count=len(compression.suspected),
            days_examined=pattern_window_days,
        )
