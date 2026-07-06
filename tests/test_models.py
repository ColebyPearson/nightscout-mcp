"""Tests that pydantic models parse realistic Nightscout payloads."""

from __future__ import annotations

from nightscout_mcp.models import Sgv, Treatment, parse_iso_to_utc


def test_sgv_parses_native_camelcase_fields() -> None:
    raw = {
        "sgv": 142,
        "date": 1_716_400_000_000,
        "dateString": "2026-05-22T18:00:00.000Z",
        "direction": "FortyFiveUp",
        "type": "sgv",
        "device": "share2",
    }
    s = Sgv.model_validate(raw)
    assert s.sgv_mgdl == 142
    assert s.sgv_mmol == 7.9  # 142 / 18.0182
    assert s.trend_arrow == "↗"
    assert s.date_iso == "2026-05-22T18:00:00.000Z"
    assert s.device == "share2"


def test_sgv_synthesizes_iso_when_datestring_missing() -> None:
    # Some Nightscout uploaders only write `date` (Unix ms), not `dateString`.
    # Discovered against a personal Nightscout instance on 2026-05-22.
    raw = {
        "_id": "abc",
        "sgv": 142,
        "date": 1_779_496_549_458,
        "direction": "Flat",
        "type": "sgv",
        # NO dateString
    }
    s = Sgv.model_validate(raw)
    assert s.date_iso.startswith("20")  # synthesized
    assert s.date_iso.endswith("Z")
    assert s.sgv_mgdl == 142


def test_sgv_handles_missing_direction_gracefully() -> None:
    raw = {
        "sgv": 100,
        "date": 1_716_400_000_000,
        "dateString": "2026-05-22T18:00:00.000Z",
        "type": "sgv",
    }
    s = Sgv.model_validate(raw)
    assert s.direction is None
    assert s.trend_arrow == "?"


def test_treatment_parses_bolus_with_underscore_id() -> None:
    raw = {
        "_id": "abc123",
        "eventType": "Correction Bolus",
        "created_at": "2026-05-22T17:30:00.000Z",
        "insulin": 1.5,
        "notes": "lunch overshoot",
        "enteredBy": "loop",
    }
    t = Treatment.model_validate(raw)
    assert t.id == "abc123"
    assert t.event_type == "Correction Bolus"
    assert t.insulin == 1.5
    assert t.entered_by == "loop"


def test_treatment_ignores_unknown_fields() -> None:
    # NS often includes extra fields we don't model — should not error.
    raw = {
        "_id": "abc",
        "eventType": "Bolus",
        "created_at": "2026-05-22T17:30:00.000Z",
        "some_future_field": {"nested": True},
        "x_internal": [1, 2, 3],
    }
    t = Treatment.model_validate(raw)
    assert t.event_type == "Bolus"


def test_parse_iso_to_utc_handles_z_and_offset_variants() -> None:
    a = parse_iso_to_utc("2026-05-22T18:00:00.000Z")
    b = parse_iso_to_utc("2026-05-22T18:00:00.000+00:00")
    assert a == b
    assert a.tzinfo is not None


def test_recommendation_models_carry_clinician_review_flag() -> None:
    """Every payload that proposes a settings/dosing direction must ship the
    structural requires_clinician_review flag (default True) so a client can
    gate the advice behind care-team sign-off."""
    from nightscout_mcp.models import (
        CrDerivation,
        DiaFitResult,
        DynIsfRecommendation,
        EffectiveIsfDerivation,
        IsfDerivation,
    )

    isf = IsfDerivation(
        sample_count=5,
        derived_isf_mgdl_per_unit=50.0,
        derived_isf_mmol_per_unit=2.8,
        profile_isf_mmol_per_unit=2.0,
        ratio_derived_over_profile=1.4,
        confidence="medium",
        recommendation="Consider raising profile ISF.",
    )
    assert isf.requires_clinician_review is True
    assert isf.model_dump()["requires_clinician_review"] is True  # serialized

    cr = CrDerivation(
        sample_count=5,
        derived_cr_g_per_unit=10.0,
        profile_cr_g_per_unit=12.0,
        ratio_derived_over_profile=0.83,
        avg_end_minus_pre_mgdl=-20.0,
        confidence="medium",
        recommendation="Consider raising CR.",
    )
    assert cr.requires_clinician_review is True

    eff = EffectiveIsfDerivation(
        sample_count=5,
        devicestatus_rows_examined=100,
        samples_without_sens=2,
        avg_effective_isf_mmol_per_unit=3.0,
        avg_realized_isf_mmol_per_unit=3.5,
        overall_ratio_realized_over_effective=1.17,
        confidence="medium",
        by_bg_band=[],
        recommendation="AAPS appears to over-dose.",
    )
    assert eff.requires_clinician_review is True

    dia = DiaFitResult(
        sample_count=20,
        best_dia_hours=5.5,
        best_peak_min=55.0,
        rmse=0.1,
        profile_dia_hours=5.0,
        recommendation_text="Trial DIA 5.5h.",
        caveat_text="Exploratory.",
    )
    assert dia.requires_clinician_review is True

    dyn = DynIsfRecommendation(
        current_af=30,
        recommended_af=25,
        recommendation_type="lower_af",
        overall_ratio=1.2,
        in_target_ratio=1.1,
        above_target_ratio=1.3,
        confidence="medium",
        sample_count=25,
        reasoning="Over-dosing above target.",
        caveat_text="Discuss with care team.",
    )
    assert dyn.requires_clinician_review is True
