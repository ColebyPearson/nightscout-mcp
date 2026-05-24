"""Integration tests for Phase 1 read tools.

Uses respx to mock httpx at the wire. Each tool is exercised end-to-end
with realistic Nightscout payloads, then the critical safety property is
verified: no tool response, when serialized to JSON, contains the token.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from nightscout_mcp.client import NightscoutClient
from nightscout_mcp.config import Settings
from nightscout_mcp.tools import aaps_history as aaps_history_tools
from nightscout_mcp.tools import analytics as analytics_tools
from nightscout_mcp.tools import metrics as metrics_tools
from nightscout_mcp.tools import read as read_tools
from nightscout_mcp.tools import synthesis as synthesis_tools

# The whole point: if this string ever leaks into a tool response, the
# regression test below fails loudly.
LEAK_CANARY = "TOKEN-LEAK-CANARY-c4e9f3a1"


# --- Test doubles -------------------------------------------------------------


class _ToolRegistry:
    """Minimal FastMCP-compatible registry: captures @mcp.tool() decorations."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NIGHTSCOUT_URL", "https://test.nightscout.example")
    monkeypatch.setenv("NIGHTSCOUT_TOKEN", LEAK_CANARY)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def registry_and_client(settings: Settings) -> tuple[_ToolRegistry, NightscoutClient]:
    client = NightscoutClient(settings)
    reg = _ToolRegistry()
    read_tools.register(reg, lambda: client)
    analytics_tools.register(reg, lambda: client)
    metrics_tools.register(reg, lambda: client)
    synthesis_tools.register(reg, lambda: client)
    aaps_history_tools.register(reg, lambda: client)
    return reg, client


# --- Realistic Nightscout payloads -------------------------------------------


def _sgv_row(mgdl: int, iso: str, direction: str = "Flat") -> dict[str, Any]:
    return {
        "_id": f"sgv-{iso}",
        "sgv": mgdl,
        "date": 1_716_400_000_000,
        "dateString": iso,
        "direction": direction,
        "type": "sgv",
        "device": "share2",
    }


SAMPLE_ENTRIES = [
    _sgv_row(142, "2026-05-22T18:05:00.000Z", "FortyFiveUp"),
    _sgv_row(138, "2026-05-22T18:00:00.000Z", "Flat"),
]

SAMPLE_TREATMENTS = [
    {
        "_id": "t1",
        "eventType": "Correction Bolus",
        "created_at": "2026-05-22T17:30:00.000Z",
        "insulin": 1.5,
        "notes": "lunch overshoot",
        "enteredBy": "loop",
    },
    {
        "_id": "t2",
        "eventType": "Carb Correction",
        "created_at": "2026-05-22T13:00:00.000Z",
        "carbs": 30.0,
        "notes": "sandwich",
    },
]

SAMPLE_DEVICESTATUS = [
    {
        "device": "openaps://AAPS",
        "created_at": "2026-05-22T18:04:00.000Z",
        "isCharging": True,
        "uploaderBattery": 84,
        "openaps": {
            "iob": {"iob": 1.23, "basaliob": 0.25, "activity": 0.0153},
            "suggested": {
                "COB": 12.5,
                "reason": "Temp 0.8U/h for 30m",
                "sens": 2.8,  # effective ISF in mmol/L/U (oref0-style; profile is mmol)
                "variable_sens": 109.5,  # AAPS Dynamic ISF, mg/dL/U
                "isfMgdlForCarbs": 173.66,
                "sensitivityRatio": 0.928,
                "runningDynamicIsf": True,
                "algorithm": "SMB",
                "bg": 168,
                "eventualBG": 135,
                "targetBG": 100,
                "tick": "+3",
                "insulinReq": 0.3,
                "carbsReq": 0,
                "units": 0.2,
                "predBGs": {
                    "IOB": [168, 165, 162, 158, 150, 142, 138, 135],
                    "COB": [168, 170, 175, 180, 182, 180, 175, 170, 165, 158],
                    "UAM": [168, 170, 168, 165, 160, 155],
                    "ZT": [168, 170, 175, 180, 185],
                },
            },
        },
        "pump": {
            "battery": {"percent": 78},
            "reservoir": 145.0,
            "extended": {
                "ActiveProfile": "regular ",
                "BaseBasalRate": 0.45,
                "LastBolus": "5/22/26 11:41 PM",
                "LastBolusAmount": 0.2,
                "TempBasalAbsoluteRate": 0.8,
                "TempBasalStart": "5/22/26 5:50 PM",
                "TempBasalRemaining": 25,
                "Version": "3.4.0.0-796d36ef0d-2026.01.04",
            },
            "status": {"status": "Closed Loop"},
        },
        "uploader": {},  # legacy empty dict — exercise the uploaderBattery top-level fallback
    }
]

