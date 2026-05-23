# Building a Nightscout MCP Server in a Weekend — A Decision and Build Plan

## TL;DR
- **Build the MCP server against the Nightscout REST API, not AndroidAPS.** AndroidAPS is a Kotlin Android app with no convenient external API; it already syncs its data *to* Nightscout via the NSClient/NSClientV3 plugins, which makes Nightscout the de facto integration point for everything you actually want to query (glucose, insulin, carbs, profiles, device status).
- **Use Python + FastMCP** as the stack. The Nightscout REST API is a plain HTTP/JSON API, FastMCP turns decorated Python functions into spec-compliant MCP tools with virtually no boilerplate, and `py-nightscout` gives you a ready-made async client for SGVs/treatments/profiles. A Python developer can stand up Phase 1 (read-only) in 4–6 hours.
- **Phase the work: read-only Phase 1 on Saturday, gated write Phase 2 on Sunday.** Use API v1 + an access token (not the raw `API_SECRET`) for reads; require a confirmation argument and an explicit `NIGHTSCOUT_ALLOW_WRITES=true` env flag for writes. Treat this strictly as personal/educational software — Nightscout itself explicitly disclaims medical use, and write operations into a diabetes management system carry real safety risk.

---

## Key Findings

1. **Nightscout (`nightscout/cgm-remote-monitor`) is the right target.** Node.js + Express + MongoDB; v15.0.6 shipped 03 Mar 2025, the seventh tagged release in the v15.x series (15.0.0 through 15.0.6, per the GitHub releases page) and the project remains actively maintained by the Nightscout Foundation. It exposes two REST APIs (v1 and v3), both Swagger-documented (`/api-docs.html` for v1, `/api/v3/swagger-ui-dist/` for v3). Every AndroidAPS, Loop, xDrip+, and Dexcom-bridge user already aggregates their data here.
2. **AndroidAPS (`nightscout/AndroidAPS`) is a poor MCP target on its own.** It is a Kotlin/Android closed-loop insulin-delivery app — latest stable release AAPS 3.4.2.2, released 10 April 2026 (per androidaps.readthedocs.io: "10 April 2026 : Version 3.4.2.2 is out" and the GitHub release tag `MilosKozak · 3.4.2.2 · 10 Apr 18:25`). It has no externally documented HTTP API; programmatic interaction is normally done through the Nightscout backend the app syncs to (via the NSClient or NSClientV3 plugins) or through on-device Android Automate flows that *also* hit the Nightscout REST endpoints. Building an MCP "directly into AAPS" would require a custom Android service or companion app and is not a weekend project.
3. **Auth has two real modes.** Reads can use the cleartext `API_SECRET` SHA-1-hashed in an `api-secret` HTTP header (v1) or — far better — a per-subject **access token** (e.g., `myreader-ad3b1f9d7b3f59d5`) created in Nightscout's Admin Tools and passed as `?token=…`. API v3 additionally requires exchanging that access token at `/api/v2/authorization/request/{accessToken}` for a JWT (valid ~8 hours, auto-refresh) used as a Bearer header. Use v1 for the weekend build; switch tools to v3 later if you need granular CRUD permissions or proper `srvModified`/HISTORY semantics.
4. **Prior art exists — learn from it, don't be blocked by it.** `adminpb/Nightscout-MCP` (TypeScript, MIT) already exposes 7 read tools (`get_current_glucose`, `get_glucose_history`, `get_statistics`, `get_treatments`, `get_profile`, `get_device_status`, `get_daily_report`) and is read-only by default with Zod validation and SHA-1-hashed secrets. An additional FastMCP/Python-based listing is registered on MCP Market (`mcpmarket.com/server/nightscout`) but its source repo is not easily discoverable from search; treat it as a reference, not a dependency.
5. **A Python `py-nightscout` library gives you the data layer for free.** `py-nightscout` (PyPI, MIT, async, last release Dec 5 2021 — version 1.3.3, per Libraries.io: "Latest release · Dec 5, 2021"; author marciogranzotto) wraps `/api/v1/entries`, `/api/v1/treatments`, and `/api/v1/profile` with typed models — perfect for FastMCP tool bodies.

