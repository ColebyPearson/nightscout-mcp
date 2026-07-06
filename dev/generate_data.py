"""Synthetic Nightscout dataset generator for local testing.

Produces entries (SGV), treatments (boluses/carbs/profile switches),
devicestatus (AAPS openaps.suggested), a profile, and a status doc — all in
the exact JSON shapes the Nightscout v1 REST API returns, so the MCP's tools
run end-to-end against realistic data without a real instance or PHI.

A tiny physiological model (bg / iob / cob with dawn effect, meals, corrections,
and a few injected overnight lows) makes the analytics tools actually surface
signals: dawn phenomenon, post-meal spikes, correction outcomes, hypo events.

Deterministic given a seed. Not medically meaningful — synthetic.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

# Profile the synthetic patient is looping on.
PROFILE_ISF_MMOL = 2.8  # ~50 mg/dL per U
PROFILE_CR = 10.0  # g per U
PROFILE_DIA = 5.0
PROFILE_TZ = "America/New_York"
TARGET_MGDL = 110.0
ISF_MGDL = 50.0  # true correction factor used by the model


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _trend(delta: float) -> str:
    if delta > 15:
        return "DoubleUp"
    if delta > 6:
        return "SingleUp"
    if delta > 2:
        return "FortyFiveUp"
    if delta < -15:
        return "DoubleDown"
    if delta < -6:
        return "SingleDown"
    if delta < -2:
        return "FortyFiveDown"
    return "Flat"


def generate(days: int = 30, seed: int = 42, end: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return {entries, treatments, devicestatus, profile, status} lists/docs."""
    rng = random.Random(seed)
    end = (end or datetime.now(UTC)).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)

    entries: list[dict[str, Any]] = []
    treatments: list[dict[str, Any]] = []
    devicestatus: list[dict[str, Any]] = []

    step_min = 5
    baseline = 130.0

    def alpha(dt_min: float, tau: float) -> float:
        """Impulse that rises then returns to 0; peaks at 1 when dt==tau."""
        if dt_min < 0:
            return 0.0
        x = dt_min / tau
        return x * math.exp(1 - x)

    def plateau(dt_min: float, rise: float, hold: float) -> float:
        """0 before the event, smoothstep up to 1 over `rise`, hold, then fall."""
        if dt_min < 0:
            return 0.0
        if dt_min < rise:
            x = dt_min / rise
            return x * x * (3 - 2 * x)
        if dt_min < rise + hold:
            return 1.0
        x = min(1.0, (dt_min - rise - hold) / rise)
        return 1.0 - x * x * (3 - 2 * x)

    # --- Pre-schedule events (positive = raises BG, drop = lowers BG) --------
    # Each: (unix_seconds, peak_mgdl, kind). Effects are summed onto a baseline.
    rises: list[tuple[float, float, float]] = []  # (ts, peak, tau) alpha up
    drops: list[tuple[float, float, float, float]] = []  # (ts, mag, rise, hold) plateau down
    low_nights = {rng.randrange(days) for _ in range(max(1, days // 5))}

    for d in range(days):
        day0 = start + timedelta(days=d)

        def at(local_h: int, local_m: int, base: datetime = day0) -> datetime:
            # local (UTC-5) -> UTC
            return base + timedelta(hours=local_h + 5, minutes=local_m)

        # Meals: net post-meal excursion (already bolus-adjusted). Occasionally
        # under-bolused -> a big >50 spike that detect_patterns should catch.
        for h, m, carbs in ((7, 30, 45), (12, 30, 60), (18, 30, 55)):
            mt = at(h, m)
            under = rng.random() < 0.35
            peak = rng.uniform(75, 100) if under else rng.uniform(35, 60)
            rises.append((mt.timestamp(), peak, 55))
            units = round(carbs / PROFILE_CR * rng.uniform(0.9, 1.05), 2)
            treatments.append(
                {
                    "_id": f"tx-meal-{int(mt.timestamp())}",
                    "eventType": "Meal Bolus",
                    "created_at": _iso(mt),
                    "carbs": carbs,
                    "insulin": units,
                    "enteredBy": "AAPS",
                }
            )

        # Isolated afternoon correction (~15:00) on ~half of days: an unlogged
        # rise pushes BG high, a correction bolus (no carbs +/-60min) brings it
        # back down by ~ISF*U, giving insulin_sensitivity_check a clean signal.
        if rng.random() < 0.5:
            rt = at(15, 0)
            rises.append((rt.timestamp(), rng.uniform(85, 105), 60))  # afternoon rise
            ct = at(15, 20)
            pre = baseline + 95
            units = round((pre - TARGET_MGDL) / ISF_MGDL * rng.uniform(0.95, 1.05), 2)
            drops.append((ct.timestamp(), ISF_MGDL * units, 90, 60))
            treatments.append(
                {
                    "_id": f"tx-corr-{int(ct.timestamp())}",
                    "eventType": "Correction Bolus",
                    "created_at": _iso(ct),
                    "insulin": units,
                    "enteredBy": "AAPS",
                }
            )

        # Overnight low on some nights: an extra dinner-insulin drop reaching ~01:30.
        if d in low_nights:
            lt = at(1, 30, base=day0 + timedelta(days=1))
            drops.append((lt.timestamp(), rng.uniform(70, 95), 70, 40))

    # --- Render the SGV trace + devicestatus --------------------------------
    prev_bg = baseline
    t = start
    while t < end:
        local_hour = (t.hour - 5) % 24
        ts = t.timestamp()
        dawn = 22.0 * max(0.0, math.sin(math.pi * (local_hour - 3) / 4)) if 3 <= local_hour < 7 else 0.0

        bg = baseline + dawn + rng.gauss(0, 6)
        for ets, peak, tau in rises:
            dt = (ts - ets) / 60
            if -1 < dt < tau * 5:
                bg += peak * alpha(dt, tau)
        for ets, mag, rise, hold in drops:
            dt = (ts - ets) / 60
            if -1 < dt < rise * 2 + hold + 5:
                bg -= mag * plateau(dt, rise, hold)
        bg = max(40.0, min(360.0, bg))

        entries.append(
            {
                "_id": f"sgv-{int(ts)}",
                "sgv": int(round(bg)),
                "date": int(ts * 1000),
                "dateString": _iso(t),
                "direction": _trend(bg - prev_bg),
                "type": "sgv",
                "device": "synthetic://dev",
            }
        )
        if t.minute % 15 == 0:
            devicestatus.append(
                {
                    "_id": f"ds-{int(ts)}",
                    "created_at": _iso(t),
                    "device": "AAPS",
                    "openaps": {
                        "suggested": {
                            "sensitivityRatio": round(rng.uniform(0.9, 1.1), 2),
                            "variable_sens": round(ISF_MGDL * rng.uniform(0.9, 1.2), 1),
                            "sens": round(ISF_MGDL, 1),
                            "IOB": round(max(0.0, rng.gauss(1.0, 0.6)), 2),
                            "COB": round(max(0.0, rng.gauss(5.0, 8.0)), 1),
                        }
                    },
                    "pump": {"reservoir": round(rng.uniform(20, 200), 1), "battery": {"percent": rng.randint(20, 100)}},
                }
            )

        prev_bg = bg
        t += timedelta(minutes=step_min)

    # One profile-switch event mid-window (gives change-attribution something).
    switch_at = start + timedelta(days=days // 2)
    treatments.append(
        {
            "_id": f"tx-switch-{int(switch_at.timestamp())}",
            "eventType": "Profile Switch",
            "created_at": _iso(switch_at),
            "profile": "Default",
            "notes": "regular (110%)",
            "enteredBy": "AAPS",
        }
    )

    profile = [
        {
            "_id": "profile-1",
            "defaultProfile": "Default",
            "startDate": _iso(start),
            "store": {
                "Default": {
                    "units": "mmol",
                    "dia": PROFILE_DIA,
                    "timezone": PROFILE_TZ,
                    "sens": [{"time": "00:00", "timeAsSeconds": 0, "value": PROFILE_ISF_MMOL}],
                    "carbratio": [{"time": "00:00", "timeAsSeconds": 0, "value": PROFILE_CR}],
                    "basal": [{"time": "00:00", "timeAsSeconds": 0, "value": 0.8}],
                    "target_low": [{"time": "00:00", "value": 5.5}],
                    "target_high": [{"time": "00:00", "value": 7.8}],
                }
            },
        }
    ]

    status = {
        "status": "ok",
        "name": "nightscout",
        "version": "15.0.3",
        "settings": {"units": "mmol", "timeFormat": 24},
    }

    # Newest-first, matching Nightscout's default ordering.
    entries.sort(key=lambda r: r["date"], reverse=True)
    treatments.sort(key=lambda r: r["created_at"], reverse=True)
    devicestatus.sort(key=lambda r: r["created_at"], reverse=True)

    return {
        "entries": entries,
        "treatments": treatments,
        "devicestatus": devicestatus,
        "profile": profile,
        "status": status,
    }
