"""Glucose unit conversion + trend-direction mapping.

Nightscout stores glucose in mg/dL regardless of the user's display
preference, so all conversions are "from mg/dL". The precise molecular
factor is 18.0156, but every diabetes display app (Nightscout, Dexcom,
LibreLink) uses 18 by convention — that's what produces the canonical
"100 mg/dL = 5.6 mmol/L" mapping users expect. We follow the convention.

- mg/dL: integer
- mmol/L: one decimal place

Trend directions come from CGM device output: Nightscout normalizes them
to a small set of strings. We map each to a single Unicode arrow that
LLMs and humans both parse instantly.
"""

from __future__ import annotations

from typing import Final

# Conventional display factor (not the precise molecular factor).
# Matches Nightscout, Dexcom, and LibreLink display behavior.
MG_DL_PER_MMOL: Final[float] = 18.0


def mgdl_to_mmol(value: float | int) -> float:
    """mg/dL → mmol/L, rounded to one decimal."""
    return round(value / MG_DL_PER_MMOL, 1)


def mmol_to_mgdl(value: float) -> int:
    """mmol/L → mg/dL, rounded to nearest integer."""
    return round(value * MG_DL_PER_MMOL)


# Direction strings come from CGM transmitter / Nightscout normalization.
# Source: the entry schema in lib/api/entries and the dexcom transmitter
# value space. NONE / NOT COMPUTABLE / "" all collapse to "?".
DIRECTION_TO_ARROW: Final[dict[str, str]] = {
    "DoubleUp": "↑↑",
    "SingleUp": "↑",
    "FortyFiveUp": "↗",
    "Flat": "→",
    "FortyFiveDown": "↘",
    "SingleDown": "↓",
    "DoubleDown": "↓↓",
    "NONE": "?",
    "NOT COMPUTABLE": "?",
    "RATE OUT OF RANGE": "?",
}


def direction_to_arrow(direction: str | None) -> str:
    """Map a Nightscout direction string to a single-glyph arrow."""
    if not direction:
        return "?"
    return DIRECTION_TO_ARROW.get(direction, "?")
