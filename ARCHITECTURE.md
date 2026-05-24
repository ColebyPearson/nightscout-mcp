# Architecture

This document explains how `nightscout-mcp` is structured: what each module does, how data flows through a tool call, and the design choices that shape the codebase.

## TL;DR

`nightscout-mcp` is not a thin pass-through to the Nightscout REST API. It is a typed Python application with **41 MCP tools** spanning five categories, where the LLM is the **interpreter** and the MCP is the **computational substrate**.

The LLM does not compute Time-in-Range, the Glycemia Risk Index, the LBGI / HBGI, or per-bolus realized ISF residuals. Those are exact formulas implemented in pure-stdlib Python and called via MCP. The LLM consumes the typed result and reasons over it. This division is deliberate — LLMs are unreliable at arithmetic, and deterministic formulas must run in code.

A useful mental model: **the MCP is to diabetes data what `pandas` + `scipy` are to general data analysis — a typed, tested, deterministic computational layer that the LLM (or a human) calls into.** It just exposes that layer over the MCP protocol instead of as Python imports.

## The pattern every tool follows

```
LLM (Claude / Cursor / etc.)
        │
        │  MCP protocol over stdio
        ▼
┌──────────────────────────────────────────────────┐
│ tools/<category>.py wrapper                       │
│                                                   │
│ 1. Fetch raw data:                                │
│      - Nightscout HTTP via async httpx, OR        │
│      - Local SQLite via stdlib sqlite3 (AAPS hist)│
│                                                   │
│ 2. Parse into typed Pydantic v2 models            │
│                                                   │
│ 3. Delegate to a pure function in                 │
│      analytics.py | metrics.py | aaps_history.py  │
│      (no I/O; deterministic; unit-tested)         │
│                                                   │
│ 4. Wrap the pure function's output in a           │
│    response Pydantic model                        │
│                                                   │
│ 5. Return to the MCP framework                    │
└──────────────────────────────────────────────────┘
        │
        ▼
LLM receives structured JSON. Interprets, synthesizes
across multiple tool calls, frames for the human user.
```

The split is intentional:
- **Wrappers** in `tools/*.py` handle I/O, pagination, parsing, and the MCP protocol.
- **Pure functions** in `analytics.py` / `metrics.py` / `aaps_history.py` contain the math. They take parsed inputs, return computed outputs, raise no exceptions for empty data (they return zero-filled responses), and are exhaustively unit-tested without any HTTP mocking.

## File-level layout

```
src/nightscout_mcp/
├── client.py            async httpx client + token redaction guard
├── config.py            env loading, units, defaults
├── server.py            FastMCP entry; registers all 5 tool modules
├── models.py            Pydantic v2 models (~870 LOC) — inputs + outputs
├── units.py             mg/dL ↔ mmol/L conversion
├── stats.py             shared GMI / TIR helpers
├── analytics.py         Phase-2 pure functions (insulin sensitivity, carb ratio,
│                          patterns, compression-low detection)
├── metrics.py           Research-grade formulas (GRI, LBGI/HBGI, MAGE, MODD,
│                          J-index, M-value, GVP, CONGA, COGI, AGP percentiles,
│                          windowed change-point detection, IOB curve fitter)
├── synthesis.py         daily_synthesis cross-tool roller
├── aaps_history.py      Local SQLite reader for the aaps-history-ingest store
├── safety.py            Token-leak guard helpers
└── tools/
    ├── read.py          10 Nightscout fetch tools
    ├── analytics.py     9 analytics wrappers
    ├── metrics.py       16 metrics + composition tool wrappers
    ├── synthesis.py     1 synthesis tool wrapper
    └── aaps_history.py  5 AAPS history tool wrappers

tests/
└── *.py                 210 tests, including a cross-tool token-leak regression
                          that asserts no tool response contains the canary token
```

## Tools by category

### Read tools (10) — `tools/read.py`

Closest to "pass-through" but still do unit conversion, freshness calculation, and trend interpretation. The LLM never has to do mg/dL ↔ mmol/L math; every glucose-valued field carries both.

| Tool | Returns |
|---|---|
| `health_check` | NS reachability + version + units |
| `get_current_glucose` | Latest SGV + trend arrow + freshness + delta vs prior |
| `get_glucose_history` | Time-series SGVs over a window |
| `get_glucose_stats` | TIR / TBR / TAR / SD / CV / GMI |
| `get_treatments` | Boluses / carbs / basals / notes |
| `get_iob_cob` | IOB + COB from `devicestatus` |
| `get_current_profile` | Basal schedule / ISF / CR / targets / DIA / timezone |
| `get_device_status` | Pump / loop / uploader state (tiered priority) |
| `get_server_status` | NS version, status, configured units |
| `search_treatments` | Substring search across notes / event types |

