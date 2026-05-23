"""Phase 1 read tools.

Each tool returns LLM-friendly structured data. Tokens never appear in any
return value — they're injected into the HTTP layer by client.py and stripped
from logs by logging_setup.py.

The `register(mcp, get_client)` pattern keeps these tools dependency-injectable:
production wires in the real httpx-backed client, tests pass a stub.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..client import NightscoutClient
from ..models import (
    CurrentGlucose,
    DeviceStatusSummary,
    GlucoseStats,
    IobCob,
    ProfileSummary,
    ScheduleEntry,
    ServerStatus,
    Sgv,
    Treatment,
    parse_iso_to_utc,
)
from ..stats import DEFAULT_TIR_HIGH, DEFAULT_TIR_LOW, compute_stats

# Be polite to free-tier hosted Nightscout instances.
MAX_ENTRY_COUNT = 2000
MAX_TREATMENT_COUNT = 2000


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso_z(dt: datetime) -> str:
    """ISO8601 with Z suffix — for `find[created_at][$gte]` (treatments)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _unix_ms(dt: datetime) -> int:
    """Unix milliseconds — for `find[date][$gte]` (entries).

    We filter entries on `date` (always present) rather than `dateString`
    (which some uploaders omit). Empirically necessary against real
    Nightscout instances even though both fields are documented.
    """
    return int(dt.timestamp() * 1000)


def _flatten_device_status(row: dict[str, Any]) -> DeviceStatusSummary:
    """Pull the LLM-relevant fields out of a (deeply nested) devicestatus row."""
    pump = row.get("pump", {}) or {}
    openaps = row.get("openaps", {}) or {}
    loop = row.get("loop", {}) or {}
    uploader = row.get("uploader", {}) or {}

    # IOB/COB can live in any of these — try richest source first.
    iob_u: float | None = None
    cob_g: float | None = None
    for src in (openaps.get("iob"), loop.get("iob"), pump.get("iob")):
        if isinstance(src, dict) and "iob" in src:
            iob_u = src.get("iob")
            break
        elif isinstance(src, (int, float)):
            iob_u = float(src)
            break
    cob_src = openaps.get("suggested", {}) if isinstance(openaps.get("suggested"), dict) else {}
    if "COB" in cob_src:
        cob_g = cob_src.get("COB")
    elif isinstance(loop.get("cob"), dict):
        cob_g = loop["cob"].get("cob")

    enacted = openaps.get("enacted") or loop.get("enacted") or {}
    suggested = openaps.get("suggested") or loop.get("recommended") or {}
    suggested_str: str | None = None
    if isinstance(suggested, dict) and suggested.get("reason"):
        suggested_str = str(suggested["reason"])[:200]

    return DeviceStatusSummary(
        device=row.get("device"),
        created_at=row.get("created_at"),
        pump_battery_percent=(pump.get("battery") or {}).get("percent")
        if isinstance(pump.get("battery"), dict)
        else None,
        pump_battery_voltage=(pump.get("battery") or {}).get("voltage")
        if isinstance(pump.get("battery"), dict)
        else None,
        pump_reservoir_u=round(pump.get("reservoir"), 1)
        if isinstance(pump.get("reservoir"), (int, float))
        else None,
        iob_u=round(iob_u, 2) if iob_u is not None else None,
        cob_g=round(cob_g, 1) if cob_g is not None else None,
        loop_enacted_rate=enacted.get("rate") if isinstance(enacted, dict) else None,
        loop_enacted_duration_min=enacted.get("duration") if isinstance(enacted, dict) else None,
        loop_temp_basal_minutes_remaining=enacted.get("minutesRemaining")
        if isinstance(enacted, dict)
        else None,
        suggested_temp=suggested_str,
        uploader_battery_percent=uploader.get("battery") if isinstance(uploader, dict) else None,
    )


