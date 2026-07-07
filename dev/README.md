# Local test harness — mock Nightscout

Exercise the MCP end-to-end without a real Nightscout instance or any PHI. A
small server speaks the Nightscout v1 REST API (over local HTTPS, honoring the
`find[...]`/`count` query filters and backward pagination the client uses) and
serves either synthetic data or JSON files you provide.

Nothing here ships in the package — it's under the `dev-server` optional extra.

## Quick start

```bash
uv sync --extra dev-server

# One-shot end-to-end check: boots the mock in-process and runs a tool from
# each category against it over real TLS.
uv run --extra dev-server python dev/smoke_test.py
```

Expected tail:

```
  [OK]   health_check                 reachable=... ok=True
  [OK]   get_current_glucose          165 mg/dL, ↗
  [OK]   get_glucose_stats            TIR 82.2% GMI 6.88%
  ...
[smoke] all tools ran end-to-end against the mock. ✓
```

## Run the mock as a standalone server

```bash
uv run --extra dev-server python dev/mock_nightscout.py           # 30d synthetic
uv run --extra dev-server python dev/mock_nightscout.py --days 14 --port 8443
```

It prints the exact env block to point the MCP at it, e.g.:

```
export NIGHTSCOUT_URL="https://localhost:8443"
export NIGHTSCOUT_TOKEN="dev-reader-token"
export NIGHTSCOUT_CA_BUNDLE="…/dev/certs/ca.pem"
export NIGHTSCOUT_UNITS="mmol/L"
```

Then run the MCP (`uv run nightscout-mcp`, MCP Inspector, or add it to Claude
with those env vars). `NIGHTSCOUT_CA_BUNDLE` is a real config option — it points
httpx's TLS verification at a CA bundle (here the mock's throwaway CA; in
production, a self-hosted instance's private CA). It never disables verification.

## Using real data (e.g. Open Humans / OpenAPS Data Commons)

Drop the raw Nightscout collections as JSON into `dev/data/` (gitignored):

```
dev/data/entries.json        # list of SGV docs   (sgv, date, dateString, type)
dev/data/treatments.json     # list of treatments (eventType, created_at, insulin, carbs)
dev/data/devicestatus.json   # list of devicestatus docs (openaps.suggested.…)
dev/data/profile.json        # profile list (store/Default/{sens,carbratio,dia,units,timezone})
dev/data/status.json         # {status, version, settings:{units}}
```

If `entries.json` is present the mock serves your files instead of synthetic
data. **This is donated human data** — keep it in `dev/data/` (gitignored), never
commit it, and honor the source's data-use agreement. Don't paste it into an LLM.

### Auto-fetch from Open Humans

`dev/openhumans_download.py` pulls a member's Nightscout files straight into
`dev/data/`. You need an Open Humans **member access token** — authorize an
Open Humans OAuth2 project against your account and use the token it issues (see
the OH direct-sharing / "on your own data" docs).

```bash
export OPEN_HUMANS_TOKEN=...
uv run --extra dev-server python dev/openhumans_download.py --list   # preview files (* = looks like Nightscout)
uv run --extra dev-server python dev/openhumans_download.py          # download Nightscout files + normalize
uv run --extra dev-server python dev/mock_nightscout.py              # serve them
```

It downloads the Nightscout-looking files, normalizes whatever shape they arrive
in (separate collection files, a combined JSON, ndjson, or a zip) into the five
canonical `dev/data/*.json` files, and synthesizes a `status.json` from the
profile units if the export lacks one. Flags: `--list` (preview only), `--all`
(grab every file, not just Nightscout-looking), `--keep-raw` (retain the raw
downloads under `dev/data/raw/` for inspection).

Same PHI rules apply — everything lands in gitignored `dev/data/`.

## Notes

- Synthetic data is deterministic (seeded) and **not medically meaningful** — it's
  shaped only to make the analytics tools surface signals (dawn phenomenon,
  post-meal spikes, isolated corrections, a few overnight lows).
- `dev/certs/` and `dev/data/` are gitignored.