---

## Details

### 1. Project background and repos

**Nightscout — `github.com/nightscout/cgm-remote-monitor`**
- Purpose: a web-based remote CGM monitor / data hub. "Nightscout acts as a web-based CGM (Continuous Glucose Monitor) to allow multiple caregivers to remotely view a patient's glucose data in real time." (repo README, v15.0.6).
- Architecture: Node.js (LTS), Express, MongoDB. The server reads MongoDB collections (`entries`, `treatments`, `devicestatus`, `profile`, `food`, `settings`) and serves both an HTML dashboard and a JSON REST API.
- Status: actively maintained by the Nightscout Foundation. v15.0.6 is current; multiple PRs merged in 2025 (e.g., #8108 stale-CGM fix). Issue tracker shows ongoing feature and bug activity through Nov 2025.
- License: AGPL-3.0.

**AndroidAPS — `github.com/nightscout/AndroidAPS`**
- Purpose: "Opensource automated insulin delivery system (closed loop)" — a Kotlin/Android hybrid closed-loop APS. Drives compatible insulin pumps (Medtronic, DanaR/RS, Omnipod, Equil, Medtrum, etc.) using CGM data and Oref-based algorithms.
- Architecture: Kotlin multi-module Android app, Room database for local persistence, Gradle build, target ~JDK 21. Latest release 3.4.2.2 (10 April 2026).
- Relationship to Nightscout: AAPS includes **NSClient** and **NSClientV3** plugins under Config Builder → Synchronization. These upload CGM values, boluses, basals, profile switches, temporary targets, etc., to your Nightscout `entries`, `treatments`, `devicestatus`, and `profile` collections. From the AAPS docs: "Enable all data upload to Nightscout … as this is now the standard method." So AAPS is the data **producer**; Nightscout is the queryable **store**.

**Key consequence:** if you want an LLM to "see" what AAPS knows about insulin, carbs, IOB, and BG trend, you read it from Nightscout. AAPS has no documented external REST API of its own.

### 2. APIs and data models

**API v1 (current, simplest, what every client uses):**
- Base: `https://YOUR-SITE/api/v1`
- Self-documented at `https://YOUR-SITE/api-docs.html` (Swagger UI). Per the repo README: "The API is Swagger enabled, so you can generate client code to make working with the API easy."
- Core endpoints:
  - `GET /entries[.json]` — CGM readings. Default returns "most recent 10 values from the last 2 days." Override with `count`, `find[dateString][$gte]=...`, `find[dateString][$lte]=...`, `find[sgv]=...`.
  - `GET /entries/sgv.json` — SGV-only filter.
  - `GET /entries/current.json` — single newest entry.
  - `GET /treatments[.json]` — boluses, carbs, temp basals, notes, profile switches, sensor/site/insulin changes. Same `find[...]` Mongo-style filters.
  - `GET /profile[.json]` and `GET /profile/current.json` — basal/ISF/IC schedules, DIA, timezone, units.
  - `GET /devicestatus[.json]` — uploader battery, pump status, OpenAPS/AAPS suggested temp, IOB, COB.
  - `GET /status.json` — Nightscout server version, settings, units.
  - `GET /count/entries/where?…` — aggregate counts.
- **Entry (SGV) fields:** `type` (`sgv`/`mbg`/`cal`), `date` (Unix ms), `dateString` (ISO 8601), `sgv` (mg/dL int), `direction` (`Flat`, `FortyFiveUp`, `SingleUp`, `DoubleUp`, `FortyFiveDown`, `SingleDown`, `DoubleDown`, `NONE`, `NOT COMPUTABLE`), `device`, `noise`, `filtered`, `unfiltered`, `rssi`. For meter values: `type: "mbg"` with an `mbg` field.
- **Treatment fields:** `created_at` (ISO), `eventType` (e.g. `Bolus`, `Correction Bolus`, `Meal Bolus`, `Carb Correction`, `Temp Basal`, `Snack Bolus`, `Note`, `Announcement`, `Exercise`, `Site Change`, `Sensor Start`, `Insulin Change`, `Temporary Target`, `Temporary Override`, `Profile Switch`, `Combo Bolus`), `insulin` (units), `carbs` (g), `duration` (min), `absolute` or `percent` (for temp basals), `notes`, `enteredBy`.
- **Filter / pagination:** `count=N`, `find[field][$gte|$lte|$eq|$in]=…`, `find[eventType]=Bolus`, etc. There is no official rate limit, but Nightscout instances are typically free-tier Heroku/Atlas, so be polite (cap `count`, cache profile/status).

**API v3 (newer, granular, JWT-secured):**
- Per the v3 tutorial in the repo: "Nightscout API v3 is a component of cgm-remote-monitor project. It aims to provide lightweight, secured and HTTP REST compliant interface for your T1D treatment data exchange." Reachable at `/api/v3` with OpenAPI at `/api/v3/swagger-ui-dist/`.
- Collections (`entries`, `treatments`, `devicestatus`, `profile`, `food`, `settings`) each support uniform operations: `LIST/SEARCH` (with `sort$desc=date`, `limit`, `skip`, `fields=…`), `CREATE`, `READ`, `UPDATE`, `PATCH`, `DELETE`, `HISTORY`, `LASTMODIFIED`.
- Per-collection CRUD permissions (`api:entries:read`, `api:treatments:create`, etc.) keyed off the JWT. The `settings` collection requires `admin`.
- Hard cap: `API3_MAX_LIMIT=1000` documents per query by default. Server-side autoprune defaults (`API3_AUTOPRUNE_DEVICESTATUS=60` days) mean old devicestatus rows may be missing.
- Auth flow:
  1. `GET /api/v2/authorization/request/{accessToken}` → `{ token: <JWT> }`.
  2. Send `Authorization: Bearer <JWT>` on every `/api/v3/*` call.
  3. JWT valid ~8 hours; refresh on 401.

**Authentication summary:**
- `API_SECRET` is the master password (≥12 chars). For HTTP, you send its SHA-1 hash, case-insensitive (issue #5724 was fixed), in the `api-secret` header: `api-secret: <sha1(API_SECRET)>`. Don't use this for an MCP server — too privileged, hard to rotate.
- **Access tokens** (preferred) are created in Admin Tools → Subjects, with roles `readable`, `careportal`, `admin`, or custom. They look like `myreader-ad3b1f9d7b3f59d5`. Pass as `?token=…` on v1, or exchange for a JWT on v3. For an MCP, mint two: one read-only role (`readable`) for Phase 1, one careportal-scoped role for Phase 2 writes.
- `AUTH_DEFAULT_ROLES=denied` on the Nightscout side ensures even reads require a token.

**Does AndroidAPS expose any direct API?** No public/external HTTP API. There is intra-app/inter-app communication on the Android device (broadcasts, Wear OS, AAPSClient companion) and some IFTTT/Automate recipes that all ultimately POST to Nightscout `/api/v1/treatments`. Community feature requests (e.g., issue #4497 to push heart-rate to NS) confirm the only viable cross-system data path is via Nightscout. **All programmatic access for an MCP should go through Nightscout.**

### 3. MCP suitability assessment

| Criterion | Nightscout REST | AndroidAPS direct |
|---|---|---|
| Documented HTTP API | Yes (v1 + v3, Swagger) | No external API |
| Reachable from a Python/Node MCP process | Yes — any URL | No — Android-bound |
| Read all relevant data (BG, treatments, profile, IOB/COB, devicestatus) | Yes | Yes only on-device |
| Write back treatments / notes / temp targets | Yes (`POST /treatments`) | Only via on-device intents |
| Auth model | API_SECRET + tokens + roles | Android per-app intents |
| Effort to integrate | Low (HTTP + JSON) | Very high (Android service / accessory app) |

**Recommendation:** Build the MCP server **exclusively against Nightscout's REST API**. This gives you full coverage of AAPS-uploaded data with none of the Android integration cost. If a future use case truly needs commands that affect the loop *immediately* (e.g., enacting a temp target), you can POST a `Temporary Target` treatment to Nightscout — AAPS's NSClientV3 will pick it up on its sync cycle (this is exactly how IFTTT-based remote overrides work today).

**Useful MCP tools to expose:**

Read tools (Phase 1):
- `get_current_glucose()` → latest SGV with `mg/dL`, `mmol/L`, `direction`, `delta`, `minutesAgo`.
- `get_glucose_history(hours: int = 6, count: int | None = None)` → time-series list of SGVs.
- `get_glucose_stats(hours: int = 24)` → mean, GMI/eA1c, SD, CV, time-in-range with configurable thresholds (default 70–180 mg/dL).
- `get_treatments(hours: int = 24, event_type: str | None = None)` → boluses/carbs/basals/notes.
- `get_insulin_on_board()` and `get_carbs_on_board()` → derived from recent treatments + profile DIA/CR (or pulled from the latest `devicestatus.openaps.suggested`).
- `get_current_profile()` → basal schedule, ISF, IC, target high/low, DIA, timezone.
- `get_device_status(latest: bool = true)` → pump reservoir/battery, loop status, last enacted temp basal.
- `get_server_status()` → version, units, features.
- `search_treatments(query, since, until)` → free-form retrieval.

Write tools (Phase 2, gated):
- `add_note(text: str, when: datetime | None = None)` — lowest-risk, write-tool training wheels.
- `add_carb_entry(grams: int, when, absorption_minutes: int | None)` — `eventType: "Carb Correction"` or `"Meal Bolus"` with `carbs` only.
- `add_bolus_entry(units: float, when, notes)` — `eventType: "Correction Bolus"`. (**Strongly recommend leaving this off by default**; logging an insulin dose can influence IOB and loop behavior even if no insulin is actually delivered.)
- `set_temporary_target(target_low, target_high, duration_minutes, reason)` — affects AAPS/Loop behavior on next sync.
- `add_exercise(duration_minutes, notes)` — informational.

MCP resources (optional but valuable) — read-only URI-addressable views the client can pull into context: `nightscout://entries/last24h`, `nightscout://profile/current`, `nightscout://stats/today`.

MCP prompts (FastMCP `@mcp.prompt`): "daily report", "explain the last spike", "review overnight basal".

### 4. Recommended stack and existing work

**Pick Python + FastMCP.**
- FastMCP is incorporated into the official MCP Python SDK and, per gofastmcp.com, "is downloaded a million times a day, and some version of FastMCP powers 70% of MCP servers across all languages." The decorator model (`@mcp.tool`, `@mcp.resource`, `@mcp.prompt`) turns a typed Python function into an MCP tool with automatic JSON-schema generation — ideal for a weekend build.
- `py-nightscout` (PyPI 1.3.3, MIT, async/`aiohttp`, last release Dec 5 2021) gives you `api.get_sgvs()`, `api.get_treatments()`, `api.get_profiles()` with typed objects, accepts either `api_secret` or a token via URL, and supports the same `find[…]` filters the Swagger docs describe. Drop-in for the data layer.
- TypeScript is a reasonable alternative (and would let you read `adminpb/Nightscout-MCP` directly), but Python is faster to iterate on for time-series math (mean/SD/TIR) and pairs better with notebooks for debugging glucose-statistic logic.

**Existing prior art:**
- **`adminpb/Nightscout-MCP`** (TypeScript, MIT) — read-only by default; 7 tools (`get_current_glucose`, `get_glucose_history`, `get_statistics`, `get_treatments`, `get_profile`, `get_device_status`, `get_daily_report`); env vars `NIGHTSCOUT_URL` + (`NIGHTSCOUT_TOKEN` | `NIGHTSCOUT_API_SECRET`); Zod validation; SHA-1 hashes API secrets before transmission; bilingual response strings (en/uk). Use this as a reference for tool naming and safety defaults.
- A Python/FastMCP listing exists at `mcpmarket.com/server/nightscout` ("Built with FastMCP… empowering AI assistants like Claude to read glucose…") but the source repo URL is not surfaced in public search; treat as informational only.
- **`nightscout/nocturne`** — a .NET 10 rewrite of Nightscout (AGPL-3.0). Its `src/Tools/` directory includes a "CLI tools and MCP server" subfolder, indicating a first-party MCP server is being developed by the Nightscout community for the next-gen platform. Worth watching, but Nocturne is not a Nightscout v15 user's reality today.
- Client libraries to lean on: `py-nightscout` (Python async, by `marciogranzotto`), `ps2/python-nightscout` (the original sync Python client), `ecc1/nightscout` (Go, complete typed models — useful as a fields reference), and `nightscout` (npm) for TypeScript.

### 5. Weekend build plan

**Prereqs (30 min, Friday night):**
- A reachable Nightscout instance. Easiest: use your existing one. To stand up a test one in <10 minutes, run the official Docker Compose from the README (MongoDB + cgm-remote-monitor) and set `API_SECRET=replace-with-12+chars` and `AUTH_DEFAULT_ROLES=denied`. For purely read-only API exploration, the public `nsapiv3.herokuapp.com` demo referenced in the v3 tutorial is a fine sandbox.
- In Nightscout → Admin Tools → Subjects, create two access tokens:
  - `mcp-reader` with role `readable`
  - `mcp-writer` with role `careportal` (Phase 2 only)
- Install Python 3.11+ and `uv`. `uv init nightscout-mcp && cd nightscout-mcp && uv add "mcp[cli]" fastmcp httpx py-nightscout pydantic python-dotenv`.

**Project structure:**
```
nightscout-mcp/
├── pyproject.toml
├── .env.example              # NIGHTSCOUT_URL, NIGHTSCOUT_TOKEN, NIGHTSCOUT_UNITS=mg/dL,
│                             # NIGHTSCOUT_ALLOW_WRITES=false, NIGHTSCOUT_WRITER_TOKEN=
├── src/nightscout_mcp/
│   ├── __init__.py
│   ├── server.py             # FastMCP() instance; tool/resource/prompt registration
│   ├── config.py             # Pydantic Settings, validates URL + token
│   ├── client.py             # Thin async wrapper over py-nightscout + httpx for v3
│   ├── stats.py              # TIR, GMI, SD/CV math (mg/dL + mmol/L aware)
│   ├── tools/
│   │   ├── read.py           # get_current_glucose, get_glucose_history, ...
│   │   └── write.py          # add_note, add_carb_entry, set_temporary_target
│   └── safety.py             # confirm-token / unit-sanity / range-guard helpers
└── tests/
```

**Phase 1 — Read-only MCP (Saturday, ~6 hours):**
1. **Hour 1.** Wire `config.py` + `client.py`. Hit `/api/v1/status.json` to verify URL/token, parse units (`mg/dL` or `mmol/L`), cache server version. Implement a single `nightscout_get(path, params)` async helper.
2. **Hour 2.** Implement `get_current_glucose` and `get_glucose_history` using `py-nightscout`'s `api.get_sgvs({'count': N})`. Standardize the return shape: `{value_mgdl, value_mmol, direction, trend_arrow, delta_mgdl, minutes_ago, iso_time}`. Format direction strings into ASCII trend arrows for the LLM.
3. **Hour 3.** Implement `get_treatments(hours, event_type)`. Translate `find[created_at][$gte]` filters from a `hours: int` argument. Implement `get_current_profile`, `get_device_status(latest=True)`, `get_server_status`.
4. **Hour 4.** Implement `get_glucose_stats(hours)` in `stats.py`: mean, SD, CV%, TIR (default 70–180), TBR <70 / <54, TAR >180 / >250, and estimated A1c using the published Glucose Management Indicator formula `GMI(%) = 3.31 + 0.02392 × mean_mgdl` (Bergenstal RM, Beck RW, Close KL., "Glucose management indicator (GMI): A new term for estimating A1C from continuous glucose monitoring," *Diabetes Care* 2018; 41:2275–2280, doi:10.2337/dc18-1581, as published on the Jaeb Center for Health Research GMI calculator at jaeb.org/gmi/). Make threshold args optional so the LLM can override.
5. **Hour 5.** Add MCP resources (`nightscout://summary/today`, `nightscout://profile/current`) and one prompt template (`@mcp.prompt def daily_review(date: str)` returning a structured analyst-style instruction).
6. **Hour 6.** Test against MCP Inspector (`uv run mcp dev src/nightscout_mcp/server.py`). Hook into Claude Desktop via `~/Library/Application Support/Claude/claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "nightscout": {
         "command": "uv",
         "args": ["--directory", "/abs/path/to/nightscout-mcp",
                  "run", "python", "-m", "nightscout_mcp.server"],
         "env": {
           "NIGHTSCOUT_URL": "https://you.nightscout.example",
           "NIGHTSCOUT_TOKEN": "mcp-reader-xxxxxxxxxx",
           "NIGHTSCOUT_UNITS": "mg/dL"
         }
       }
     }
   }
   ```

**Phase 2 — Gated write access (Sunday, ~4 hours):**
1. **Hour 1.** Add `NIGHTSCOUT_ALLOW_WRITES=true|false` and `NIGHTSCOUT_WRITER_TOKEN` env vars. In `safety.py`, raise immediately if a write tool is called and writes are disabled. Use the *separate* `mcp-writer` token, never the `API_SECRET`.
2. **Hour 2.** Implement `add_note(text, when=None)` first — the safest write. POST to `/api/v1/treatments` with `{ eventType: "Note", notes, enteredBy: "mcp", created_at }`. Validate `text` length, strip control chars.
3. **Hour 3.** Implement `add_carb_entry(grams, when, absorption_minutes)` with sanity bounds (`0 < grams <= 200`, `when` within ±24h of now, future-dated requires extra confirm flag). Implement `set_temporary_target(target_low, target_high, duration_minutes, reason)` with bounds (`72 <= target <= 270 mg/dL` per AAPS automation limits, `5 <= duration <= 240`).
4. **Hour 4.** Add an idempotency token: every write tool requires a `confirmation_phrase: str` argument the user/LLM must echo back (e.g., the carbs and time). Log every write call to a local `~/.nightscout-mcp/writes.log` with timestamp, tool, payload, and Nightscout response. Add a `dry_run` boolean defaulting to `False` on the most powerful tool (`set_temporary_target`).

**Stretch (optional):**
- Switch the client to API v3 with JWT (`/api/v2/authorization/request/{token}` → cache for ~7h → Bearer). Gives you proper per-collection permissions and pagination via `limit`/`skip`/`sort$desc=date`.
- Add a "diff since I last asked" resource using v3's `LASTMODIFIED`/`HISTORY` endpoints for efficient incremental queries.
- Package as a `uvx`-installable script so Claude Desktop users can `uvx nightscout-mcp` without cloning.

### Safety, privacy, and medical-data considerations

- **This is health/PHI data.** Nightscout's own security doc states the project "currently makes no attempt at HIPAA privacy compliance" and "no password protected privacy or security provided by these tools; all data you upload can be available for anyone on the Internet to read if they have your specific URL." Treat the MCP as a personal tool only — never multi-tenant, never shared.
- **Never ship the `API_SECRET` to the MCP process.** Use access tokens. Rotate by changing `API_SECRET` (which invalidates all tokens) periodically.
- **Tokens must never enter the LLM context.** The MCP server should hold them in env vars and use them in HTTP headers only; tool responses must contain only derived data. The `adminpb/Nightscout-MCP` README explicitly documents this pattern ("Tokens never reach the AI — only processed data is returned via MCP") and it should be your default too.
- **Cleartext HTTP is unsafe** — require `https://` for `NIGHTSCOUT_URL` and refuse to start otherwise. AAPS docs themselves emphasize "Nightscout API_SECRET is your site main password: don't share it publicly."
- **No medical advice.** The MCP descriptions and prompts should be explicit that the assistant is summarizing logged data only and is not providing clinical recommendations. Mirror Nightscout's disclaimer: "Use Nightscout at your own risk, and do not use the information or code to make medical decisions."
- **Write safety:** every write tool must (a) require `NIGHTSCOUT_ALLOW_WRITES=true`, (b) use a careportal-scoped token, (c) demand an explicit confirmation argument, (d) validate units and clamp to sane ranges, (e) tag `enteredBy: "mcp"` and a hostname so the user can audit/delete entries, (f) log locally. Consider not exposing `add_bolus_entry` at all — even logging a bolus that wasn't actually delivered can mislead IOB calculations and loop decisions in AAPS/Loop.
- **AAPS commands:** if you do post `Temporary Target` or `Temporary Override` treatments, AAPS will pick them up on its next NSClient sync (typically within a minute on cellular). Document this latency to the user; don't claim "instant" enactment.

---

## Recommendations

**Do now (Friday/Saturday):**
1. Stand up Phase 1 in Python + FastMCP + `py-nightscout`, targeting Nightscout API v1. Ship the 7–9 read tools listed above and the `nightscout://summary/today` resource.
2. Use access tokens, not the API_SECRET. Set `AUTH_DEFAULT_ROLES=denied` on your Nightscout if it isn't already.
3. Test against MCP Inspector, then connect to Claude Desktop with the config block above.

**Do next (Sunday):**
4. Add Phase 2 writes behind `NIGHTSCOUT_ALLOW_WRITES=true` and a *separate* careportal-only token. Start with `add_note` and `add_carb_entry`; defer or omit `add_bolus_entry`.
5. Add a local write-audit log and a `confirmation_phrase` argument on every write tool.

**Defer (next sprint):**
6. Migrate the client to API v3 with JWT once you need per-collection RBAC, large-window pagination via `limit`/`skip`, or incremental sync via `LASTMODIFIED`.
7. Watch `nightscout/nocturne` — if its first-party MCP server (`src/Tools/` MCP subfolder) lands, evaluate replacing your custom server with it or contributing tools upstream.

**Thresholds that would change the plan:**
- If you find you need closed-loop *commands* with sub-minute latency (e.g., cancel a pump bolus in progress), Nightscout-as-bus won't cut it — you'd need a custom AAPS plugin and that is no longer a weekend project.
- If multiple users would share the MCP, drop write tools entirely and put an OAuth proxy in front. Personal/single-user only is the only sane scope here.

---

## Caveats

- **Nightscout itself is non-HIPAA, AGPL-3.0 community software** explicitly intended for educational use; the same disclaimer must propagate to anything you build on top.
- **`py-nightscout`'s last PyPI release was Dec 5, 2021 (1.3.3).** It still works against modern Nightscout v15 because the v1 API is stable, but you may need to add a small shim for the `token=` query param if you don't want to pass `api_secret`. The original `ps2/python-nightscout` is similarly low-activity.
- **The Python/FastMCP Nightscout listing on `mcpmarket.com/server/nightscout` could not be traced to a public GitHub repo** at the time of writing; if you want to study its exact tool surface before building, you'll need to browse the mcpmarket page in a real browser (their site blocks automated fetchers).
- **API v3 specifics evolve** — community discussion on `nightscout/xDrip` #4462 notes the JWT-exchange flow ("You cannot connect directly to the api/v3 using the role token from nightscout. Auth flow is documented in NightscoutFollowV3"). Build against v1 first; treat v3 as a deliberate later migration.
- **AAPS write effects are real.** Anything you POST to `/api/v1/treatments` that AAPS recognizes (carbs, temp targets, profile switches, overrides) will influence the loop's next decision once NSClientV3 syncs. This is not a sandbox — confirmation gates and small-blast-radius defaults are mandatory.