def _parse_profile(raw: list[dict[str, Any]] | dict[str, Any]) -> ProfileSummary:
    """Map the active subprofile from /api/v1/profile.json to a ProfileSummary.

    The endpoint returns a list of profile records (history). Each record has
    a `defaultProfile` string naming which subprofile in `store` is active.
    """
    # Some NS deployments return a list, some a single dict — handle both.
    record = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(record, dict):
        raise ValueError(f"Unexpected profile shape: {type(record).__name__}")

    default_name = record.get("defaultProfile", "Default")
    store = record.get("store", {}) or {}
    sub = store.get(default_name) or next(iter(store.values()), {})

    def schedule(field: str) -> list[ScheduleEntry]:
        raw_entries = sub.get(field, []) or []
        return [
            ScheduleEntry(time=e.get("time", "00:00"), value=float(e.get("value", 0)))
            for e in raw_entries
            if isinstance(e, dict)
        ]

    return ProfileSummary(
        name=default_name,
        units=sub.get("units", "mmol"),
        timezone=sub.get("timezone", "UTC"),
        dia_hours=float(sub.get("dia", 0) or 0),
        basal=schedule("basal"),
        isf=schedule("sens"),
        carb_ratio=schedule("carbratio"),
        target_low=schedule("target_low"),
        target_high=schedule("target_high"),
    )


