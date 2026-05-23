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
    AlgorithmState,
    BgPredictions,
    CurrentGlucose,
    DeviceStatusSummary,
    GlucoseAtTime,
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
from ..units import direction_to_arrow, mgdl_to_mmol

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


def _safe_dict(obj: Any) -> dict[str, Any]:
    """Return obj if it's a dict, else an empty dict — for defensive nested reads."""
    return obj if isinstance(obj, dict) else {}


def _algorithm_state(suggested: dict[str, Any]) -> AlgorithmState | None:
    """Build an AlgorithmState from openaps.suggested (or openaps.enacted as
    a fallback). Returns None if no recognizable algorithm fields present.
    """
    if not suggested:
        return None
    # If NONE of the algorithm fields are present, skip the sub-model entirely
    keys_of_interest = (
        "algorithm",
        "runningDynamicIsf",
        "bg",
        "eventualBG",
        "targetBG",
        "tick",
        "variable_sens",
        "isfMgdlForCarbs",
        "sensitivityRatio",
        "insulinReq",
        "carbsReq",
        "units",
        "reason",
    )
    if not any(k in suggested for k in keys_of_interest):
        return None
    return AlgorithmState(
        algorithm=suggested.get("algorithm") if isinstance(suggested.get("algorithm"), str) else None,
        running_dynamic_isf=suggested.get("runningDynamicIsf")
        if isinstance(suggested.get("runningDynamicIsf"), bool)
        else None,
        current_bg_mgdl=int(suggested["bg"]) if isinstance(suggested.get("bg"), (int, float)) else None,
        eventual_bg_mgdl=int(suggested["eventualBG"])
        if isinstance(suggested.get("eventualBG"), (int, float))
        else None,
        target_bg_mgdl=int(suggested["targetBG"])
        if isinstance(suggested.get("targetBG"), (int, float))
        else None,
        bg_tick=suggested.get("tick") if isinstance(suggested.get("tick"), str) else None,
        effective_isf_mgdl_per_u=float(suggested["variable_sens"])
        if isinstance(suggested.get("variable_sens"), (int, float))
        else None,
        isf_for_carbs_mgdl_per_u=float(suggested["isfMgdlForCarbs"])
        if isinstance(suggested.get("isfMgdlForCarbs"), (int, float))
        else None,
        sensitivity_ratio=float(suggested["sensitivityRatio"])
        if isinstance(suggested.get("sensitivityRatio"), (int, float))
        else None,
        insulin_required_u=float(suggested["insulinReq"])
        if isinstance(suggested.get("insulinReq"), (int, float))
        else None,
        carbs_required_g=float(suggested["carbsReq"])
        if isinstance(suggested.get("carbsReq"), (int, float))
        else None,
        smb_units=float(suggested["units"])
        if isinstance(suggested.get("units"), (int, float))
        else None,
        reason=str(suggested["reason"]) if isinstance(suggested.get("reason"), str) else None,
    )


def _bg_predictions(suggested: dict[str, Any]) -> BgPredictions | None:
    """Summarize openaps.suggested.predBGs.{IOB,COB,UAM,ZT} to (length, endpoint)
    pairs. Returns None if no predBGs sub-arrays present.
    """
    pred = _safe_dict(suggested.get("predBGs"))
    if not pred:
        return None
    kwargs: dict[str, Any] = {}
    for prefix, key in (("iob", "IOB"), ("cob", "COB"), ("uam", "UAM"), ("zt", "ZT")):
        arr = pred.get(key)
        if isinstance(arr, list) and arr:
            # Cadence is 5 min per element in oref0/AAPS conventions
            kwargs[f"{prefix}_minutes_ahead"] = len(arr) * 5
            last = arr[-1]
            if isinstance(last, (int, float)):
                kwargs[f"{prefix}_endpoint_mgdl"] = int(last)
    if not kwargs:
        return None
    return BgPredictions(**kwargs)