SAMPLE_PROFILE = [
    {
        "_id": "p1",
        "defaultProfile": "Default",
        "store": {
            "Default": {
                "dia": 5,
                "units": "mmol",
                "timezone": "America/New_York",
                "basal": [{"time": "00:00", "value": 0.5}],
                "sens": [{"time": "00:00", "value": 50}],
                "carbratio": [{"time": "00:00", "value": 10}],
                "target_low": [{"time": "00:00", "value": 5.0}],
                "target_high": [{"time": "00:00", "value": 7.0}],
            }
        },
    }
]

SAMPLE_STATUS = {
    "version": "15.0.5",
    "status": "ok",
    "name": "test-ns",
    "settings": {"units": "mmol"},
    "apiEnabled": True,
}


def _mock_all(respx_mock: respx.MockRouter) -> None:
    """Mount mocks for every NS endpoint the read tools touch."""
    base = "https://test.nightscout.example"
    respx_mock.get(url__startswith=f"{base}/api/v1/entries/sgv.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_ENTRIES)
    )
    respx_mock.get(url__startswith=f"{base}/api/v1/treatments.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_TREATMENTS)
    )
    respx_mock.get(url__startswith=f"{base}/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_DEVICESTATUS)
    )
    respx_mock.get(url__startswith=f"{base}/api/v1/profile.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROFILE)
    )
    respx_mock.get(url__startswith=f"{base}/api/v1/status.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_STATUS)
    )


# --- Per-tool behavior tests --------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_current_glucose_returns_latest_with_delta(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        result = await reg.tools["get_current_glucose"]()
    finally:
        await client.aclose()
    assert result.sgv_mgdl == 142
    assert result.delta_mgdl == 4  # 142 - 138
    assert result.trend_arrow == "↗"


