"""Phase 2 analytics tools — HTTP fetch + delegate to analytics.py pure functions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..analytics import (
    analyze_meal as _analyze_meal,
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
    insulin_sensitivity_check as _isf_check,
)
from ..analytics import (
    overnight_analysis as _overnight_analysis,
)
from ..client import NightscoutClient
from ..models import (
    CompressionAnalysis,
    DailyReport,
    DetectedPatterns,
    IsfDerivation,
    MealAnalysis,
    OvernightAnalysis,
    PeriodComparison,
    Sgv,
    Treatment,
    parse_iso_to_utc,
)

MAX_ENTRY_COUNT = 2000


def _unix_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_date(s: str) -> datetime:
    """Accept YYYY-MM-DD and return a UTC midnight datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


async def _fetch_sgvs_between(
    client: NightscoutClient, start: datetime, end: datetime
) -> list[Sgv]:
    """Fetch SGVs in [start, end). 5min cadence → ~13/hr; cap at 2000."""
    hours = max(1, int((end - start).total_seconds() // 3600))
    count = min(hours * 13, MAX_ENTRY_COUNT)
    rows = await client.get(
        "/api/v1/entries/sgv.json",
        {
            "count": count,
            "find[date][$gte]": _unix_ms(start),
            "find[date][$lt]": _unix_ms(end),
        },
    )
    return [Sgv.model_validate(r) for r in rows]


async def _fetch_treatments_between(
    client: NightscoutClient, start: datetime, end: datetime
) -> list[Treatment]:
    rows = await client.get(
        "/api/v1/treatments.json",
        {
            "count": MAX_ENTRY_COUNT,
            "find[created_at][$gte]": _iso_z(start),
            "find[created_at][$lt]": _iso_z(end),
        },
    )
    return [Treatment.model_validate(r) for r in rows]


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach all Phase 2 analytics tools to the FastMCP instance."""

    @mcp.tool()
    async def get_daily_report(date: str) -> DailyReport:
        """One-day glucose stats + treatment summary + notes.

        Args:
            date: YYYY-MM-DD (interpreted as UTC for consistency).
        """
        client = get_client()
        start = _parse_date(date)
        end = start + timedelta(days=1)
        sgvs, txs = await _fetch_sgvs_between(client, start, end), await _fetch_treatments_between(
            client, start, end
        )
        return _daily_report(date, sgvs, txs)

    @mcp.tool()
    async def compare_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
        period_a_label: str = "A",
        period_b_label: str = "B",
    ) -> PeriodComparison:
        """Side-by-side glucose stats comparison between two date ranges.

        Args:
            period_a_start: YYYY-MM-DD inclusive
            period_a_end:   YYYY-MM-DD exclusive
            period_b_start: YYYY-MM-DD inclusive
            period_b_end:   YYYY-MM-DD exclusive
            period_a_label, period_b_label: human labels (e.g. "last_week").

        Both periods MUST be the same length; otherwise stats aren't comparable.
        """
        client = get_client()
        a_start, a_end = _parse_date(period_a_start), _parse_date(period_a_end)
        b_start, b_end = _parse_date(period_b_start), _parse_date(period_b_end)
        if (a_end - a_start) != (b_end - b_start):
            raise ValueError(
                "compare_periods requires equal-length periods; "
                f"got A={a_end - a_start}, B={b_end - b_start}."
            )
        hours = int((a_end - a_start).total_seconds() // 3600)
        readings_a = await _fetch_sgvs_between(client, a_start, a_end)
        readings_b = await _fetch_sgvs_between(client, b_start, b_end)
        return _compare_periods(
            period_a_label, readings_a, period_b_label, readings_b, hours_each=hours
        )

    @mcp.tool()
    async def analyze_meal(
        meal_time_iso: str, window_hours: int = 4
    ) -> MealAnalysis:
        """Glucose response in a window after a meal/carb entry.

        Args:
            meal_time_iso: ISO8601 (e.g. "2026-05-22T18:30:00Z"). We look for
                a matching Carb Correction / Meal Bolus treatment within
                ±15 min of this time and analyze the BG response over the
                next `window_hours` hours.
            window_hours: default 4. Tighter = misses tail recovery; longer
                = picks up an unrelated subsequent meal.
        """
        client = get_client()
        meal_time = parse_iso_to_utc(meal_time_iso)
        bracket_start = meal_time - timedelta(minutes=30)
        bracket_end = meal_time + timedelta(hours=window_hours)
        sgvs = await _fetch_sgvs_between(client, bracket_start, bracket_end)

        # Look for a treatment near meal_time with carbs or matching eventType
        tx_window_start = meal_time - timedelta(minutes=15)
        tx_window_end = meal_time + timedelta(minutes=15)
        txs = await _fetch_treatments_between(client, tx_window_start, tx_window_end)
        candidates = [
            t
            for t in txs
            if (t.carbs and t.carbs > 0)
            or t.event_type in ("Carb Correction", "Meal Bolus", "Snack Bolus")
        ]
        meal = candidates[0] if candidates else None
        return _analyze_meal(meal_time, meal, sgvs, window_hours)

    @mcp.tool()
    async def overnight_analysis(date: str) -> OvernightAnalysis:
        """Characterize the overnight window (00:00-07:00 UTC) for one date.

        Args:
            date: YYYY-MM-DD (interpreted as UTC).

        Returns drift, min/max, time in low ranges, and dawn-rise magnitude.
        """
        client = get_client()
        start = _parse_date(date)
        end = start + timedelta(hours=7)
        sgvs = await _fetch_sgvs_between(client, start, end)
        return _overnight_analysis(date, sgvs)

    @mcp.tool()
    async def detect_patterns(days: int = 14) -> DetectedPatterns:
        """Detect recurring glucose patterns over the past N days.

        Patterns: overnight lows, dawn phenomenon, post-meal spikes.

        Args:
            days: number of days to analyze. Default 14, max 30.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        # Group by date string (YYYY-MM-DD)
        grouped: dict[str, list[Sgv]] = {}
        for s in sgvs:
            date_key = parse_iso_to_utc(s.date_iso).strftime("%Y-%m-%d")
            grouped.setdefault(date_key, []).append(s)
        daily_groups = sorted(grouped.items())
        return _detect_patterns(days, daily_groups)

    @mcp.tool()
    async def insulin_sensitivity_check(days: int = 14) -> IsfDerivation:
        """Derive your *real* insulin sensitivity from correction-bolus outcomes
        and compare to your profile ISF.

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
        # Profile ISF (in mmol/L per U)
        profile = await client.get("/api/v1/profile.json")
        profile_isf_mmol = None
        dia = 5.0
        try:
            record = profile[0] if isinstance(profile, list) and profile else profile
            sub = (record.get("store") or {}).get(record.get("defaultProfile", "Default"))
            if sub:
                isf_entries = sub.get("sens") or []
                if isf_entries:
                    profile_isf_mmol = float(isf_entries[0].get("value", 0)) or None
                dia = float(sub.get("dia", 5.0) or 5.0)
        except (AttributeError, KeyError, IndexError, TypeError):
            pass
        return _isf_check(txs, sgvs, profile_isf_mmol, dia_hours=dia)

    @mcp.tool()
    async def compression_low_analysis(days: int = 14) -> CompressionAnalysis:
        """Flag CGM dips that look like sensor-compression artifacts.

        Heuristic: fast drop ≥30 mg/dL in ≤15 min into the low band, then
        equally fast recovery within 15 min. Real hypos don't bounce back
        that fast without intervention.

        Args:
            days: lookback window. Default 14, max 30.
        """
        client = get_client()
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        sgvs = await _fetch_sgvs_between(client, start, end)
        return _compression_low_analysis(days, sgvs)
