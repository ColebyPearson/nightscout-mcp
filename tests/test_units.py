"""Unit conversion + trend arrow tests. Pure math, no network."""

from __future__ import annotations

import pytest

from nightscout_mcp.units import (
    DIRECTION_TO_ARROW,
    direction_to_arrow,
    mgdl_to_mmol,
    mmol_to_mgdl,
)


@pytest.mark.parametrize(
    "mgdl,expected_mmol",
    [
        (180, 10.0),
        (100, 5.6),
        (54, 3.0),
        (270, 15.0),
        (0, 0.0),
    ],
)
def test_mgdl_to_mmol_rounds_to_one_decimal(mgdl: int, expected_mmol: float) -> None:
    assert mgdl_to_mmol(mgdl) == expected_mmol


def test_mmol_to_mgdl_rounds_to_integer() -> None:
    assert mmol_to_mgdl(5.6) == 101
    assert mmol_to_mgdl(10.0) == 180


def test_roundtrip_stable_within_tolerance() -> None:
    # mg/dL → mmol/L → mg/dL should be within ±1 mg/dL of original.
    for mgdl in [70, 100, 140, 180, 250]:
        assert abs(mmol_to_mgdl(mgdl_to_mmol(mgdl)) - mgdl) <= 1


@pytest.mark.parametrize(
    "direction,expected",
    [
        ("Flat", "→"),
        ("SingleUp", "↑"),
        ("DoubleUp", "↑↑"),
        ("FortyFiveUp", "↗"),
        ("SingleDown", "↓"),
        ("DoubleDown", "↓↓"),
        ("FortyFiveDown", "↘"),
        ("NONE", "?"),
        ("NOT COMPUTABLE", "?"),
        ("", "?"),
        (None, "?"),
        ("unknown-future-value", "?"),
    ],
)
def test_direction_to_arrow(direction: str | None, expected: str) -> None:
    assert direction_to_arrow(direction) == expected


def test_every_known_direction_has_a_single_glyph_or_question() -> None:
    # Every value in the map should be a 1-2 character glyph — no English words.
    for arrow in DIRECTION_TO_ARROW.values():
        assert 1 <= len(arrow) <= 2
