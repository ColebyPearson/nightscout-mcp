"""Download a member's Nightscout data from Open Humans into dev/data/.

Open Humans stores each member's uploaded files behind the direct-sharing API.
This fetches the file list for the member who authorized your access token,
picks the Nightscout ones, downloads them, and normalizes whatever shape they
arrive in (separate collection files, a combined JSON, ndjson, or a zip) into
the five files the local mock server expects:
    dev/data/{entries,treatments,devicestatus,profile,status}.json

Getting a token: authorize an Open Humans OAuth2 project against your account
and use the resulting member access token (see the OH "On Your Own Data" /
direct-sharing docs). Then:

    export OPEN_HUMANS_TOKEN=...        # or pass --token
    uv run --extra dev-server python dev/openhumans_download.py --list      # preview
    uv run --extra dev-server python dev/openhumans_download.py             # download + normalize
    uv run --extra dev-server python dev/mock_nightscout.py                 # serve it

This talks to the real OH API and pulls **donated human data** — keep it in
dev/data/ (gitignored), never commit it, and honor the data-use agreement.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx

OH_EXCHANGE_URL = "https://www.openhumans.org/api/direct-sharing/project/exchange-member/"
DATA_DIR = Path(__file__).parent / "data"
RAW_DIR = DATA_DIR / "raw"
CANONICAL = ("entries", "treatments", "devicestatus", "profile", "status")


# --- Open Humans API --------------------------------------------------------


def list_member_files(token: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Return the member's data-file records (each has basename + download_url)."""
    resp = httpx.get(OH_EXCHANGE_URL, params={"access_token": token}, timeout=timeout)
    if resp.status_code == 401:
        raise SystemExit("Open Humans rejected the token (401). Check OPEN_HUMANS_TOKEN.")
    resp.raise_for_status()
    return resp.json().get("data", [])


def looks_like_nightscout(record: dict[str, Any]) -> bool:
    """Heuristic: match by source, basename, or metadata tags."""
    hay = " ".join(str(record.get(k, "")).lower() for k in ("source", "basename"))
    tags = " ".join(str(t).lower() for t in (record.get("metadata", {}) or {}).get("tags", []))
    hay = f"{hay} {tags}"
    if "nightscout" in hay or "cgm" in hay:
        return True
    base = str(record.get("basename", "")).lower()
    return any(base.startswith(c) or base == f"{c}.json" for c in CANONICAL)


def download_bytes(url: str, timeout: float = 120.0) -> bytes:
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


# --- Normalization (pure; offline-testable) ---------------------------------


def _infer_collection(basename: str, rows: list[dict[str, Any]]) -> str | None:
    """Map a file to one of the canonical collections by name, then by content."""
    b = basename.lower()
    for c in CANONICAL:
        if c in b:
            return c
    sample = next((r for r in rows if isinstance(r, dict)), None)
    if sample is None:
        return None
    if "sgv" in sample or sample.get("type") == "sgv":
        return "entries"
    if "eventType" in sample or "created_at" in sample and "insulin" in sample:
        return "treatments"
    if "openaps" in sample or "pump" in sample or "uploader" in sample:
        return "devicestatus"
    if "store" in sample or "defaultProfile" in sample:
        return "profile"
    return None


def _parse_json_or_ndjson(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ndjson: one JSON object per line, skipping any unparseable lines.
    out: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(stripped))
    return out


def normalize_dataset(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Turn arbitrary Nightscout export files into the canonical five collections.

    Accepts (basename, raw_bytes) pairs; unpacks .zip members; handles separate
    collection files, a combined `{entries:[...], treatments:[...]}` dict, and
    ndjson. Returns {collection: list-or-dict}. `status` is synthesized from the
    profile units if the export didn't include one.
    """
    result: dict[str, Any] = {}

    def ingest(basename: str, raw: bytes) -> None:
        if basename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    if not name.endswith("/"):
                        ingest(Path(name).name, zf.read(name))
            return
        parsed = _parse_json_or_ndjson(raw)
        # A combined dict keyed by collection name.
        if isinstance(parsed, dict) and any(k in parsed for k in CANONICAL):
            for c in CANONICAL:
                if c in parsed and c not in result:
                    result[c] = parsed[c]
            return
        # A single profile document (dict with a store).
        if isinstance(parsed, dict) and ("store" in parsed or "defaultProfile" in parsed):
            result.setdefault("profile", [parsed])
            return
        if isinstance(parsed, list):
            coll = _infer_collection(basename, parsed)
            if coll and coll not in result:
                result[coll] = parsed

    for basename, raw in files:
        ingest(basename, raw)

    # Synthesize a status doc if absent, using profile display units.
    if "status" not in result:
        units = "mg/dl"
        prof = result.get("profile")
        if isinstance(prof, list) and prof:
            store = prof[0].get("store") or {}
            default = store.get(prof[0].get("defaultProfile", "Default"), {})
            units = default.get("units", units)
        result["status"] = {"status": "ok", "name": "openhumans", "version": "15.0.0", "settings": {"units": units}}

    return result


def write_dataset(data: dict[str, Any], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for coll in CANONICAL:
        if coll in data:
            path = out_dir / f"{coll}.json"
            path.write_text(json.dumps(data[coll]))
            n = len(data[coll]) if isinstance(data[coll], list) else 1
            written.append(f"{path.name} ({n} record{'s' if n != 1 else ''})")
    return written


# --- CLI --------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Download Nightscout data from Open Humans into dev/data/.")
    ap.add_argument("--token", default=os.environ.get("OPEN_HUMANS_TOKEN"))
    ap.add_argument("--list", action="store_true", help="list the member's files and exit")
    ap.add_argument("--all", action="store_true", help="download every file, not just Nightscout-looking ones")
    ap.add_argument("--keep-raw", action="store_true", help="keep the raw downloads in dev/data/raw/")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("Set OPEN_HUMANS_TOKEN or pass --token. See the module docstring for how to get one.")

    records = list_member_files(args.token)
    if not records:
        raise SystemExit("No data files available for this member/token.")

    if args.list:
        print(f"{len(records)} file(s) for this member:\n")
        for r in records:
            mark = "  * " if looks_like_nightscout(r) else "    "
            print(f"{mark}{r.get('basename', '?'):<28} source={r.get('source', '?')}")
        print("\n( * = looks like Nightscout data )")
        return

    chosen = records if args.all else [r for r in records if looks_like_nightscout(r)]
    if not chosen:
        raise SystemExit(
            "No Nightscout-looking files found. Re-run with --list to inspect, or --all to grab everything."
        )

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, bytes]] = []
    for r in chosen:
        base = r.get("basename", f"file-{r.get('id', 'x')}")
        url = r.get("download_url")
        if not url:
            continue
        print(f"[oh] downloading {base} ...")
        raw = download_bytes(url)
        files.append((base, raw))
        if args.keep_raw:
            (RAW_DIR / base).write_bytes(raw)

    data = normalize_dataset(files)
    written = write_dataset(data, DATA_DIR)
    if not written:
        raise SystemExit(
            "Downloaded files but couldn't recognize any Nightscout collections. "
            "Try --keep-raw and inspect dev/data/raw/."
        )
    print("\n[oh] wrote to dev/data/:")
    for w in written:
        print(f"  - {w}")
    print("\n[oh] now serve it:  uv run --extra dev-server python dev/mock_nightscout.py")


if __name__ == "__main__":
    sys.exit(main())