def _flatten_device_status(row: dict[str, Any]) -> DeviceStatusSummary:
    """Pull the LLM-relevant fields out of a (deeply nested) devicestatus row.

    Reads from `openaps.suggested` for algorithm state (the *decision* state),
    falling back to `openaps.enacted` when suggested is absent. Pump-extended
    metadata (active profile, AAPS version, temp basal info) comes from
    `pump.extended.*`. Phone state from top-level `isCharging` and
    `uploaderBattery`.
    """
    pump = _safe_dict(row.get("pump"))
    openaps = _safe_dict(row.get("openaps"))
    loop = _safe_dict(row.get("loop"))
    uploader = _safe_dict(row.get("uploader"))
    pump_battery = _safe_dict(pump.get("battery"))
    pump_extended = _safe_dict(pump.get("extended"))
    pump_status = _safe_dict(pump.get("status"))

    # IOB / IOB-detail. Top-level iob_u stays simple for backwards compat;
    # basal_iob and activity come from openaps.iob if present.
    iob_u: float | None = None
    basal_iob_u: float | None = None
    insulin_activity: float | None = None
    iob_blob = openaps.get("iob") or loop.get("iob") or pump.get("iob")
    if isinstance(iob_blob, dict):
        if isinstance(iob_blob.get("iob"), (int, float)):
            iob_u = float(iob_blob["iob"])
        if isinstance(iob_blob.get("basaliob"), (int, float)):
            basal_iob_u = float(iob_blob["basaliob"])
        if isinstance(iob_blob.get("activity"), (int, float)):
            insulin_activity = float(iob_blob["activity"])
    elif isinstance(iob_blob, (int, float)):
        iob_u = float(iob_blob)

    # COB
    cob_g: float | None = None
    suggested = _safe_dict(openaps.get("suggested")) or _safe_dict(loop.get("recommended"))
    enacted = _safe_dict(openaps.get("enacted")) or _safe_dict(loop.get("enacted"))
    if isinstance(suggested.get("COB"), (int, float)):
        cob_g = float(suggested["COB"])
    elif isinstance(loop.get("cob"), dict) and isinstance(loop["cob"].get("cob"), (int, float)):
        cob_g = float(loop["cob"]["cob"])

    # Algorithm state — prefer suggested, fall back to enacted
    algorithm = _algorithm_state(suggested) or _algorithm_state(enacted)
    predictions = _bg_predictions(suggested) or _bg_predictions(enacted)

    # Backwards-compat truncated reason
    reason_full = suggested.get("reason") if isinstance(suggested.get("reason"), str) else None
    suggested_str = reason_full[:200] if reason_full else None

    # Uploader battery — bug fix: real field is top-level `uploaderBattery`,
    # `uploader.battery` is a legacy/empty path. Try the canonical location
    # first, fall back for older Nightscout/AAPS data.
    uploader_battery: int | None = None
    if isinstance(row.get("uploaderBattery"), (int, float)):
        uploader_battery = int(row["uploaderBattery"])
    elif isinstance(uploader.get("battery"), (int, float)):
        uploader_battery = int(uploader["battery"])

    return DeviceStatusSummary(
        device=row.get("device"),
        created_at=row.get("created_at"),
        phone_charging=row.get("isCharging") if isinstance(row.get("isCharging"), bool) else None,
        uploader_battery_percent=uploader_battery,
        pump_battery_percent=pump_battery.get("percent")
        if isinstance(pump_battery.get("percent"), (int, float))
        else None,
        pump_battery_voltage=pump_battery.get("voltage")
        if isinstance(pump_battery.get("voltage"), (int, float))
        else None,
        pump_reservoir_u=round(float(pump.get("reservoir")), 1)
        if isinstance(pump.get("reservoir"), (int, float))
        else None,
        iob_u=round(iob_u, 2) if iob_u is not None else None,
        cob_g=round(cob_g, 1) if cob_g is not None else None,
        basal_iob_u=round(basal_iob_u, 3) if basal_iob_u is not None else None,
        insulin_activity=round(insulin_activity, 4) if insulin_activity is not None else None,
        loop_enacted_rate=enacted.get("rate") if isinstance(enacted.get("rate"), (int, float)) else None,
        loop_enacted_duration_min=enacted.get("duration")
        if isinstance(enacted.get("duration"), (int, float))
        else None,
        loop_temp_basal_minutes_remaining=enacted.get("minutesRemaining")
        if isinstance(enacted.get("minutesRemaining"), (int, float))
        else None,
        suggested_temp=suggested_str,
        # Pump-extended metadata
        active_profile=pump_extended.get("ActiveProfile")
        if isinstance(pump_extended.get("ActiveProfile"), str)
        else None,
        base_basal_rate_uph=float(pump_extended["BaseBasalRate"])
        if isinstance(pump_extended.get("BaseBasalRate"), (int, float))
        else None,
        last_bolus_at=pump_extended.get("LastBolus")
        if isinstance(pump_extended.get("LastBolus"), str)
        else None,
        last_bolus_units=float(pump_extended["LastBolusAmount"])
        if isinstance(pump_extended.get("LastBolusAmount"), (int, float))
        else None,
        temp_basal_absolute_rate_uph=float(pump_extended["TempBasalAbsoluteRate"])
        if isinstance(pump_extended.get("TempBasalAbsoluteRate"), (int, float))
        else None,
        temp_basal_started_at=pump_extended.get("TempBasalStart")
        if isinstance(pump_extended.get("TempBasalStart"), str)
        else None,
        aaps_version=pump_extended.get("Version")
        if isinstance(pump_extended.get("Version"), str)
        else None,
        pump_status_text=pump_status.get("status")
        if isinstance(pump_status.get("status"), str)
        else None,
        # Rich nested sub-views
        algorithm=algorithm,
        predictions=predictions,
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
            # IOB/COB/enacted/suggested are the original signals; algorithm state
            # (effective ISF, target, etc.) is the strongest indicator that this
            # row carries real per-cycle AAPS decision-making data.
            return any(
                v is not None
                for v in (ds.iob_u, ds.cob_g, ds.loop_enacted_rate, ds.suggested_temp)
            ) or (
                ds.algorithm is not None
                and any(
                    v is not None
                    for v in (
                        ds.algorithm.effective_isf_mgdl_per_u,
                        ds.algorithm.target_bg_mgdl,
                        ds.algorithm.algorithm,
                    )
                )
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

    @mcp.tool()
    async def glucose_at_time(time_iso: str) -> GlucoseAtTime:
        """The CGM reading closest to a given timestamp.

        Args:
            time_iso: ISO8601 timestamp (e.g. "2026-05-22T03:00:00Z" — UTC
                recommended). The returned reading may be slightly before or
                after the requested time; `minutes_from_requested` is signed
                (negative = before, positive = after) and `within_tolerance`
                is True only if the closest reading is within ±15 min.
        """
        client = get_client()
        target = parse_iso_to_utc(time_iso)
        bracket_start = target - timedelta(minutes=15)
        bracket_end = target + timedelta(minutes=15)
        rows = await client.get(
            "/api/v1/entries/sgv.json",
            {
                "count": 20,
                "find[date][$gte]": int(bracket_start.timestamp() * 1000),
                "find[date][$lt]": int(bracket_end.timestamp() * 1000),
            },
        )
        sgvs = [Sgv.model_validate(r) for r in rows]

        if not sgvs:
            return GlucoseAtTime(
                requested_iso=time_iso,
                sgv_mgdl=None,
                sgv_mmol=None,
                direction=None,
                trend_arrow=direction_to_arrow(None),
                actual_iso=None,
                minutes_from_requested=None,
                within_tolerance=False,
            )

        # Pick the reading closest in absolute time distance.
        def distance_minutes(s: Sgv) -> int:
            return abs(int((parse_iso_to_utc(s.date_iso) - target).total_seconds() // 60))

        closest = min(sgvs, key=distance_minutes)
        actual_dt = parse_iso_to_utc(closest.date_iso)
        signed_delta_min = int((actual_dt - target).total_seconds() // 60)
        return GlucoseAtTime(
            requested_iso=time_iso,
            sgv_mgdl=closest.sgv_mgdl,
            sgv_mmol=mgdl_to_mmol(closest.sgv_mgdl),
            direction=closest.direction,
            trend_arrow=closest.trend_arrow,
            actual_iso=closest.date_iso,
            minutes_from_requested=signed_delta_min,
            within_tolerance=abs(signed_delta_min) <= 15,
        )
