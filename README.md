# nightscout-mcp

> A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM (Claude Desktop, Claude Code, any MCP client) read glucose, treatments, and derived analytics from a personal [Nightscout](https://github.com/nightscout/cgm-remote-monitor) instance.

**Status:** 🚧 Alpha — Phase 0 scaffolding. See [PLAN.md](./PLAN.md) for the full roadmap.

## ⚠️ Disclaimer

This tool reads from your Nightscout instance and surfaces glucose data to an LLM. **It is not a medical device, does not provide clinical advice, and must not be used to make treatment decisions.** Nightscout itself states it "currently makes no attempt at HIPAA privacy compliance" and is intended for educational use only. Use at your own risk.

## What it does (Phase 1 + 2 — coming)

Read-only MCP tools backed by the Nightscout REST API v1:

- `get_current_glucose` — latest SGV with trend, delta, age
- `get_glucose_history` — time-series SGVs over a window
- `get_glucose_stats` — mean, SD, CV%, TIR, GMI, TBR/TAR breakdowns
- `get_treatments` — boluses, carbs, basals, notes
- `get_iob_cob` — insulin/carbs on board (from `devicestatus`)
- `get_current_profile` — basal schedule, ISF, IC, DIA
- `get_device_status` — pump/loop/uploader state
- `get_server_status` — Nightscout version, units, features
- `search_treatments` — free-form retrieval
- *Analytics (Phase 2):* `detect_patterns`, `compare_periods`, `analyze_meal`, `overnight_analysis`, `get_daily_report`

Writes are deferred. See [PLAN.md §4 Phase 3](./PLAN.md) for the rationale.

## Install (once Phase 0 lands on GitHub)

```bash
uv tool install nightscout-mcp
```

Or from source:

```bash
git clone https://github.com/<user>/nightscout-mcp
cd nightscout-mcp
uv sync
```

## Configure

1. In Nightscout → **Admin Tools → Subjects**, create a token with the `readable` role (e.g. `mcp-reader`).
2. Copy `.env.example` to `.env` and fill in `NIGHTSCOUT_URL` (must be `https://`) and `NIGHTSCOUT_TOKEN`.

## Run

### MCP Inspector (development)

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

## Safety model

- Token lives in env vars only; never returned to the LLM in tool responses.
- `NIGHTSCOUT_URL` must be `https://` — the server refuses to start otherwise.
- Use a `readable`-role access token, never the raw `API_SECRET`.
- All numeric outputs include both `mg/dL` and `mmol/L` regardless of `NIGHTSCOUT_UNITS`.

## License

[MIT](./LICENSE). Nightscout itself is [AGPL-3.0](https://github.com/nightscout/cgm-remote-monitor/blob/master/LICENSE) — this project consumes its HTTP API but does not include or redistribute its code.

## Acknowledgments

- The [Nightscout Foundation](https://nightscout.github.io/) and the wider #WeAreNotWaiting community.
- [`adminpb/Nightscout-MCP`](https://github.com/adminpb/Nightscout-MCP) — TypeScript prior art that informed the tool surface.
- [FastMCP](https://gofastmcp.com) / the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