### Analytics tools (9) — `tools/analytics.py`

Domain-specific analysis on fetched data. Substantial math.

| Tool | Returns |
|---|---|
| `get_daily_report` | One-day stats + treatment totals + filtered user notes |
| `compare_periods` | Side-by-side stats with plain-English delta summary |
| `analyze_meal` | Pre-meal BG / peak / time-to-peak / rise / recovery |
| `overnight_analysis` | Drift / min/max / time-below / dawn rise / flatness |
| `detect_patterns` | Recurring overnight lows, dawn phenomenon, post-meal spikes |
| `insulin_sensitivity_check` | Real-world ISF from correction-bolus outcomes vs profile |
| `effective_isf_check` | Real-world ISF vs AAPS Dynamic ISF, per-BG-band stratified |
| `carb_ratio_check` | Real-world CR from meal-bolus outcomes vs profile |
| `compression_low_analysis` | Suspected sensor-compression artifacts (false lows) |

### Research-grade metrics (11) — `tools/metrics.py`

Canonical CGM-research formulas from the clinical literature. Each implements its source paper exactly. References cited in module docstrings.

| Tool | Formula source |
|---|---|
| `glycemia_risk_index` | Klonoff *JDST* 2023;17:1226 |
| `bg_risk_indices` (LBGI / HBGI / ADRR) | Kovatchev *Diabetes Care* 1998;21:1870 + 2006;29:2433 |
| `glucose_variability_metrics` (MAGE, MODD, J-index, M-value, GVP, CONGA-{1,2,4}h, COGI, CV) | Service 1970, Molnar 1972, Schlichtkrull 1965, McDonnell 2005, Peyser 2018, Leelarathna 2020 |
| `time_in_range_with_ci` | Battelino *Diabetes Care* 2019;42:1593 + Wilson 1927 binomial CI |
| `per_meal_period_tir` | (composition) |
| `ambulatory_glucose_profile` | Battelino 2019 AGP consensus |
| `bolus_event_residuals` | Per-bolus realized-vs-AAPS-predicted ISF, BG-band stratified |
| `change_points_bg` / `change_points_tdd` | Page 1954 (CUSUM family), windowed mean-shift |
| `dia_fit_estimate` | oref0 exponential IOB curve, grid-search fit |
| `clinic_packet` | Composite 30-day markdown report for endo visits |

### Composition tools (5) — `tools/metrics.py`

Decision-tree wrappers that combine multiple tools. Where deterministic clinical-decision logic lives.

| Tool | What it does |
|---|---|
| `dynisf_adjustment_recommender` | Reads `bolus_event_residuals` output and applies a BG-curve-vs-uniform-AF decision tree to recommend a Dynamic ISF Adjustment Factor (or "hold") with confidence band |
| `consensus_target_audit` | Pass/fail audit of 10 metrics vs Battelino 2019 / ISPAD 2022 / Klonoff 2023 / Kovatchev thresholds |
| `settings_change_attribution` | For each profile-switch event in window: pre-vs-post TIR / TBR / GRI with Benjamini-Hochberg FDR correction across changes |
| `agp_markdown_render` | Markdown-rendered AGP with ASCII IQR-band visualization |
| `time_period_compare` | Two-window TIR / TBR<54 / GRI / LBGI / HBGI / CV / GMI side-by-side with 95 % CIs and statistical-significance flagging |

### AAPS history tools (5) — `tools/aaps_history.py`

