# nightscout-mcp

> A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM (Claude Desktop, Claude Code, any MCP client) read glucose, treatments, and derived analytics from a personal [Nightscout](https://github.com/nightscout/cgm-remote-monitor) instance.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![CI](https://github.com/ColebyPearson/nightscout-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ColebyPearson/nightscout-mcp/actions/workflows/ci.yml)
[![MCP](https://img.shields.io/badge/MCP-compatible-9cf)](https://modelcontextprotocol.io)

## ⚠️ Not a medical device

This tool reads CGM data and surfaces it to an LLM. **It is not a medical device, does not provide clinical advice, and must not be used to make treatment decisions.** Nightscout itself states it "currently makes no attempt at HIPAA privacy compliance" and is intended for educational use only. Use at your own risk.

## What you get

**31 read-only MCP tools** spanning live data, history, and analytics. The LLM can ask things like *"What's my current BG and how much insulin is on board?"* or *"Has the dawn phenomenon hit me on more than half the mornings this week?"* and get back structured answers backed by real Nightscout queries.

### Read tools (10)

| Tool | Returns |
|---|---|
| `health_check` | NS reachability + version + units |
| `get_current_glucose` | Latest SGV + trend arrow + freshness + delta vs prior |
| `get_glucose_history` | Time-series SGVs over a window |
| `get_glucose_stats` | TIR / TBR / TAR / SD / CV / GMI (A1C estimate) over a window |
| `get_treatments` | Boluses / carbs / basals / notes |
| `get_iob_cob` | Insulin- and carbs-on-board from `devicestatus` |
| `get_current_profile` | Basal schedule / ISF / CR / targets / DIA / timezone |
| `get_device_status` | Pump / loop / uploader state (tiered priority for loop data) |
| `get_server_status` | Nightscout version, status, configured units |
| `search_treatments` | Free-form substring across notes / event types |

### Analytics tools (10)

| Tool | Returns |
|---|---|
| `get_daily_report` | One-day stats + treatment totals + filtered user notes |
| `compare_periods` | Side-by-side stats with plain-English delta summary |
| `analyze_meal` | Pre-meal BG / peak / time-to-peak / rise / recovery / notes |
| `overnight_analysis` | Drift / min/max / time-below / dawn rise / flatness |
| `detect_patterns` | Recurring overnight lows, dawn phenomenon, post-meal spikes |
| `insulin_sensitivity_check` | **Real-world ISF derived from correction-bolus outcomes** + comparison to profile |
| `effective_isf_check` | Real-world ISF compared against AAPS Dynamic ISF (per-cycle `variable_sens`), stratified by pre-bolus BG band |
| `carb_ratio_check` | Real-world CR derived from meal-bolus outcomes + comparison to profile |
| `glucose_at_time` | Single point-in-time SGV with ±N-min tolerance |
| `compression_low_analysis` | Suspected sensor-compression artifacts (false lows) |

### Research-grade clinical metrics (11)

Canonical CGM metrics from the clinical literature, computed from existing Nightscout data. Added per a 2026-05-24 deep research review of pediatric closed-loop T1D analytics. All formulas cited; references in module docstrings.

| Tool | Returns | Reference |
|---|---|---|
| `glycemia_risk_index` | GRI + Hypo / Hyper component decomposition | Klonoff *JDST* 2023;17:1226 |
| `bg_risk_indices` | LBGI / HBGI / ADRR with risk bands | Kovatchev *Diabetes Care* 1998;21:1870 + 2006;29:2433 |
| `glucose_variability_metrics` | MAGE, MODD, J-index, M-value, GVP, CONGA-{1h,2h,4h}, COGI, CV | Service 1970, Molnar 1972, Schlichtkrull 1965, McDonnell 2005, Peyser 2018, Leelarathna 2020 |
| `time_in_range_with_ci` | TIR / TBR / TAR with Wilson binomial 95 % CIs per band | Battelino *Diabetes Care* 2019;42:1593 |
| `per_meal_period_tir` | TIR by breakfast / lunch / dinner / overnight / afternoon / evening | (composition) |
| `ambulatory_glucose_profile` | AGP-style 5/25/50/75/95th percentile bands by hour-of-day | Battelino 2019 AGP consensus |
| `bolus_event_residuals` | Per-bolus realized-vs-AAPS-predicted ISF, stratified by BG band + time-of-day | (composition of the above + devicestatus) |
| `change_points_bg` | Windowed mean-shift change-point detection on hourly mean BG, annotated with profile changes | Page 1954 (CUSUM family) |
| `change_points_tdd` | Same on daily total daily dose (catches puberty/illness/site shifts) | |
| `dia_fit_estimate` | Exploratory fit of AAPS exponential IOB curve to observed bolus residuals → suggested DIA + peak | oref0 exponential / AAPS Oref |
| `clinic_packet` | Composite 30-day markdown report (TIR + GRI + LBGI/HBGI + per-meal-period + change-points) ready to share with endo team | |

## Example: what the LLM actually sees

`insulin_sensitivity_check(days=14)` against a personal Nightscout instance returns something like:

```json
{
  "sample_count": 37,
  "derived_isf_mgdl_per_unit": 304.7,
  "derived_isf_mmol_per_unit": 16.9,
  "profile_isf_mmol_per_unit": 14.0,
  "ratio_derived_over_profile": 1.21,
  "confidence": "high",
  "recommendation": "Derived ISF suggests you're MORE sensitive than your profile says (each unit drops you further). Consider lowering profile ISF or reviewing for overcorrections."
}
```

`detect_patterns(days=7)` surfaces things like:

```json
{
  "type": "post_meal_spike",
  "occurrence_count": 7,
  "avg_value_mgdl": 73.6,
  "description": "Rapid rises >50 mg/dL within 30 min seen on 7 of 7 days. Bolus-to-eat timing may benefit from a longer pre-bolus."
}
```

These aren't generic — they're computed from the user's actual outcomes, not their profile claims.

## Why this exists

There are a few other Nightscout MCPs in the wild:
- **`adminpb/Nightscout-MCP`** (TypeScript) — 24 tools, but no tests visible
- **`easyweek/mcp-nightscout`** (Python, HTTP+Docker) — built for shared/remote deployment, ships destructive writes including `remove_treatment`
- **`nightscout/nocturne`** (C#/.NET) — first-party but targets the future Nocturne platform, not v15

This project takes a deliberately different slot:

| Axis | This project |
|---|---|
| Transport | **stdio** — no exposed network surface |
| Auth | **Token only** — refuses `API_SECRET` (least privilege) |
| Writes | **None** — provably safer for personal/educational use |
| Tests | **175 passing** including a cross-tool token-leak regression |
| Default units | **mmol/L** (overridable) — every payload includes both |
| Analytics | **Real-world ISF**, compression-low detection, recurring pattern detection |

## Install

```bash
git clone https://github.com/ColebyPearson/nightscout-mcp
cd nightscout-mcp
uv sync
```

(PyPI publication planned — see [PLAN.md §10](./PLAN.md).)

## Configure

1. In Nightscout → **Admin Tools → Subjects**, create a token with the **`readable`** role (e.g. `mcp-reader`).
2. Copy `.env.example` to `.env` and fill in:

```ini
NIGHTSCOUT_URL=https://your-nightscout.example.com   # MUST be https://
NIGHTSCOUT_TOKEN=mcp-reader-xxxxxxxxxx               # Never use API_SECRET
NIGHTSCOUT_UNITS=mmol/L                              # or mg/dL
```

3. **Protect the file.** Your token lets anyone read your CGM data. Same precaution as `~/.ssh/`:

```bash
chmod 600 .env     # macOS / Linux
icacls .env /inheritance:r /grant:r "%USERNAME%:R"     # Windows PowerShell
```

`.env` is gitignored from `.gitignore:2` — it won't be committed even by accident.

## Run

### MCP Inspector (development / manual testing)

```bash
uv run mcp dev src/nightscout_mcp/server.py
```

### Claude Desktop

Add to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "nightscout": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/nightscout-mcp", "run", "nightscout-mcp"],
      "env": {
        "NIGHTSCOUT_URL": "https://you.nightscout.example",
        "NIGHTSCOUT_TOKEN": "mcp-reader-xxxxxxxxxx",
        "NIGHTSCOUT_UNITS": "mmol/L"
      }
    }
  }
}
```

Restart Claude Desktop. The 31 tools appear under the 🔌 menu.

### Claude Code

```bash
claude mcp add nightscout --env NIGHTSCOUT_URL=https://… --env NIGHTSCOUT_TOKEN=… -- uv --directory /abs/path/to/nightscout-mcp run nightscout-mcp
```

## Safety model

What this project does to protect your data:

- **`NIGHTSCOUT_URL` must be `https://`** — refuses to start otherwise.
- **Token never enters LLM context.** It's used only as a `?token=…` query parameter on outbound HTTPS calls. Tool responses contain only derived data.
- **Token never appears in logs.** A regex filter on the httpx logger replaces `token=…`, `Bearer …`, and `api-secret: …` values with `***` before emission. There's a unit test that asserts this.
- **Token never appears in any tool response.** There's a cross-tool regression test that sets a canary token, calls every tool, JSON-serializes the response, and asserts the canary doesn't appear.
- **Read-only by design.** Write tools are not imported. Even if `NIGHTSCOUT_ALLOW_WRITES=true` were set, nothing would happen.
- **Local-only by default.** stdio transport means there's no listening port. Your data stays on your machine + your Nightscout host.

What this project does *not* do:

- Encrypt your `.env` at rest (use OS file permissions)
- Detect token compromise (rotate via Admin Tools if you suspect leakage)
- Replace clinical judgment (it's not a medical device)

## Develop

```bash
uv sync --extra dev
uv run pytest                            # all 75 tests
uv run pytest tests/test_analytics.py    # just analytics
uv run ruff check .                      # lint
```

Architecture: [PLAN.md](./PLAN.md). Issue-driven workflow with self-reviewed PRs.

## License

[MIT](./LICENSE). Nightscout itself is [AGPL-3.0](https://github.com/nightscout/cgm-remote-monitor/blob/master/LICENSE) — this project consumes its HTTP API but does not include or redistribute its code.

## Acknowledgments

- The [Nightscout Foundation](https://nightscout.github.io/) and the wider **#WeAreNotWaiting** community — for building the platform this MCP rides on top of.
- [`adminpb/Nightscout-MCP`](https://github.com/adminpb/Nightscout-MCP) — TypeScript prior art that informed several tool names.
- [`easyweek/mcp-nightscout`](https://github.com/easyweek/mcp-nightscout) — the JSON-log-scrubbing pattern.
- [`amansk/librelink-mcp-server`](https://github.com/amansk/librelink-mcp-server) — the "data never leaves your machine" credential-security framing.
- The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and the FastMCP decorator API.
- Bergenstal et al., *Diabetes Care* 2018; 41:2275–2280 — for the GMI formula used in `get_glucose_stats`.

> **Why I built this:** I wanted an LLM analyst that could reason over my actual Nightscout history without me having to copy-paste CSVs. The existing options either had no tests, shipped destructive writes, or targeted a future platform. So I built the one I wanted to use.
