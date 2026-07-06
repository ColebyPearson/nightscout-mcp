# Nightscout MCP — Implementation Plan

**Status:** Draft v1 (2026-05-22). Builds on the [research report](./compass_artifact_wf-3a9b938b-c945-4de6-ba39-a3bbceeb8356_text_markdown.md). Locked decisions are marked ✅; open items in [§9](#9-open-items).

---

## 1. Goal & scope

A Model Context Protocol server that lets an LLM (Claude Desktop, Claude Code, any MCP client) introspect a personal Nightscout instance — current glucose, history, treatments, profile, device status, and derived analytics — through a small, typed, audited tool surface.

**In scope (first cut):** read-only tools + analytics tier against Nightscout REST API v1.
**Deferred:** write tools (carb/note/temp-target), API v3 + JWT migration, multi-user.
**Out of scope:** AAPS direct integration, medical/clinical recommendations, multi-tenant deployment.

## 2. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Language/stack | ✅ **Python 3.11+ + FastMCP** | Cleanest tool decorators, official SDK, easiest stats math |
| HTTP client | ✅ **httpx (direct)** — *not* `py-nightscout` | `py-nightscout` last released Dec 2021; one httpx client handles v1 *and* v3 |
| Target instance | ✅ User's existing Nightscout site | URL + reader token needed before first run ([§9](#9-open-items)) |
| API version | ✅ **v1 first**, v3 deferred | v1 is simpler, stable, sufficient for read-only |
| Write tools | ✅ **Deferred** (Phase 3 not in first cut) | Lowest-risk first ship; safer for public repo |
| Repo visibility | ✅ **Public portfolio** | Public code, private credentials — no instance URL or tokens committed |
| License | ✅ **MIT** for the MCP, with Nightscout (AGPL-3.0) attribution in README | Most permissive; we're not redistributing Nightscout's code |
| Default glucose units | ✅ **mmol/L** | User preference; tools still return both units in payloads |
| Repo name | ✅ **`nightscout-mcp`** | |

## 2a. Competitive landscape (surveyed 2026-05-22)

Three actual Nightscout MCPs exist publicly, plus one marketplace listing of unclear provenance. Mapping the empty space:

| Niche | Filled by |
|---|---|
| TypeScript / analytics-heavy / personal stdio | [`adminpb/Nightscout-MCP`](https://github.com/adminpb/Nightscout-MCP) — 24 tools, no tests, low engagement |
| Python / Docker remote service / writes incl. destructive | [`easyweek/mcp-nightscout`](https://github.com/easyweek/mcp-nightscout) — 15 tools, HTTP+stdio dual, auth middleware tests only |
| .NET / **Nocturne** platform / first-party | [`nightscout/nocturne` → `Nocturne.Tools.McpServer`](https://github.com/nightscout/nocturne) — 7 tools, stdio+SSE, "no authentication implemented" |
| **Python / stdio / personal / safety-first / well-tested / Nightscout v15** | **← This project. No one occupies it.** |

**What we steal:**
- From `easyweek`: structured JSON logging + token-scrubbing log filter (now formal Phase 1 scope, [§4](#phase-1--read-tools-6h))
- From `adminpb`: 2 well-named analytics tools added to Phase 2 (`insulin_sensitivity_check`, `compression_low_analysis`) so users coming from the TS world find familiar entry points
- From `amansk/librelink-mcp-server` (adjacent): explicit "data never leaves your machine" framing and file-permission guidance in README

**What we consciously don't copy:** destructive writes, `API_SECRET` fallback, HTTP transport with auth complexity, tool sprawl without tests.

## 3. Architecture

```
┌─────────────────┐    stdio (MCP)    ┌─────────────────────┐    HTTPS+token    ┌──────────────────┐
│  Claude / any   │ ◀───────────────▶ │   nightscout_mcp    │ ◀───────────────▶ │ Nightscout v15.x │
│   MCP client    │                   │  (FastMCP server)   │   /api/v1/...     │  (Node + Mongo)  │
└─────────────────┘                   └─────────────────────┘                   └──────────────────┘
                                                ▲
                                                │ env: NIGHTSCOUT_URL, NIGHTSCOUT_TOKEN, NIGHTSCOUT_UNITS
                                                ▼
                                        ┌───────────────┐
                                        │ .env (local)  │   ← never committed
                                        └───────────────┘
```

**Data flow per tool call:**
1. MCP client invokes a tool (e.g. `get_glucose_stats(hours=24)`).
2. FastMCP validates args against the typed Python signature.
3. `client.py` GETs `/api/v1/entries.json?token=…&count=…&find[dateString][$gte]=…`.
4. `stats.py` (or pass-through) shapes the response into a stable schema: `{value_mgdl, value_mmol, direction, trend_arrow, delta_mgdl, minutes_ago, iso_time}` etc.
5. Tokens stay in env / HTTP headers — never returned to the LLM.

## 4. Phased delivery

### Phase 0 — Foundations (≈1h)
- `uv init nightscout-mcp` → `uv add "mcp[cli]" fastmcp httpx pydantic python-dotenv pytest pytest-asyncio respx`.
- `.env.example` with `NIGHTSCOUT_URL`, `NIGHTSCOUT_TOKEN`, `NIGHTSCOUT_UNITS` (mg/dL default).
- `config.py` (pydantic-settings) refuses to boot if URL isn't `https://` or token is empty.
- `client.py` async httpx client with one `nightscout_get(path, params)` helper. Confirms reachability against `/api/v1/status.json` on startup.
- `pyproject.toml` entry point: `nightscout-mcp = "nightscout_mcp.server:main"`.

### Phase 1 — Read tools + logging hygiene (≈7h)

**Tools (9):**

| Tool | Signature | Notes |
|---|---|---|
| `get_current_glucose` | `() → CurrentGlucose` | Latest SGV; both units; ASCII trend arrow; minutes_ago; delta vs prior |
| `get_glucose_history` | `(hours: int = 6, count: int \| None = None) → list[Sgv]` | Default 6h; hard-cap count to 2000 to be polite to free-tier instances |
| `get_glucose_stats` | `(hours: int = 24, tir_low: int = 70, tir_high: int = 180) → GlucoseStats` | mean, SD, CV%, TIR, TBR<70/<54, TAR>180/>250, GMI (folds in `a1c_estimator` equivalent) |
| `get_treatments` | `(hours: int = 24, event_type: str \| None = None) → list[Treatment]` | Mongo-style `find[created_at][$gte]` filter |
| `get_iob_cob` | `() → IobCob` | **Reads from latest `devicestatus.openaps`/`pump`/`loop`** — more accurate than re-deriving |
| `get_current_profile` | `() → Profile` | Basal schedule, ISF, IC, target high/low, DIA, timezone |
| `get_device_status` | `(latest: bool = True) → DeviceStatus \| list[DeviceStatus]` | Pump reservoir/battery, loop state, last enacted temp basal |
| `get_server_status` | `() → ServerStatus` | NS version, units, features — cache 5 min |
| `search_treatments` | `(query: str, since: datetime \| None, until: datetime \| None) → list[Treatment]` | Free-form note/event search |

**Logging hygiene (new for Phase 1, lifted from `easyweek`):**
- `logging_setup.py` with a structured (key=value) formatter and an httpx event hook that scrubs `token=…` from logged URLs before emission. Avoids leaking the token into Claude Desktop's debug log when running under stdio.
- **Regression test:** assert that no Phase 1 tool response, serialized to JSON, contains the token substring — even by accident.

### Phase 2 — Analytics tier (≈4h)

Mirrors the most useful tools from `adminpb/Nightscout-MCP` but in Python with proper statistics. Tool *names* deliberately match adminpb's where possible so users coming from the TS world find familiar entry points.

| Tool | Signature | Notes |
|---|---|---|
| `detect_patterns` | `(days: int = 14) → list[Pattern]` | Overnight lows, dawn phenomenon, post-meal spikes, variability windows |
| `compare_periods` | `(period_a_start, period_a_end, period_b_start, period_b_end) → PeriodComparison` | Side-by-side stats; flags improvement/regression |
| `analyze_meal` | `(meal_time: datetime, window_hours: int = 4) → MealAnalysis` | Peak, time-to-peak, rise, recovery, bolus assessment |
| `overnight_analysis` | `(date: date) → OvernightAnalysis` | Stability, drift, dawn effect, basal adequacy proxy |
| `get_daily_report` | `(date: date) → DailyReport` | Stats + treatments + events for a 24h window |
| `insulin_sensitivity_check` | `(days: int = 14) → IsfDerivation` | **Derives *real* ISF from correction-bolus outcomes** vs. profile-stated ISF. Genuinely clever — one of the strongest tools adminpb ships. |
| `compression_low_analysis` | `(days: int = 14) → list[SuspectedCompression]` | Detects false-low sensor compression artifacts (lying on sensor at night) — adminpb's most differentiated analytical tool. Stretch goal. |

GMI formula: `GMI(%) = 3.31 + 0.02392 × mean_mgdl` (Bergenstal et al., *Diabetes Care* 2018; 41:2275–2280).

### Phase 3 — DEFERRED (writes)

Documented for later in a separate spike. Will require: `NIGHTSCOUT_ALLOW_WRITES=true` + separate `NIGHTSCOUT_WRITER_TOKEN` (careportal scope) + `confirmation_phrase` per call + write-audit log at `~/.nightscout-mcp/writes.log` + range clamps. `add_bolus_entry` will **not** be implemented — phantom insulin entries corrupt AAPS IOB calculations.

## 5. Module layout

```
nightscout-mcp/
├── pyproject.toml
├── README.md                # Public — install + Claude Desktop config + screenshot
├── PLAN.md                  # This file
├── compass_artifact_*.md    # Original research
├── LICENSE                  # TBD (MIT lean)
├── .env.example
├── .gitignore               # .env, .venv, __pycache__, .pytest_cache, *.log
├── src/nightscout_mcp/
│   ├── __init__.py
│   ├── server.py            # FastMCP() instance, main() entry point, tool registration
│   ├── config.py            # pydantic-settings; validates URL is https://, token present
│   ├── client.py            # async httpx wrapper; one nightscout_get(path, params)
│   ├── models.py            # Pydantic response models (Sgv, Treatment, Profile, …)
│   ├── stats.py             # TIR/GMI/SD/CV math, unit-aware
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── read.py          # Phase 1 tools
│   │   └── analytics.py     # Phase 2 tools
│   └── safety.py            # https-only guard, unit sanity, range guards
└── tests/
    ├── test_client.py       # respx-mocked HTTP
    ├── test_stats.py        # pure math, no network
    └── test_tools_read.py   # tool-level integration with mocked client
```

## 6. Safety, privacy, secrets

This is health data on a public repo — non-negotiables:

1. **Never commit `.env`, real URLs, or tokens.** `.gitignore` includes `.env` from line 1.
2. **`NIGHTSCOUT_URL` must be `https://`** — `config.py` refuses to start otherwise.
3. **Tokens never enter LLM context.** They live in env vars, used as `?token=…` query string only. Tool responses contain only derived data.
4. **Use a `readable`-role access token, never `API_SECRET`.** Created in Nightscout Admin Tools → Subjects.
5. **README disclaimer mirrors Nightscout's:** "Use at your own risk; not a medical device; no clinical advice."
6. **No client/patient identifiers in commits.** All test fixtures use synthetic data.

## 7. Repo hygiene (per global CLAUDE.md)

- `main` branch protected (squash-merge only) once first PR lands.
- Work happens on `feat/phase-0-foundations`, `feat/phase-1-read-tools`, `feat/phase-2-analytics` — one PR per phase, with test plan + self-review comment.
- Bugs caught during the build → GitHub issue with `bug` label → fix → close with **Resolved** comment referencing commit.
- Conventional commit messages, *why*-focused.

## 8. Verification plan

- **Unit:** `pytest tests/` with `respx` mocking httpx calls. Cover stats math, unit conversion, filter-string generation.
- **Local end-to-end:** point at your existing NS instance with a `readable` token, run `uv run mcp dev src/nightscout_mcp/server.py` (MCP Inspector), exercise each tool.
- **Claude Desktop:** add to `claude_desktop_config.json`, verify each tool is callable and returns sane shapes.
- **Phase 2 sanity:** spot-check `detect_patterns` and `analyze_meal` outputs against a known recent meal/overnight window manually.

## 9. Open items

| # | Item | Needed before |
|---|---|---|
| 1 | **Your Nightscout URL** (e.g. `https://something.up.railway.app`) | Phase 0 verification step |
| 2 | **`readable`-role access token** from your NS Admin Tools → Subjects (mint as `mcp-reader`) | Phase 0 verification step |
| ~~3~~ | ~~Units preference~~ | ✅ **mmol/L** |
| ~~4~~ | ~~License choice~~ | ✅ **MIT** |
| ~~5~~ | ~~Repo name~~ | ✅ **`nightscout-mcp`** |

## 10. Next actions

1. ✅ Phase 0 scaffold committed to main (`56710e0`, `31a207d`). Live `health_check` verified against `a personal Nightscout instance` (Nightscout v15.0.5). 8/8 tests passing.
2. **▶ Phase 1 read tools + logging hygiene on `feat/phase-1-read-tools`. PR #1.**
3. Phase 2 analytics (incl. `insulin_sensitivity_check`, optional `compression_low_analysis`) on `feat/phase-2-analytics`. PR #2.
4. README polish + Claude Desktop config screenshot for the portfolio angle. PR #3.
5. (Optional) Publish to PyPI as `nightscout-mcp` once Phase 2 is stable.