Read-only access to a **local SQLite** store of decrypted AAPS settings snapshots populated by the companion [aaps-history-ingest](https://github.com/ColebyPearson/aaps-history-ingest) service. These tools do not touch Nightscout at all.

When the ingest service is not configured, every tool returns a graceful empty response with a setup note — so the MCP is fully usable on a fresh install without the ingest pipeline.

| Tool | Returns |
|---|---|
| `aaps_history_status` | DB presence, snapshot count, captured-at range, latest AAPS version |
| `aaps_setting_at` | Value of a setting at a point in time, resolved from the most-recent prior snapshot |
| `aaps_setting_history` | Timeline of all changes to a single setting over a window |
| `aaps_settings_diff` | All settings changed between two timestamps |
| `aaps_log_user_entries` | USER ENTRY records from the AAPS log archive (populated once the log-ingestion pipeline ships) |

DB location resolves via the `AAPS_INGEST_DB_PATH` env var.

### Synthesis (1) — `tools/synthesis.py`

| Tool | What it does |
|---|---|
| `daily_synthesis` | One-shot composite that pulls all relevant data for the last N days and returns a single object: TIR + patterns + ISF/CR checks + recommendations. Built for "give me the whole picture in one call." |

## Concrete example: `glycemia_risk_index(days=14)` execution path

```
LLM → MCP framework → tools/metrics.py::glycemia_risk_index(days=14)
      │
      │ 1. Compute window
      │      start = now - 14d
      │      end   = now
      │
      │ 2. Fetch SGVs via existing helper
      │      sgvs = await _fetch_sgvs_between(client, start, end)
      │      (paginated; handles the 2000-row Nightscout per-request cap)
      │
      │ 3. Extract mg/dL values
      │      values = [float(s.sgv_mgdl) for s in sgvs if s.type == "sgv"]
      │
      │ 4. Call pure function
      │      result = metrics.gri(values)
      │
      │      Inside metrics.gri:
      │        n = len(values)
      │        very_low  = sum(1 for v in values if v < 54)
      │        low       = sum(1 for v in values if 54 <= v < 70)
      │        high      = sum(1 for v in values if 180 < v <= 250)
      │        very_high = sum(1 for v in values if v > 250)
      │        pct_*     = c / n * 100  for each band
      │        gri_hypo  = 3.0 * pct_very_low + 2.4 * pct_low
      │        gri_hyper = 1.6 * pct_very_high + 0.8 * pct_high
      │        gri       = clamp(gri_hypo + gri_hyper, 0, 100)
      │      → returns dict of all the above
      │
      │ 5. Wrap in Pydantic model
      │      return GlycemiaRiskIndex(
      │          gri=62.27,
      │          gri_hypo_component=9.04,
      │          gri_hyper_component=53.23,
      │          pct_very_low_lt54=0.77,
      │          pct_low_54_69=2.80,
      │          pct_in_target_70_180=52.34,
      │          pct_high_181_250=21.63,
      │          pct_very_high_gt250=22.45,
      │          sample_count=3888,
      │          days=14,
      │      )
      ▼
LLM receives this and reasons:
  "GRI 62.27 with Hypo 9.0 + Hyper 53.2 means hyper-dominant.
   Combined with HBGI = 12.21 (high band, from bg_risk_indices),
   the overall picture is high hyperglycemic burden with controlled
   hypo risk. The dominant signal is TAR > 180 at 44%, not TBR."
```

**The LLM does not compute the GRI.** It interprets the result. The MCP guarantees the formula is exactly the published one, every call.

## Design decisions

### Why pure functions + thin wrappers

- The math is the **interesting code** — it must be testable without HTTP mocking.
- Wrappers become trivial: fetch, delegate, wrap. No business logic. Easy to refactor when Nightscout API changes.
- Pure functions can be reused in other contexts (e.g., a CLI, a Jupyter notebook, a future PyPI library extraction).

### Why the MCP does the math, not the LLM

LLMs are unreliable at arithmetic and statistics:
- Sums over thousands of CGM readings are not LLM-friendly.
- Specific weighted formulas (`GRI = 3.0·VLow + 2.4·Low + 1.6·VHigh + 0.8·High`) must be **exact**, not approximate.
- Statistical machinery (Wilson CI, FDR correction, two-proportion z-test) must be correct.
- Same query → same answer matters. Determinism is a feature.

The LLM's role is to interpret the structured output, tie multiple tool calls together, surface anomalies, and frame for the human user. That is what LLMs are good at.

### Why Pydantic v2 everywhere

- Inputs are validated at the tool boundary; no defensive None-checking downstream.
- Outputs serialize to JSON cleanly for the MCP protocol.
- `model_post_init` is used for derived fields (e.g., every `Sgv` gets both `sgv_mgdl` and `sgv_mmol` populated once at parse time, so no downstream code does unit math).
- Field aliases let Nightscout's mixed `camelCase` / `snake_case` parse without forcing callers to know which is which.

### Why no scipy / numpy / ruptures

- All metrics formulas have textbook implementations in pure stdlib (`statistics`, `math`).
- CUSUM-family change-point detection is implemented in ~30 LOC.
- The Wilson binomial CI is a 6-line function.
- The exponential IOB curve is a direct port from oref0's JavaScript.
- Eliminates ~100+ MB of transitive Android / numpy / scipy dependencies that would otherwise bloat install size.
- Easier to audit against cited papers; every formula stays close to its source.

The tradeoff: certain algorithms (e.g., `ruptures` PELT) would be more sophisticated than the windowed mean-shift implemented here. For the n ≤ ~720 hourly-BG window the simpler algorithm is adequate. If a future tool needs production-grade statistical inference, scipy can be added with a deliberate dependency decision.

### Why every tool has a Pydantic response model (vs. returning plain dicts)

- The MCP protocol's tool-definition includes input + output schemas.
- Typed responses let the LLM client introspect what each tool returns without trial-and-error.
- Pydantic models are self-documenting via field types + descriptions.
- Catches developer mistakes at the boundary (wrong field name, wrong type) instead of silently producing bad JSON.

### Token-leak safety

A cross-tool regression test sets a canary Nightscout token, invokes every tool, JSON-serializes the responses, and asserts the canary string does not appear in any of them. This catches the case where an exception message echoes back the URL (with token in the query string) into a tool response.

Currently 41 tools, all covered.

### Two data sources, one MCP

The MCP reads from two completely independent stores:

1. **Nightscout REST API** — over HTTP, async httpx, the user's NS instance
2. **Local SQLite** at `AAPS_INGEST_DB_PATH` — populated by the separate `aaps-history-ingest` service

Tools that need both (`bolus_event_residuals` — Nightscout devicestatus + treatments + entries — does not currently cross-reference AAPS settings, but a future tool could) would simply call both code paths.

## Privacy + safety model

- **The MCP code contains zero PHI.** It knows about settings *keys* (`DynISFAdjust`, `autosens_min`, `LocalProfile_isf_0`) but no patient-specific values are hardcoded.
- **PHI flows through the MCP at request time.** When a tool fetches CGM data or a settings snapshot, that data is in the response — which is returned to the LLM client. Same model as any local-database query tool.
- **The MCP never proactively transmits to a third party.** All HTTP traffic is to the user's own Nightscout instance.
- **Configuration uses the token-only Nightscout auth** with `read` scope. No `API_SECRET` (which would grant write access). Even if `NIGHTSCOUT_ALLOW_WRITES=true` is set in the environment, no write tools are imported.
- **The decrypted-AAPS-export full JSON stays on the local PC.** Sanitized markdown lands in a private companion repo (`cysSETTINGS`); the public MCP never sees it.

## Future directions

This section captures architectural decisions still open. None are urgent.

### Extracting a `cgm-metrics` PyPI library

The contents of `src/nightscout_mcp/metrics.py` (GRI, LBGI / HBGI, ADRR, MAGE, MODD, J-index, M-value, GVP, CONGA, COGI, AGP percentile bands, Wilson CI, exponential IOB curve) are reusable beyond MCP. There is no comparable production-quality Python CGM-metrics library — R's `iglu` is the closest analog and Python users do not have an equivalent.

A future `cgm-metrics` PyPI package containing the pure formula functions (zero deps; stdlib only) would be a meaningful open-source contribution. The MCP would `pip install` it as a dependency and remain focused on the MCP tool surface.

Estimated effort: 3-4 hours. No urgency; current state is fine.

### Splitting AAPS-specific tools out

The 5 AAPS history tools (and the future log-archive tools, blocked on the AAPS upstream `ActionLogsExport` work) are architecturally separate — they read a local SQLite and never touch Nightscout. They live in this repo today for installer convenience.

If/when the log-archive tools land (adding 5+ more tools all reading the same local SQLite), splitting into a separate `aaps-history-mcp` would be a reasonable next step. Trigger: ~10+ tools that share a non-Nightscout data source.

### What this codebase deliberately does NOT do

- **ML BG forecasting.** Published research (OhioT1DM benchmarks) shows ML models do not reliably outperform AAPS's existing Oref + Dynamic ISF in deployable form. The MCP exposes the algorithm state AAPS already publishes (variable_sens, predBGs trajectories) rather than running parallel models.
- **Custom meal absorption modeling.** AAPS UAM already does this. Re-implementing Hovorka / Dalla Man / Bergman models in the MCP would add complexity without obvious benefit.
- **CGM accuracy correction.** Dexcom G7 MARD ~8 % is at the noise floor; layering a correction model adds complexity without obvious benefit.
- **Voice / EDA / hydration / SpO2 sensor integration.** Research-stage at best; not yet evidence-supported for closed-loop use.

These are listed not because they are bad ideas in general but because they have been considered and explicitly deferred.

## Reference

- README — installation, configuration, MCP client integration
- `pyproject.toml` — dependencies and packaging
- `tests/test_*.py` — exhaustive unit tests; the best documentation of the math
- `aaps-history-ingest` — the companion ingest service for the AAPS history tools
