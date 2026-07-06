"""Phase 2 analytics tools — HTTP fetch + delegate to analytics.py pure functions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..analytics import (
    analyze_meal as _analyze_meal,
)
from ..analytics import (
    carb_ratio_check as _carb_ratio_check,
)
from ..analytics import (
    compare_periods as _compare_periods,
)
from ..analytics import (
    compression_low_analysis as _compression_low_analysis,
)
from ..analytics import (
    daily_report as _daily_report,
)
from ..analytics import (
    detect_patterns as _detect_patterns,
)
from ..analytics import (
    effective_isf_check as _effective_isf_check,
)
from ..analytics import (
    hypo_episodes as _hypo_episodes,
)
from ..analytics import (
    insulin_sensitivity_check as _isf_check,
)
from ..analytics import (
    overnight_analysis as _overnight_analysis,
)
from ..client import NightscoutClient
from ..models import (
    CompressionAnalysis,
    CrDerivation,
    DailyReport,
    DetectedPatterns,
    EffectiveIsfDerivation,
    HypoEpisodeReport,
    IsfDerivation,
    MealAnalysis,
    OvernightAnalysis,
    PeriodComparison,
    Sgv,
    Treatment,
    parse_iso_to_utc,
)
from ..units import mgdl_to_mmol

MAX_ENTRY_COUNT_PER_PAGE = 2000
PAGINATION_TOTAL_CAP = 20000  # safety bound; ~14 days at 1min cadence = 20K


def _unix_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_date_utc_midnight(s: str) -> datetime:
    """Accept YYYY-MM-DD and return a UTC midnight datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _parse_date_in_timezone(s: str, tz_name: str | None) -> datetime:
    """Interpret YYYY-MM-DD as local midnight in the given timezone,
    convert to a UTC datetime. Falls back to UTC if no timezone is given.
    """
    if not tz_name:
        return _parse_date_utc_midnight(s)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return _parse_date_utc_midnight(s)
    except ImportError:
        return _parse_date_utc_midnight(s)
    naive = datetime.fromisoformat(s)
    local_midnight = naive.replace(tzinfo=tz)
    return local_midnight.astimezone(UTC)


def _tz_offset_hours(tz_name: str | None, at: datetime) -> float:
    """UTC offset in hours for a zone at a given instant (DST-aware).

    Returns 0.0 (UTC) when no zone is given or it can't be resolved.
    """
    if not tz_name:
        return 0.0
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return 0.0
    except ImportError:
        return 0.0
    offset = at.astimezone(tz).utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else 0.0


async def _resolve_timezone(client: NightscoutClient) -> str | None:
    """Look up the user's timezone from the active profile. Caller passes the
    result through to _parse_date_in_timezone.
    """
    try:
        profile = await client.get("/api/v1/profile.json")
        record = profile[0] if isinstance(profile, list) and profile else profile
        sub = (record.get("store") or {}).get(record.get("defaultProfile", "Default"))
        if sub and sub.get("timezone"):
            return sub["timezone"]
    except (AttributeError, KeyError, IndexError, TypeError):
        pass
    return None


async def _fetch_sgvs_between(client: NightscoutClient, start: datetime, end: datetime) -> list[Sgv]:
    """Fetch SGVs in [start, end), paginating through Nightscout's per-request cap.

    Single requests are capped at 2000 rows by Nightscout's API3_MAX_LIMIT
    (and most default deployments). We page backwards through the window
    until either no more rows return or we hit the safety bound (20K rows,
    ~14 days at 1-minute CGM cadence).
    """
    all_rows: list[dict[str, Any]] = []
    current_end = end
    seen_ids: set[str] = set()
    while True:
        page = await client.get(
            "/api/v1/entries/sgv.json",
            {
                "count": MAX_ENTRY_COUNT_PER_PAGE,
                "find[date][$gte]": _unix_ms(start),
                "find[date][$lt]": _unix_ms(current_end),
            },
        )
        if not page:
            break
        # Dedup defensively — Nightscout sometimes returns boundary rows twice.
        fresh = [r for r in page if (r.get("_id") or str(r.get("date"))) not in seen_ids]
        for r in fresh:
            seen_ids.add(r.get("_id") or str(r.get("date")))
        all_rows.extend(fresh)
        if len(page) < MAX_ENTRY_COUNT_PER_PAGE:
            break  # got all available in window
        if len(all_rows) >= PAGINATION_TOTAL_CAP:
            break  # safety bound
        oldest_ms = min(int(r.get("date", 0)) for r in page)
        next_end = datetime.fromtimestamp(oldest_ms / 1000, tz=UTC)
        if next_end >= current_end:
            break  # not making progress
        current_end = next_end
    return [Sgv.model_validate(r) for r in all_rows]