# --- The register function ---------------------------------------------------


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach all Phase 1 read tools to the FastMCP instance."""

    @mcp.tool()
    async def health_check() -> dict[str, Any]:
        """Verify the MCP can reach your Nightscout instance.

        Returns the Nightscout server version and configured units. Use this
        first after editing .env to confirm the round-trip works. No glucose
        data is returned.
        """
        client = get_client()
        status = await client.status()
        return {
            "nightscout_url": client.base_url,
            "nightscout_version": status.get("version"),
            "nightscout_status": status.get("status"),
            "nightscout_server_units": (status.get("settings") or {}).get("units"),
            "ok": True,
        }

    @mcp.tool()
    async def get_current_glucose() -> CurrentGlucose:
        """Latest CGM reading with trend arrow, freshness, and delta from prior."""
        client = get_client()
        rows = await client.get("/api/v1/entries/sgv.json", {"count": 2})
        if not rows:
            raise RuntimeError("No SGV readings available")
        latest = Sgv.model_validate(rows[0])
        prior = Sgv.model_validate(rows[1]) if len(rows) > 1 else None

        delta_mgdl = (latest.sgv_mgdl - prior.sgv_mgdl) if prior else None
        delta_mmol = (
            round(latest.sgv_mmol - prior.sgv_mmol, 1) if prior is not None else None
        )

        latest_dt = parse_iso_to_utc(latest.date_iso)
        minutes_ago = max(0, int((_now_utc() - latest_dt).total_seconds() // 60))

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

    @mcp.tool()
    async def get_glucose_history(hours: int = 6, count: int | None = None) -> list[Sgv]:
        """Time-series CGM readings over the past N hours.

        Args:
            hours: lookback window. Default 6.
            count: optional hard cap on number of readings. Default: enough to
                cover the window at 5min cadence (~13/hour), clamped to 2000.
        """
        client = get_client()
        since = _now_utc() - timedelta(hours=hours)
        effective_count = min(count or (hours * 13), MAX_ENTRY_COUNT)
        rows = await client.get(
            "/api/v1/entries/sgv.json",
            {
                "count": effective_count,
                "find[date][$gte]": _unix_ms(since),
            },
        )
        return [Sgv.model_validate(r) for r in rows]

    @mcp.tool()
    async def get_glucose_stats(
        hours: int = 24,
        tir_low_mgdl: int = DEFAULT_TIR_LOW,
        tir_high_mgdl: int = DEFAULT_TIR_HIGH,
    ) -> GlucoseStats:
        """Aggregate stats: mean, SD, CV%, GMI (A1C estimate), TIR/TBR/TAR.

        Args:
            hours: lookback window. Default 24.
            tir_low_mgdl: lower bound of time-in-range. Default 70.
            tir_high_mgdl: upper bound of time-in-range. Default 180.
        """
        client = get_client()
        since = _now_utc() - timedelta(hours=hours)
        effective_count = min(hours * 13, MAX_ENTRY_COUNT)
        rows = await client.get(
            "/api/v1/entries/sgv.json",
            {
                "count": effective_count,
                "find[date][$gte]": _unix_ms(since),
            },
        )
        readings = [Sgv.model_validate(r) for r in rows]
        return compute_stats(readings, window_hours=hours, tir_low=tir_low_mgdl, tir_high=tir_high_mgdl)

    @mcp.tool()
    async def get_treatments(
        hours: int = 24, event_type: str | None = None
    ) -> list[Treatment]:
        """Treatment records (boluses, carbs, basals, notes) over the past N hours.

        Args:
            hours: lookback window. Default 24.
            event_type: optional Nightscout eventType filter (e.g. "Bolus",
                "Carb Correction", "Temp Basal", "Note").
        """
        client = get_client()
        since = _now_utc() - timedelta(hours=hours)
        params: dict[str, Any] = {
            "count": MAX_TREATMENT_COUNT,
            "find[created_at][$gte]": _iso_z(since),
        }
        if event_type:
            params["find[eventType]"] = event_type
        rows = await client.get("/api/v1/treatments.json", params)
        return [Treatment.model_validate(r) for r in rows]

    @mcp.tool()
    async def get_iob_cob() -> IobCob:
        """Current insulin-on-board and carbs-on-board from latest devicestatus.

        Pulls from openaps/loop/pump fields in the most recent few devicestatus
        rows — more accurate than client-side re-derivation.
        """
        client = get_client()
        rows = await client.get("/api/v1/devicestatus.json", {"count": 5})
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

    @mcp.tool()
    async def get_current_profile() -> ProfileSummary:
        """Active profile: basal schedule, ISF, carb ratio, targets, DIA, timezone."""
        client = get_client()
        raw = await client.get("/api/v1/profile.json")
        return _parse_profile(raw)

    @mcp.tool()
    async def get_device_status(
        latest: bool = True,
    ) -> DeviceStatusSummary | list[DeviceStatusSummary]:
        """Pump / loop / uploader status.

        Args:
            latest: if True (default) return the most recent row carrying
                actual loop or pump data. If False, return the last 10 rows.

        When `latest=True` we walk up to 10 recent rows with a tiered
        preference: loop fields (iob/cob/enacted) beat pump fields
        (reservoir/battery) beat uploader-only rows. Necessary because
        AAPS often interleaves rows from the loop instance with rows
        from the AAPSClient phone, and the chronologically newest row
        is frequently the latter. See issue #2.
        """
        client = get_client()
        rows = await client.get("/api/v1/devicestatus.json", {"count": 10})
        flat = [_flatten_device_status(r) for r in rows]
        if not flat:
            return DeviceStatusSummary() if latest else []
        if not latest:
            return flat

        def has_loop(ds: DeviceStatusSummary) -> bool:
            return any(
                v is not None
                for v in (ds.iob_u, ds.cob_g, ds.loop_enacted_rate, ds.suggested_temp)
            )

        def has_pump(ds: DeviceStatusSummary) -> bool:
            return any(
                v is not None
                for v in (ds.pump_reservoir_u, ds.pump_battery_percent)
            )

        # Tier 1: most recent row with loop data
        for ds in flat:
            if has_loop(ds):
                return ds
        # Tier 2: most recent row with pump data
        for ds in flat:
            if has_pump(ds):
                return ds
        # Tier 3: literal latest
        return flat[0]

    @mcp.tool()
    async def get_server_status() -> ServerStatus:
        """Nightscout server version, status, and configured units."""
        client = get_client()
        raw = await client.status()
        return ServerStatus(
            version=raw.get("version"),
            status=raw.get("status"),
            name=raw.get("name"),
            server_units=(raw.get("settings") or {}).get("units"),
            api_enabled=raw.get("apiEnabled"),
        )

    @mcp.tool()
    async def search_treatments(
        query: str, hours: int = 720, event_type: str | None = None
    ) -> list[Treatment]:
        """Free-form text search across treatment notes/eventType/enteredBy.

        Args:
            query: case-insensitive substring to match.
            hours: lookback window. Default 720 (30 days).
            event_type: optional Nightscout eventType filter.
        """
        client = get_client()
        since = _now_utc() - timedelta(hours=hours)
        params: dict[str, Any] = {
            "count": MAX_TREATMENT_COUNT,
            "find[created_at][$gte]": _iso_z(since),
        }
        if event_type:
            params["find[eventType]"] = event_type
        rows = await client.get("/api/v1/treatments.json", params)
        treatments = [Treatment.model_validate(r) for r in rows]
        q = query.lower()
        return [
            t
            for t in treatments
            if q in (t.notes or "").lower()
            or q in t.event_type.lower()
            or q in (t.entered_by or "").lower()
        ]
