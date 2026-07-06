"""Local mock of the Nightscout v1 REST API for testing this MCP end-to-end.

Serves the five endpoints the MCP touches — status, entries/sgv, treatments,
devicestatus, profile — over local HTTPS, honoring the `find[date][$gte]` /
`find[created_at][$lt]` / `count` query filters the client sends (including
its backward pagination). Data is either synthetic (default) or loaded from
JSON files you drop in a data dir (e.g. Open Humans exports).

Run it:

    uv run --extra dev-server python dev/mock_nightscout.py

It prints the exact env block to point the MCP at it. TLS uses a throwaway CA
generated with `trustme`; the MCP trusts it via NIGHTSCOUT_CA_BUNDLE (never by
disabling verification).
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from generate_data import generate  # noqa: E402

CERT_DIR = Path(__file__).parent / "certs"
DATA_DIR_DEFAULT = Path(__file__).parent / "data"
DEV_TOKEN = "dev-reader-token"


def _load_dataset(data_dir: Path, days: int) -> dict[str, Any]:
    """Load {entries,treatments,devicestatus,profile,status} from JSON files if
    present in `data_dir`, else generate a synthetic dataset."""
    files = {
        "entries": data_dir / "entries.json",
        "treatments": data_dir / "treatments.json",
        "devicestatus": data_dir / "devicestatus.json",
        "profile": data_dir / "profile.json",
        "status": data_dir / "status.json",
    }
    if files["entries"].exists():
        data: dict[str, Any] = {}
        for key, path in files.items():
            data[key] = json.loads(path.read_text()) if path.exists() else ([] if key != "status" else {})
        # Ensure newest-first ordering the client expects.
        data["entries"].sort(key=lambda r: r.get("date", 0), reverse=True)
        for key in ("treatments", "devicestatus"):
            data[key].sort(key=lambda r: r.get("created_at", ""), reverse=True)
        print(f"[mock] loaded dataset from {data_dir}")
        return data
    print(f"[mock] no files in {data_dir}; generating {days}d synthetic dataset")
    return generate(days=days)


def _filter_rows(rows: list[dict], params: dict[str, list[str]], field: str, as_int: bool) -> list[dict]:
    """Apply Nightscout find[field][$gte|$lt] + count to already-desc-sorted rows."""
    gte = params.get(f"find[{field}][$gte]", [None])[0]
    lt = params.get(f"find[{field}][$lt]", [None])[0]
    count = int(params.get("count", ["10"])[0])

    def keep(r: dict) -> bool:
        val = r.get(field)
        if val is None:
            return False
        if as_int:
            val = int(val)
            if gte is not None and val < int(gte):
                return False
            if lt is not None and val >= int(lt):
                return False
        else:
            if gte is not None and str(val) < gte:
                return False
            if lt is not None and str(val) >= lt:
                return False
        return True

    return [r for r in rows if keep(r)][:count]


class _Handler(BaseHTTPRequestHandler):
    dataset: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # quieter than the default
        pass

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        ds = self.dataset

        if path == "/api/v1/status.json":
            self._send(ds["status"])
        elif path in ("/api/v1/entries/sgv.json", "/api/v1/entries.json"):
            self._send(_filter_rows(ds["entries"], params, "date", as_int=True))
        elif path == "/api/v1/treatments.json":
            self._send(_filter_rows(ds["treatments"], params, "created_at", as_int=False))
        elif path == "/api/v1/devicestatus.json":
            self._send(_filter_rows(ds["devicestatus"], params, "created_at", as_int=False))
        elif path == "/api/v1/profile.json":
            self._send(ds["profile"])
        else:
            self._send({"error": f"unmocked path {path}"}, status=404)


def _ensure_certs() -> tuple[Path, Path]:
    """Return (ca_pem, server_pem), generating them with trustme if missing."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    ca_pem = CERT_DIR / "ca.pem"
    server_pem = CERT_DIR / "server.pem"
    if ca_pem.exists() and server_pem.exists():
        return ca_pem, server_pem
    try:
        import trustme
    except ImportError:
        sys.exit("trustme is required — run with: uv run --extra dev-server python dev/mock_nightscout.py")
    ca = trustme.CA()
    cert = ca.issue_cert("localhost", "127.0.0.1")
    ca.cert_pem.write_to_path(str(ca_pem))
    cert.private_key_and_cert_chain_pem.write_to_path(str(server_pem))
    print(f"[mock] generated dev CA + cert in {CERT_DIR}")
    return ca_pem, server_pem


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock Nightscout v1 REST API over local HTTPS.")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--days", type=int, default=30, help="days of synthetic data (ignored if data dir has files)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    _Handler.dataset = _load_dataset(args.data_dir, args.days)
    ca_pem, server_pem = _ensure_certs()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(server_pem))

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    url = f"https://localhost:{args.port}"
    n = len(_Handler.dataset["entries"])
    print(f"\n[mock] Nightscout mock serving {n} SGV rows at {url}")
    print("[mock] point the MCP at it with:\n")
    print(f'  export NIGHTSCOUT_URL="{url}"')
    print(f'  export NIGHTSCOUT_TOKEN="{DEV_TOKEN}"')
    print(f'  export NIGHTSCOUT_CA_BUNDLE="{ca_pem}"')
    print('  export NIGHTSCOUT_UNITS="mmol/L"')
    print("\n[mock] Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] stopped.")


if __name__ == "__main__":
    main()