async def _fetch_treatments_between(client: NightscoutClient, start: datetime, end: datetime) -> list[Treatment]:
    """Paginated treatment fetch in [start, end). Same approach as SGVs."""
    all_rows: list[dict[str, Any]] = []
    current_end = end
    seen_ids: set[str] = set()
    while True:
        page = await client.get(
            "/api/v1/treatments.json",
            {
                "count": MAX_ENTRY_COUNT_PER_PAGE,
                "find[created_at][$gte]": _iso_z(start),
                "find[created_at][$lt]": _iso_z(current_end),
            },
        )
        if not page:
            break
        fresh = [r for r in page if (r.get("_id") or r.get("created_at")) not in seen_ids]
        for r in fresh:
            seen_ids.add(r.get("_id") or r.get("created_at"))
        all_rows.extend(fresh)
        if len(page) < MAX_ENTRY_COUNT_PER_PAGE:
            break
        if len(all_rows) >= PAGINATION_TOTAL_CAP:
            break
        oldest_iso = min(r.get("created_at", "") for r in page)
        if not oldest_iso:
            break
        try:
            next_end = parse_iso_to_utc(oldest_iso)
        except Exception:
            break
        if next_end >= current_end:
            break
        current_end = next_end
    return [Treatment.model_validate(r) for r in all_rows]