@pytest.mark.asyncio
@respx.mock
async def test_get_glucose_history_passes_window_filter(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    route = respx.get(url__startswith="https://test.nightscout.example/api/v1/entries/sgv.json").mock(
        return_value=httpx.Response(200, json=SAMPLE_ENTRIES)
    )
    try:
        history = await reg.tools["get_glucose_history"](hours=24)
    finally:
        await client.aclose()
    assert len(history) == 2
    # The 24h ago filter param must be on the wire.
    params = route.calls.last.request.url.params
    assert "find[dateString][$gte]" in str(params) or any("find" in k for k in params)


@pytest.mark.asyncio
@respx.mock
async def test_get_glucose_stats_computes_over_filtered_window(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        stats = await reg.tools["get_glucose_stats"](hours=24)
    finally:
        await client.aclose()
    assert stats.reading_count == 2
    assert stats.mean_mgdl == 140.0  # (142+138)/2
    assert stats.tir_percent == 100.0


@pytest.mark.asyncio
@respx.mock
async def test_get_treatments_default_window(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        treatments = await reg.tools["get_treatments"](hours=24)
    finally:
        await client.aclose()
    assert len(treatments) == 2
    assert treatments[0].event_type == "Correction Bolus"


@pytest.mark.asyncio
@respx.mock
async def test_get_iob_cob_pulls_from_openaps(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        result = await reg.tools["get_iob_cob"]()
    finally:
        await client.aclose()
    assert result.iob_u == 1.23
    assert result.cob_g == 12.5
    assert result.source == "openaps"


@pytest.mark.asyncio
@respx.mock
async def test_get_current_profile_extracts_default_subprofile(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        profile = await reg.tools["get_current_profile"]()
    finally:
        await client.aclose()
    assert profile.name == "Default"
    assert profile.units == "mmol"
    assert profile.dia_hours == 5
    assert len(profile.basal) == 1
    assert profile.basal[0].value == 0.5


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_flattens_nested_fields(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.pump_battery_percent == 78
    assert ds.pump_reservoir_u == 145.0
    assert ds.iob_u == 1.23


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_surfaces_phone_charging_and_uploader_battery(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """uploaderBattery is a TOP-LEVEL field, not uploader.battery (bug fix).
    isCharging is also a top-level field on every AAPS-published row."""
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.uploader_battery_percent == 84  # from top-level uploaderBattery
    assert ds.phone_charging is True


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_surfaces_iob_detail(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """basal_iob_u and insulin_activity come from openaps.iob.{basaliob, activity}."""
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.basal_iob_u == 0.25
    assert ds.insulin_activity == 0.0153


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_surfaces_pump_extended_metadata(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """pump.extended.* carries active profile name, AAPS version, last bolus
    info, and temp-basal start time — none of which were surfaced before."""
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.active_profile == "regular "
    assert ds.base_basal_rate_uph == 0.45
    assert ds.last_bolus_at == "5/22/26 11:41 PM"
    assert ds.last_bolus_units == 0.2
    assert ds.temp_basal_absolute_rate_uph == 0.8
    assert ds.temp_basal_started_at == "5/22/26 5:50 PM"
    assert ds.aaps_version == "3.4.0.0-796d36ef0d-2026.01.04"
    assert ds.pump_status_text == "Closed Loop"


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_surfaces_aaps_algorithm_state(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """AAPS Dynamic ISF state (variable_sens, sensitivityRatio, runningDynamicIsf,
    targetBG, eventualBG, etc.) is the entire reason the algorithm sub-model exists."""
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.algorithm is not None
    assert ds.algorithm.algorithm == "SMB"
    assert ds.algorithm.running_dynamic_isf is True
    assert ds.algorithm.current_bg_mgdl == 168
    assert ds.algorithm.current_bg_mmol == 9.3  # 168 / 18.0
    assert ds.algorithm.eventual_bg_mgdl == 135
    assert ds.algorithm.target_bg_mgdl == 100
    assert ds.algorithm.target_bg_mmol == 5.6
    assert ds.algorithm.bg_tick == "+3"
    assert ds.algorithm.effective_isf_mgdl_per_u == 109.5
    assert ds.algorithm.effective_isf_mmol_per_u == 6.1  # 109.5 / 18.0
    assert ds.algorithm.isf_for_carbs_mgdl_per_u == 173.66
    assert ds.algorithm.sensitivity_ratio == 0.928
    assert ds.algorithm.insulin_required_u == 0.3
    assert ds.algorithm.smb_units == 0.2
    assert ds.algorithm.reason == "Temp 0.8U/h for 30m"  # full text, no truncation


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_summarizes_bg_predictions(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """predBGs.{IOB,COB,UAM,ZT} arrays are summarized to (length × 5min, endpoint)
    pairs — we drop the bodies to avoid LLM token bloat."""
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.predictions is not None
    # IOB list has 8 entries → 40 min ahead, endpoint 135 → 7.5 mmol
    assert ds.predictions.iob_minutes_ahead == 40
    assert ds.predictions.iob_endpoint_mgdl == 135
    assert ds.predictions.iob_endpoint_mmol == 7.5
    # COB list has 10 entries; last value in fixture is 158
    assert ds.predictions.cob_minutes_ahead == 50
    assert ds.predictions.cob_endpoint_mgdl == 158
    # UAM list has 6 entries
    assert ds.predictions.uam_minutes_ahead == 30
    assert ds.predictions.uam_endpoint_mgdl == 155
    # ZT list has 5 entries
    assert ds.predictions.zt_minutes_ahead == 25
    assert ds.predictions.zt_endpoint_mgdl == 185


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_uploaderBattery_fallback_to_legacy_nested(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """If a row lacks top-level uploaderBattery but has uploader.battery
    (older Nightscout / AAPS data), we still surface it."""
    reg, client = registry_and_client
    payload = [
        {
            "device": "openaps://Old AAPS",
            "created_at": "2026-05-22T18:00:00.000Z",
            "openaps": {"suggested": {"reason": "no data"}},
            "uploader": {"battery": 67},  # legacy location
        }
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.uploader_battery_percent == 67


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_algorithm_state_falls_back_to_enacted(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """Some loop cycles only have an enacted block (no suggested). The algorithm
    sub-model should fall back to reading from openaps.enacted."""
    reg, client = registry_and_client
    payload = [
        {
            "device": "openaps://AAPS",
            "created_at": "2026-05-22T18:00:00.000Z",
            "openaps": {
                "enacted": {
                    "algorithm": "SMB",
                    "variable_sens": 95.0,
                    "targetBG": 100,
                    "reason": "enacted-only row",
                },
            },
        }
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.algorithm is not None
    assert ds.algorithm.algorithm == "SMB"
    assert ds.algorithm.effective_isf_mgdl_per_u == 95.0
    assert ds.algorithm.target_bg_mgdl == 100


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_algorithm_state_is_none_when_no_signal(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """If neither suggested nor enacted carry any recognizable algorithm fields,
    `algorithm` should be None (not an all-None sub-model dragging tokens)."""
    reg, client = registry_and_client
    payload = [
        {
            "device": "openaps://AAPS",
            "created_at": "2026-05-22T18:00:00.000Z",
            "openaps": {"iob": {"iob": 1.0}},
            "pump": {"battery": {"percent": 90}, "reservoir": 100.0},
        }
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.algorithm is None
    assert ds.predictions is None
    assert ds.iob_u == 1.0  # but IOB still surfaces


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_tier_priority_prefers_rows_with_algorithm(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """When the chronologically-newest row has only pump info but an older row
    carries algorithm state, prefer the older row — the algorithm fields are
    the strongest signal for an LLM."""
    reg, client = registry_and_client
    payload = [
        # Latest: pump-only, no openaps blob
        {
            "device": "openaps://AAPSClient Phone",
            "created_at": "2026-05-22T18:10:00.000Z",
            "pump": {"reservoir": 75.0},
        },
        # Older: has full algorithm state
        {
            "device": "openaps://Loop Phone",
            "created_at": "2026-05-22T18:05:00.000Z",
            "openaps": {
                "suggested": {
                    "algorithm": "SMB",
                    "variable_sens": 110.0,
                    "targetBG": 95,
                    "bg": 180,
                    "reason": "older richer row",
                },
            },
        },
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    # Picked the older row because the newer one has no algorithm state
    assert ds.algorithm is not None
    assert ds.algorithm.algorithm == "SMB"
    assert ds.algorithm.effective_isf_mgdl_per_u == 110.0


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_skips_uploader_only_rows(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """Issue #2: when the latest devicestatus row is from a non-loop uploader,
    walk further back to find a row with actual loop/pump fields.
    """
    reg, client = registry_and_client
    payload = [
        # Most recent: uploader phone, no loop data
        {
            "device": "openaps://AAPSClient Phone",
            "created_at": "2026-05-23T00:50:00.000Z",
            "uploader": {"battery": 84},
        },
        # 1 step back: still uploader
        {
            "device": "openaps://AAPSClient Phone",
            "created_at": "2026-05-23T00:45:00.000Z",
            "uploader": {"battery": 83},
        },
        # 2 steps back: actual loop instance with iob
        {
            "device": "openaps://Loop Phone",
            "created_at": "2026-05-23T00:40:00.000Z",
            "openaps": {"iob": {"iob": 2.75}, "suggested": {"COB": 31.5}},
            "pump": {"battery": {"percent": 88}, "reservoir": 145.0},
        },
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    # Should have skipped the two uploader-only rows.
    assert ds.iob_u == 2.75
    assert ds.cob_g == 31.5
    assert ds.pump_reservoir_u == 145.0


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_prefers_loop_data_over_pump_only_latest(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """Issue #2 — live scenario from gladoctopus.my.nightscoutpro.com:

    The chronologically newest devicestatus row has pump fields (reservoir,
    battery) but no loop fields (iob, cob, enacted). A prior row carries
    the openaps loop blob. The tool must prefer the row with loop data
    even though it's older.
    """
    reg, client = registry_and_client
    payload = [
        # Latest: pump-only (typical AAPSClient cycle without iob publish)
        {
            "device": "openaps://AAPSClient Phone",
            "created_at": "2026-05-23T01:15:00.000Z",
            "pump": {"reservoir": 75.0},
        },
        # Older: has loop data we actually want
        {
            "device": "openaps://Loop Phone",
            "created_at": "2026-05-23T01:10:00.000Z",
            "openaps": {"iob": {"iob": 2.75}, "suggested": {"COB": 31.5}},
            "pump": {"reservoir": 80.0},
        },
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    # Must surface IOB/COB from the older row, not the pump-only newer one.
    assert ds.iob_u == 2.75
    assert ds.cob_g == 31.5


@pytest.mark.asyncio
@respx.mock
async def test_get_device_status_falls_back_when_no_row_has_loop_data(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """If NO recent row has loop/pump data, return the literal latest row
    (caller still gets timestamp + device + uploader battery).
    """
    reg, client = registry_and_client
    payload = [
        {
            "device": "openaps://AAPSClient Phone",
            "created_at": "2026-05-23T00:50:00.000Z",
            "uploader": {"battery": 84},
        }
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/devicestatus.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        ds = await reg.tools["get_device_status"](latest=True)
    finally:
        await client.aclose()
    assert ds.device == "openaps://AAPSClient Phone"
    assert ds.iob_u is None


@pytest.mark.asyncio
@respx.mock
async def test_get_server_status(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        status = await reg.tools["get_server_status"]()
    finally:
        await client.aclose()
    assert status.version == "15.0.5"
    assert status.server_units == "mmol"


@pytest.mark.asyncio
@respx.mock
async def test_search_treatments_filters_by_substring(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        results = await reg.tools["search_treatments"](query="sandwich")
    finally:
        await client.aclose()
    assert len(results) == 1
    assert results[0].id == "t2"


@pytest.mark.asyncio
@respx.mock
async def test_glucose_at_time_returns_closest_reading(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    # Three readings; the 3am one is closest to the requested 03:01.
    payload = [
        _sgv_row(120, "2026-05-22T03:05:00.000Z"),
        _sgv_row(118, "2026-05-22T03:00:00.000Z"),
        _sgv_row(122, "2026-05-22T02:55:00.000Z"),
    ]
    respx.get(url__startswith="https://test.nightscout.example/api/v1/entries/sgv.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        result = await reg.tools["glucose_at_time"](time_iso="2026-05-22T03:01:00Z")
    finally:
        await client.aclose()
    assert result.sgv_mgdl == 118  # the 03:00 reading is closest (1 min before)
    assert result.minutes_from_requested == -1
    assert result.within_tolerance is True


@pytest.mark.asyncio
@respx.mock
async def test_glucose_at_time_handles_no_readings_in_window(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    reg, client = registry_and_client
    respx.get(url__startswith="https://test.nightscout.example/api/v1/entries/sgv.json").mock(
        return_value=httpx.Response(200, json=[])
    )
    try:
        result = await reg.tools["glucose_at_time"](time_iso="2026-05-22T03:00:00Z")
    finally:
        await client.aclose()
    assert result.sgv_mgdl is None
    assert result.within_tolerance is False
    assert result.minutes_from_requested is None


# --- The critical safety property --------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_no_tool_response_contains_the_token(
    registry_and_client: tuple[_ToolRegistry, NightscoutClient],
) -> None:
    """Regression test: a tool response must never include the token,
    even by accident (e.g. echoed back from a Nightscout error payload).
    """
    reg, client = registry_and_client
    _mock_all(respx.mock)
    try:
        responses = [
            await reg.tools["get_current_glucose"](),
            await reg.tools["get_glucose_history"](hours=6),
            await reg.tools["get_glucose_stats"](hours=24),
            await reg.tools["get_treatments"](hours=24),
            await reg.tools["get_iob_cob"](),
            await reg.tools["get_current_profile"](),
            await reg.tools["get_device_status"](latest=True),
            await reg.tools["get_server_status"](),
            await reg.tools["search_treatments"](query="bolus"),
            await reg.tools["glucose_at_time"](time_iso="2026-05-22T18:00:00Z"),
            await reg.tools["effective_isf_check"](days=1),
            await reg.tools["daily_synthesis"](days_back=1),
            await reg.tools["health_check"](),
            # PR #1-6 — research-driven metrics tools (token-leak coverage)
            await reg.tools["glycemia_risk_index"](days=1),
            await reg.tools["bg_risk_indices"](days=1),
            await reg.tools["glucose_variability_metrics"](days=1),
            await reg.tools["time_in_range_with_ci"](days=1),
            await reg.tools["per_meal_period_tir"](days=1),
            await reg.tools["ambulatory_glucose_profile"](days=1),
            await reg.tools["bolus_event_residuals"](days=1),
            await reg.tools["change_points_bg"](days=7),
            await reg.tools["change_points_tdd"](days=14),
            await reg.tools["dia_fit_estimate"](days=7),
            await reg.tools["clinic_packet"](days=7),
            # Section C — composition tools
            await reg.tools["dynisf_adjustment_recommender"](days=7),
            await reg.tools["consensus_target_audit"](days=7),
            await reg.tools["settings_change_attribution"](days=14, pre_post_days=3),
            await reg.tools["agp_markdown_render"](days=7),
            await reg.tools["time_period_compare"](
                period_a_start="2026-05-15",
                period_a_end="2026-05-18",
                period_b_start="2026-05-18",
                period_b_end="2026-05-21",
            ),
            # Track 5 — AAPS history tools (graceful-empty when DB missing)
            await reg.tools["aaps_history_status"](),
            await reg.tools["aaps_setting_at"](
                key="DynISFAdjust", time_iso="2026-05-24T00:00:00Z"
            ),
            await reg.tools["aaps_setting_history"](key="DynISFAdjust", days_back=30),
            await reg.tools["aaps_settings_diff"](
                from_iso="2026-05-23T00:00:00Z", to_iso="2026-05-25T00:00:00Z"
            ),
            await reg.tools["aaps_log_user_entries"](
                start_iso="2026-05-23T00:00:00Z",
                end_iso="2026-05-25T00:00:00Z",
                event_types=["BOLUS"],
            ),
        ]
    finally:
        await client.aclose()

    for resp in responses:
        # Pydantic models / lists / dicts — serialize uniformly via JSON.
        if hasattr(resp, "model_dump_json"):
            serialized = resp.model_dump_json()
        else:
            serialized = json.dumps(resp, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o))
        assert LEAK_CANARY not in serialized, f"Token leaked in: {type(resp).__name__}"
