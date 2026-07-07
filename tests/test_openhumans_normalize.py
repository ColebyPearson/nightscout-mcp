"""Offline tests for the Open Humans data normalizer (dev/openhumans_download).

The network path (token exchange + download) needs a real Open Humans token and
isn't tested here; the normalization logic — which handles the varied export
shapes — is pure and fully covered.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dev"))

from openhumans_download import normalize_dataset  # noqa: E402


def _b(obj: object) -> bytes:
    return json.dumps(obj).encode()


ENTRY = {"sgv": 142, "date": 1_716_400_000_000, "dateString": "2026-05-22T18:00:00.000Z", "type": "sgv"}
TX = {"eventType": "Meal Bolus", "created_at": "2026-05-22T12:30:00.000Z", "insulin": 6.0, "carbs": 60}
DS = {"created_at": "2026-05-22T12:30:00.000Z", "openaps": {"suggested": {"sens": 50}}}
PROFILE = {"defaultProfile": "Default", "store": {"Default": {"units": "mmol", "dia": 5.0}}}


def test_normalizes_separate_collection_files() -> None:
    files = [
        ("entries.json", _b([ENTRY])),
        ("treatments.json", _b([TX])),
        ("devicestatus.json", _b([DS])),
        ("profile.json", _b([PROFILE])),
    ]
    data = normalize_dataset(files)
    assert data["entries"] == [ENTRY]
    assert data["treatments"] == [TX]
    assert data["devicestatus"] == [DS]
    # status synthesized from profile display units.
    assert data["status"]["settings"]["units"] == "mmol"


def test_normalizes_combined_dict_file() -> None:
    combined = {"entries": [ENTRY], "treatments": [TX], "profile": [PROFILE]}
    data = normalize_dataset([("nightscout-export.json", _b(combined))])
    assert data["entries"] == [ENTRY]
    assert data["treatments"] == [TX]


def test_infers_collection_from_content_when_name_is_generic() -> None:
    # A generic filename whose rows are clearly SGVs -> entries.
    data = normalize_dataset([("datafile-9931.json", _b([ENTRY, ENTRY]))])
    assert data["entries"] == [ENTRY, ENTRY]


def test_unpacks_zip_archive() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export/entries.json", json.dumps([ENTRY]))
        zf.writestr("export/treatments.json", json.dumps([TX]))
    data = normalize_dataset([("nightscout.zip", buf.getvalue())])
    assert data["entries"] == [ENTRY]
    assert data["treatments"] == [TX]


def test_parses_ndjson_entries() -> None:
    ndjson = ("\n".join(json.dumps(ENTRY) for _ in range(3))).encode()
    data = normalize_dataset([("entries.json", ndjson)])
    assert len(data["entries"]) == 3


def test_single_profile_document_recognized() -> None:
    data = normalize_dataset([("profile.json", _b(PROFILE))])
    assert data["profile"] == [PROFILE]
    assert data["status"]["settings"]["units"] == "mmol"


def test_status_defaults_to_mgdl_without_profile() -> None:
    data = normalize_dataset([("entries.json", _b([ENTRY]))])
    assert data["status"]["settings"]["units"] == "mg/dl"