async def _fetch_devicestatus_between(client: NightscoutClient, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Paginated devicestatus fetch in [start, end), returning raw dicts.

    Devicestatus rows aren't modeled — analytics needs the full nested
    openaps blob (suggested.sens, iob, etc.) which varies by loop. Returning
    raw dicts keeps the consumer flexible.

    Note: devicestatus is the highest-cardinality collection in Nightscout —
    AAPS publishes one row per loop cycle (~5 min). 30 days ≈ 8600 rows. The
    PAGINATION_TOTAL_CAP=20000 safety bound covers this comfortably.
    """
    all_rows: list[dict[str, Any]] = []
    current_end = end
    seen_ids: set[str] = set()
    while True:
        page = await client.get(
            "/api/v1/devicestatus.json",
            {
                "count": MAX_ENTRY_COUNT_PER_PAGE,
                "find[created_at][$gte]": _iso_z(start),
                "find[created_at][$lt]": _iso_z(current_end),
            },
        )
        if not page:
            break
        fresh = [r for r in page if (r.get("_id") or r.get("created_at")) not in seen_ids]
        for r in fresh:
            seen_ids.add(r.get("_id") or r.get("created_at"))
        all_rows.extend(fresh)
        if len(page) < MAX_ENTRY_COUNT_PER_PAGE:
            break
        if len(all_rows) >= PAGINATION_TOTAL_CAP:
            break
        oldest_iso = min(r.get("created_at", "") for r in page)
        if not oldest_iso:
            break
        try:
            next_end = parse_iso_to_utc(oldest_iso)
        except Exception:
            break
        if next_end >= current_end:
            break
        current_end = next_end
    return all_rows


async def _extract_profile_settings(
    client: NightscoutClient,
) -> tuple[float | None, float | None, float, str]:
    """Read the active profile's ISF (mmol/L/U), carb ratio (g/U), DIA (hours),
    and units string ("mmol" or "mg/dL") from /api/v1/profile.json.

    Used by insulin_sensitivity_check, carb_ratio_check, and effective_isf_check.
    Falls back to (None, None, 5.0, "mmol") on any parsing failure.
    """
    profile_isf: float | None = None
    profile_cr: float | None = None
    dia: float = 5.0
    units: str = "mmol"
    try:
        profile = await client.get("/api/v1/profile.json")
        record = profile[0] if isinstance(profile, list) and profile else profile
        sub = (record.get("store") or {}).get(record.get("defaultProfile", "Default"))
        if sub:
            # Normalize units first — Nightscout emits "mmol", "mg/dl", "mg/dL".
            raw_units = str(sub.get("units", "mmol")).lower()
            units = "mg/dL" if raw_units in ("mg/dl", "mgdl") else "mmol"
            cr_entries = sub.get("carbratio") or []
            if cr_entries:
                profile_cr = float(cr_entries[0].get("value", 0)) or None
            isf_entries = sub.get("sens") or []
            if isf_entries:
                raw_isf = float(isf_entries[0].get("value", 0)) or None
                # The `sens` value is in the profile's own units. Callers expect
                # mmol/L per U, so convert mg/dL profiles. (Skipping this yields
                # an ~18x wrong ratio in insulin_sensitivity_check.)
                if raw_isf is not None:
                    profile_isf = mgdl_to_mmol(raw_isf) if units == "mg/dL" else raw_isf
            dia = float(sub.get("dia", 5.0) or 5.0)
    except (AttributeError, KeyError, IndexError, TypeError):
        pass
    return profile_isf, profile_cr, dia, units


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach all Phase 2 analytics tools to the FastMCP instance."""

    @mcp.tool()
    async def get_daily_report(date: str, timezone: str | None = None) -> DailyReport:
        """One-day glucose stats + treatment summary + notes.

        Args:
            date: YYYY-MM-DD.
            timezone: Olson timezone name (e.g. "America/Toronto"). If None,
                we auto-detect from your Nightscout profile, falling back to
                UTC if no profile timezone is set. The "day" boundary is
                local midnight to local midnight.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        start = _parse_date_in_timezone(date, tz_name)
        end = start + timedelta(days=1)
        sgvs, txs = await _fetch_sgvs_between(client, start, end), await _fetch_treatments_between(client, start, end)
        return _daily_report(date, sgvs, txs)

    @mcp.tool()
    async def compare_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
        period_a_label: str = "A",
        period_b_label: str = "B",
        timezone: str | None = None,
    ) -> PeriodComparison:
        """Side-by-side glucose stats comparison between two date ranges.

        Args:
            period_a_start: YYYY-MM-DD inclusive
            period_a_end:   YYYY-MM-DD exclusive
            period_b_start: YYYY-MM-DD inclusive
            period_b_end:   YYYY-MM-DD exclusive
            period_a_label, period_b_label: human labels (e.g. "last_week").
            timezone: Olson zone name; if None, auto-detect from profile.

        Both periods MUST be the same length; otherwise stats aren't comparable.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        a_start = _parse_date_in_timezone(period_a_start, tz_name)
        a_end = _parse_date_in_timezone(period_a_end, tz_name)
        b_start = _parse_date_in_timezone(period_b_start, tz_name)
        b_end = _parse_date_in_timezone(period_b_end, tz_name)
        if (a_end - a_start) != (b_end - b_start):
            raise ValueError(
                f"compare_periods requires equal-length periods; got A={a_end - a_start}, B={b_end - b_start}."
            )
        hours = int((a_end - a_start).total_seconds() // 3600)
        readings_a = await _fetch_sgvs_between(client, a_start, a_end)
        readings_b = await _fetch_sgvs_between(client, b_start, b_end)
        return _compare_periods(period_a_label, readings_a, period_b_label, readings_b, hours_each=hours)

    @mcp.tool()
    async def analyze_meal(
        meal_time_iso: str,
        window_hours: int = 4,
        match_window_minutes: int = 30,
    ) -> MealAnalysis:
        """Glucose response in a window after a meal/carb entry.

        Args:
            meal_time_iso: ISO8601 (e.g. "2026-05-22T18:30:00Z"). We look for
                a matching Carb Correction / Meal Bolus treatment within
                ±match_window_minutes of this time and analyze the BG response
                over the next `window_hours` hours.
            window_hours: default 4. Tighter = misses tail recovery; longer
                = picks up an unrelated subsequent meal.
            match_window_minutes: how loosely to associate the timestamp with
                a logged treatment. Default 30 (was 15 — too tight in practice;
                users frequently log carbs 20-30 min late).
        """
        client = get_client()
        meal_time = parse_iso_to_utc(meal_time_iso)
        bracket_start = meal_time - timedelta(minutes=30)
        bracket_end = meal_time + timedelta(hours=window_hours)
        sgvs = await _fetch_sgvs_between(client, bracket_start, bracket_end)

        # Look for a treatment near meal_time with carbs or matching eventType
        tx_window_start = meal_time - timedelta(minutes=match_window_minutes)
        tx_window_end = meal_time + timedelta(minutes=match_window_minutes)
        txs = await _fetch_treatments_between(client, tx_window_start, tx_window_end)
        candidates = [
            t
            for t in txs
            if (t.carbs and t.carbs > 0) or t.event_type in ("Carb Correction", "Meal Bolus", "Snack Bolus")
        ]
        meal = candidates[0] if candidates else None
        return _analyze_meal(meal_time, meal, sgvs, window_hours)

    @mcp.tool()
    async def overnight_analysis(date: str, timezone: str | None = None) -> OvernightAnalysis:
        """Characterize the overnight window (00:00-07:00 local) for one date.

        Args:
            date: YYYY-MM-DD.
            timezone: Olson zone name; if None, auto-detect from profile.

        Returns drift, min/max, time in low ranges, and dawn-rise magnitude.
        """
        client = get_client()
        tz_name = timezone or await _resolve_timezone(client)
        start = _parse_date_in_timezone(date, tz_name)
        end = start + timedelta(hours=7)
        sgvs = await _fetch_sgvs_between(client, start, end)
        offset = _tz_offset_hours(tz_name, start)
        return _overnight_analysis(date, sgvs, tz_offset_hours=offset)

    @mcp.tool()
    async def detect_patterns(days: int = 14, timezone: str | None = None) -> DetectedPatterns:
        """Detect recurring glucose patterns over the past N days.

        Patterns: overnight lows, dawn phenomenon, post-meal spikes. Overnight
        and dawn windows are evaluated in local time so "overnight" means the
        patient's night, not 00:00-06:00 UTC.

        Args:
            days: number of days to analyze. Default 14, max 30.
            timezone: Olson zone name; if None, auto-detect from profile.
        """
        client = get_client()
        days = max(1, min(days, 30))
        tz_name = timezone or await _resolve_timezone(client)
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=days)
        offset = _tz_offset_hours(tz_name, start)
        sgvs = await _fetch_sgvs_between(client, start, end)
        # Group by LOCAL date string (YYYY-MM-DD) so a night belongs to its
        # local calendar day, not the UTC one.
        grouped: dict[str, list[Sgv]] = {}
        for s in sgvs:
            local_ts = parse_iso_to_utc(s.date_iso) + timedelta(hours=offset)
            date_key = local_ts.strftime("%Y-%m-%d")
            grouped.setdefault(date_key, []).append(s)
        daily_groups = sorted(grouped.items())
        return _detect_patterns(days, daily_groups, tz_offset_hours=offset)

    @mcp.tool()
    async def hypoglycemia_episodes(days: int = 14, timezone: str | None = None) -> HypoEpisodeReport:
        """Detect consensus hypoglycemic EVENTS (not just % time below range).

        Applies the Battelino 2019 / ATTD definition: an event is BG <70 mg/dL
        for >=15 min; level 2 (clinically significant) is a nadir <54. Reports
        per-event start/end/duration/nadir/level, whether it was nocturnal
        (local 00:00-06:00), and whether a rescue carb was logged during it —
        plus summary counts and % CGM-active so an under-sampled window is
        flagged rather than silently undercounting.

        Args:
            days: lookback window. Default 14, max 90.
            timezone: Olson zone name; if None, auto-detect from profile.
        """
        client = get_client()
        days = max(1, min(days, 90))
        tz_name = timezone or await _resolve_timezone(client)
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        offset = _tz_offset_hours(tz_name, start)
        sgvs = await _fetch_sgvs_between(client, start, end)
        txs = await _fetch_treatments_between(client, start, end)
        return _hypo_episodes(days, sgvs, txs, tz_offset_hours=offset)

    @mcp.tool()
    async def insulin_sensitivity_check(days: int = 14) -> IsfDerivation:
        """Derive your *real* insulin sensitivity from correction-bolus outcomes
        and compare to your PROFILE ISF.

        Use this when you want to validate your profile ISF against real
        correction outcomes. For AAPS Dynamic ISF users, prefer
        `effective_isf_check` — Dynamic ISF overrides profile ISF entirely.

        Args:
            days: lookback window. Default 14, max 30.

        Algorithm: find isolated correction boluses (no carbs within ±60 min),
        measure the BG drop in the next DIA hours, divide by units. Returns
        mg/dL drop per unit + a recommendation.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        txs = await _fetch_treatments_between(client, start, end)
        profile_isf_mmol, _, dia, _ = await _extract_profile_settings(client)
        return _isf_check(txs, sgvs, profile_isf_mmol, dia_hours=dia)

    @mcp.tool()
    async def carb_ratio_check(days: int = 14) -> CrDerivation:
        """Derive a real-world carb-ratio signal from meal-bolus outcomes
        and compare to your profile carb ratio.

        Args:
            days: lookback window. Default 14, max 30.

        A meal is eligible when: carbs > 5g, insulin > 0, has a pre-meal CGM
        reading AND a CGM reading ~4h later, AND no other meal lands in that
        4h window. Reports both the average CR the user actually applied
        (carbs ÷ insulin) and the average post-meal residual (end BG − pre-meal
        BG) so over- vs under-bolusing is visible separately from CR drift.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        txs = await _fetch_treatments_between(client, start, end)
        _, profile_cr, _, _ = await _extract_profile_settings(client)
        return _carb_ratio_check(txs, sgvs, profile_cr)

    @mcp.tool()
    async def effective_isf_check(days: int = 14) -> EffectiveIsfDerivation:
        """Compare AAPS's per-correction effective ISF (from devicestatus) to
        realized BG drops, stratified by pre-bolus BG band.

        Use this when you run AAPS Dynamic ISF and want to know whether AAPS's
        per-correction effective ISF matches reality. For non-AAPS / flat-ISF
        users, use `insulin_sensitivity_check` instead.

        Args:
            days: lookback window. Default 14, max 30.

        Note: heavier than insulin_sensitivity_check — devicestatus carries
        ~288 rows/day (~5 min cadence). 30-day windows fetch ~8600 rows.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        txs = await _fetch_treatments_between(client, start, end)
        dss = await _fetch_devicestatus_between(client, start, end)
        _, _, dia, units = await _extract_profile_settings(client)
        return _effective_isf_check(txs, sgvs, dss, profile_units=units, dia_hours=dia)

    @mcp.tool()
    async def compression_low_analysis(days: int = 14) -> CompressionAnalysis:
        """Flag CGM dips that look like sensor-compression artifacts.

        Heuristic: fast drop ≥30 mg/dL in ≤15 min into the low band, then
        equally fast recovery within 15 min. Real hypos don't bounce back
        that fast *without intervention* — so we also cross-check treatments:
        candidates where a carb treatment (>10g) landed within ±15 min of
        the minimum are suppressed (those were real lows the user treated).

        Args:
            days: lookback window. Default 14, max 30.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        txs = await _fetch_treatments_between(client, start, end)
        return _compression_low_analysis(days, sgvs, treatments=txs)
