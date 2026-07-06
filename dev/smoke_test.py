"""End-to-end smoke test: run the MCP's tools against the in-process mock.

Boots the mock Nightscout over local HTTPS in a background thread, points a real
NightscoutClient at it (real TLS, real query filters, real pagination — things
the respx unit tests only approximate), and calls a representative tool from
each category, printing a one-line result. A fast "does the whole thing work
against realistic data?" check.

    uv run --extra dev-server python dev/smoke_test.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_nightscout import DEV_TOKEN, _ensure_certs, _Handler, _load_dataset  # noqa: E402

PORT = 8443


def _start_server() -> ThreadingHTTPServer:
    _Handler.dataset = _load_dataset(Path(__file__).parent / "data", days=30)
    _ca, server_pem = _ensure_certs()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(server_pem))
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def _run() -> int:
    ca_pem, _ = _ensure_certs()
    os.environ["NIGHTSCOUT_URL"] = f"https://localhost:{PORT}"
    os.environ["NIGHTSCOUT_TOKEN"] = DEV_TOKEN
    os.environ["NIGHTSCOUT_CA_BUNDLE"] = str(ca_pem)
    os.environ["NIGHTSCOUT_UNITS"] = "mmol/L"

    from nightscout_mcp.client import NightscoutClient
    from nightscout_mcp.config import load_settings
    from nightscout_mcp.tools import analytics, metrics, read

    client = NightscoutClient(load_settings())

    class _Reg:
        def __init__(self) -> None:
            self.tools: dict = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn

            return deco

    reg = _Reg()
    for mod in (read, analytics, metrics):
        mod.register(reg, lambda: client)

    checks = [
        ("health_check", lambda: reg.tools["health_check"]()),
        ("get_current_glucose", lambda: reg.tools["get_current_glucose"]()),
        ("get_glucose_stats", lambda: reg.tools["get_glucose_stats"](hours=24 * 14)),
        ("detect_patterns", lambda: reg.tools["detect_patterns"](days=14)),
        ("insulin_sensitivity_check", lambda: reg.tools["insulin_sensitivity_check"](days=14)),
        ("hypoglycemia_episodes", lambda: reg.tools["hypoglycemia_episodes"](days=14)),
        ("glycemia_risk_index", lambda: reg.tools["glycemia_risk_index"](days=14)),
        ("data_sufficiency_report", lambda: reg.tools["data_sufficiency_report"](days=14)),
        ("consensus_target_audit", lambda: reg.tools["consensus_target_audit"](days=14)),
        ("clinic_packet", lambda: reg.tools["clinic_packet"](days=14)),
    ]

    failures = 0
    try:
        for name, call in checks:
            try:
                result = await call()
                print(f"  [OK]   {name:<28} {_summarize(name, result)}")
            except Exception as exc:  # noqa: BLE001 — smoke test wants the message
                failures += 1
                print(f"  [FAIL] {name:<28} {type(exc).__name__}: {exc}")
    finally:
        await client.aclose()
    return failures


def _summarize(name: str, r: object) -> str:
    if name == "health_check":
        return f"reachable={getattr(r, 'reachable', r)}"
    if name == "get_current_glucose":
        return f"{getattr(r, 'sgv_mgdl', '?')} mg/dL, {getattr(r, 'trend_arrow', '')}"
    if name == "get_glucose_stats":
        return f"TIR {getattr(r, 'tir_percent', '?')}% GMI {getattr(r, 'gmi_percent', '?')}%"
    if name == "detect_patterns":
        return f"{len(getattr(r, 'patterns', []))} patterns"
    if name == "insulin_sensitivity_check":
        return f"n={getattr(r, 'sample_count', '?')} derived={getattr(r, 'derived_isf_mmol_per_unit', '?')}"
    if name == "hypoglycemia_episodes":
        return f"{getattr(r, 'total_episodes', '?')} events, {getattr(r, 'level2_episodes', '?')} L2"
    if name == "glycemia_risk_index":
        return f"GRI {getattr(r, 'gri', '?')}"
    if name == "data_sufficiency_report":
        return f"{getattr(r, 'pct_active', '?')}% active, meets={getattr(r, 'meets_agp_consensus', '?')}"
    if name == "consensus_target_audit":
        return f"{getattr(r, 'summary_fail_count', '?')} unmet"
    if name == "clinic_packet":
        return f"{len(getattr(r, 'markdown_body', ''))} chars markdown"
    return "ok"


def main() -> None:
    # Windows consoles default to cp1252; the mock/markdown contain non-ASCII.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    _start_server()
    print("\n[smoke] mock up; running tools against it over HTTPS...\n")
    failures = asyncio.run(_run())
    print()
    if failures:
        print(f"[smoke] {failures} tool(s) failed.")
        raise SystemExit(1)
    print("[smoke] all tools ran end-to-end against the mock. ✓")


if __name__ == "__main__":
    main()
