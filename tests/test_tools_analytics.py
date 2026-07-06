"""Tool-wrapper tests for tools/analytics.py helpers (respx-mocked HTTP)."""

from __future__ import annotations

import httpx
import pytest
import respx

from nightscout_mcp.client import NightscoutClient
from nightscout_mcp.config import Settings
from nightscout_mcp.tools.analytics import _extract_profile_settings

BASE = "https://test.nightscout.example"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> NightscoutClient:
    monkeypatch.setenv("NIGHTSCOUT_URL", BASE)
    monkeypatch.setenv("NIGHTSCOUT_TOKEN", "mcp-reader-abc12345")
    return NightscoutClient(Settings(_env_file=None))  # type: ignore[call-arg]


def _profile_doc(units: str, sens_value: float) -> list[dict]:
    return [
        {
            "defaultProfile": "Default",
            "store": {
                "Default": {
                    "units": units,
                    "dia": 5.0,
                    "sens": [{"time": "00:00", "value": sens_value}],
                    "carbratio": [{"time": "00:00", "value": 10}],
                }
            },
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_extract_profile_converts_mgdl_sens_to_mmol(
    client: NightscoutClient,
) -> None:
    """A mg/dL profile stores sens in mg/dL (e.g. 50). It must be returned as
    mmol (~2.78), not passed through as 50 — otherwise insulin_sensitivity_check
    compares 50 against a ~2.8 mmol derived value and reports an ~18x error."""
    respx.get(url__startswith=f"{BASE}/api/v1/profile.json").mock(
        return_value=httpx.Response(200, json=_profile_doc("mg/dl", 50))
    )
    try:
        isf_mmol, cr, dia, units = await _extract_profile_settings(client)
    finally:
        await client.aclose()
    assert units == "mg/dL"
    assert isf_mmol is not None and abs(isf_mmol - 2.78) < 0.05
    assert cr == 10.0  # carb ratio is unit-independent
    assert dia == 5.0


@pytest.mark.asyncio
@respx.mock
async def test_extract_profile_keeps_mmol_sens_as_is(
    client: NightscoutClient,
) -> None:
    respx.get(url__startswith=f"{BASE}/api/v1/profile.json").mock(
        return_value=httpx.Response(200, json=_profile_doc("mmol", 2.8))
    )
    try:
        isf_mmol, _cr, _dia, units = await _extract_profile_settings(client)
    finally:
        await client.aclose()
    assert units == "mmol"
    assert isf_mmol == 2.8
