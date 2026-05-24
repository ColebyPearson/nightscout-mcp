"""Tests for the AAPS history reader + MCP tool wrappers.

Uses temp SQLite DBs constructed with the same schema as
aaps-history-ingest to exercise the queries end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nightscout_mcp import aaps_history as ah
from nightscout_mcp.tools import aaps_history as ah_tools

SCHEMA_SQL = """
CREATE TABLE settings_snapshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    file_hash TEXT UNIQUE NOT NULL,
    aaps_version TEXT,
    device_name_hashed TEXT,
    n_keys INTEGER,
    full_json BLOB,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE settings_change_events (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER REFERENCES settings_snapshots(id),
    key TEXT NOT NULL,
    change_type TEXT NOT NULL,
    previous_value TEXT,
    new_value TEXT,
    detected_at TEXT NOT NULL
);
"""


def _make_db(
    tmp_path: Path,
    snapshots: list[dict],
    events: list[dict] | None = None,
    add_log_table: bool = False,
    log_rows: list[dict] | None = None,
) -> Path:
    """Construct a temp SQLite DB with the ingest schema + given rows."""
    db = tmp_path / "ingest.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    for s in snapshots:
        conn.execute(
            "INSERT INTO settings_snapshots "
            "(captured_at, file_hash, aaps_version, n_keys, full_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                s["captured_at"],
                s["file_hash"],
                s.get("aaps_version", "3.4.2.2"),
                s.get("n_keys", len(s.get("prefs", {}))),
                json.dumps(s.get("prefs", {})),
            ),
        )
    if events:
        for e in events:
            conn.execute(
                "INSERT INTO settings_change_events "
                "(snapshot_id, key, change_type, previous_value, new_value, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    e["snapshot_id"],
                    e["key"],
                    e["change_type"],
                    json.dumps(e["previous_value"]) if e.get("previous_value") is not None else None,
                    json.dumps(e["new_value"]) if e.get("new_value") is not None else None,
                    e["detected_at"],
                ),
            )
    if add_log_table:
        conn.executescript(
            """
            CREATE TABLE log_user_entries (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT,
                raw_text TEXT,
                values_json TEXT
            );
            """
        )
        for r in log_rows or []:
            conn.execute(
                "INSERT INTO log_user_entries (ts, event_type, source, raw_text, values_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    r["ts"],
                    r["event_type"],
                    r.get("source"),
                    r.get("raw_text"),
                    json.dumps(r["values"]) if r.get("values") is not None else None,
                ),
            )
    conn.commit()
    conn.close()
    return db


# --- Reader (pure-function) tests ------------------------------------------


def test_resolve_db_missing(tmp_path: Path):
    loc = ah.resolve_db(tmp_path / "nope.sqlite")
    assert loc.exists is False
    assert loc.path == tmp_path / "nope.sqlite"


def test_list_snapshots_empty(tmp_path: Path):
    db = _make_db(tmp_path, snapshots=[])
    rows = ah.list_snapshots(db)
    assert rows == []


def test_list_snapshots_returns_newest_first(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-22T23:55:00Z", "file_hash": "a", "prefs": {"k": 1}},
            {"captured_at": "2026-05-24T23:55:00Z", "file_hash": "b", "prefs": {"k": 2}},
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "c", "prefs": {"k": 3}},
        ],
    )
    rows = ah.list_snapshots(db)
    assert len(rows) == 3
    assert rows[0].captured_at == "2026-05-24T23:55:00Z"  # newest first
    assert rows[-1].captured_at == "2026-05-22T23:55:00Z"


def test_setting_at_returns_value_from_latest_snapshot_at_or_before_time(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-20T23:55:00Z", "file_hash": "a",
             "prefs": {"DynISFAdjust": 35}},
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "b",
             "prefs": {"DynISFAdjust": 30}},
        ],
    )
    # Query between the two snapshots → returns the older one
    result = ah.setting_at("DynISFAdjust", "2026-05-22T12:00:00Z", db)
    assert result.value == 35
    assert result.found_in_snapshot is True
    # Query after both → returns the newer
    result = ah.setting_at("DynISFAdjust", "2026-05-24T12:00:00Z", db)
    assert result.value == 30


def test_setting_at_returns_no_snapshot_when_too_early(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "a",
             "prefs": {"k": 1}},
        ],
    )
    result = ah.setting_at("k", "2025-01-01T00:00:00Z", db)
    assert result.value is None
    assert result.snapshot_captured_at is None
    assert result.found_in_snapshot is False


def test_setting_at_key_missing_in_snapshot(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "a",
             "prefs": {"other_key": 1}},
        ],
    )
    result = ah.setting_at("DynISFAdjust", "2026-05-24T12:00:00Z", db)
    assert result.value is None
    assert result.found_in_snapshot is False
    assert result.snapshot_captured_at == "2026-05-23T23:55:00Z"


def test_setting_history_returns_events(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-24T23:55:00Z", "file_hash": "a",
             "prefs": {"DynISFAdjust": 30}},
        ],
        events=[
            {"snapshot_id": 1, "key": "DynISFAdjust", "change_type": "changed",
             "previous_value": 35, "new_value": 30,
             "detected_at": "2026-05-24T23:55:00Z"},
            {"snapshot_id": 1, "key": "autosens_min", "change_type": "changed",
             "previous_value": 0.2, "new_value": 0.7,
             "detected_at": "2026-05-24T23:55:00Z"},
        ],
    )
    history = ah.setting_history("DynISFAdjust", days_back=365, db_path=db)
    assert len(history.events) == 1
    assert history.events[0].previous_value == 35
    assert history.events[0].new_value == 30
    assert history.current_value == 30


def test_settings_diff_filters_by_window(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "a",
             "prefs": {"k": "v"}},
        ],
        events=[
            {"snapshot_id": 1, "key": "DynISFAdjust", "change_type": "changed",
             "previous_value": 35, "new_value": 30,
             "detected_at": "2026-05-23T10:29:45Z"},
            {"snapshot_id": 1, "key": "autosens_min", "change_type": "changed",
             "previous_value": 0.2, "new_value": 0.7,
             "detected_at": "2026-05-23T11:00:00Z"},
            {"snapshot_id": 1, "key": "old_change", "change_type": "added",
             "previous_value": None, "new_value": 1,
             "detected_at": "2026-05-20T00:00:00Z"},
        ],
    )
    diff = ah.settings_diff("2026-05-23T00:00:00Z", "2026-05-24T00:00:00Z", db)
    keys = sorted({c.key for c in diff.changes})
    assert keys == ["DynISFAdjust", "autosens_min"]


def test_log_user_entries_returns_false_when_table_missing(tmp_path: Path):
    db = _make_db(tmp_path, snapshots=[])
    entries, present = ah.log_user_entries(
        "2026-05-23T00:00:00Z", "2026-05-25T00:00:00Z", db_path=db
    )
    assert present is False
    assert entries == []


def test_log_user_entries_returns_rows_when_table_present(tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[],
        add_log_table=True,
        log_rows=[
            {"ts": "2026-05-23T10:29:45Z", "event_type": "PROFILE_REMOVED",
             "source": "LocalProfile", "raw_text": "row1",
             "values": {"name": "LocalProfile2"}},
            {"ts": "2026-05-23T11:53:16Z", "event_type": "BOLUS",
             "source": "SMS", "raw_text": "row2",
             "values": {"units": 1.0}},
            {"ts": "2026-05-22T00:00:00Z", "event_type": "BOLUS",
             "source": "SMS", "raw_text": "out-of-window",
             "values": {"units": 0.5}},
        ],
    )
    entries, present = ah.log_user_entries(
        "2026-05-23T00:00:00Z", "2026-05-24T00:00:00Z", db_path=db
    )
    assert present is True
    assert len(entries) == 2
    # Type filter
    entries_filtered, _ = ah.log_user_entries(
        "2026-05-23T00:00:00Z", "2026-05-24T00:00:00Z",
        event_types=["BOLUS"], db_path=db
    )
    assert len(entries_filtered) == 1
    assert entries_filtered[0].event_type == "BOLUS"
    assert entries_filtered[0].values == {"units": 1.0}


# --- Tool wrapper tests (with monkeypatched DB path) -----------------------


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def d(f):
            self.tools[f.__name__] = f
            return f

        return d


def _client_stub():
    return None  # tools don't actually use it


@pytest.mark.asyncio
async def test_tool_aaps_history_status_db_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AAPS_INGEST_DB_PATH", str(tmp_path / "nope.sqlite"))
    # Reload module to pick up env var
    import importlib

    import nightscout_mcp.aaps_history as ah_module
    importlib.reload(ah_module)
    importlib.reload(ah_tools)

    reg = _Reg()
    ah_tools.register(reg, _client_stub)
    result = await reg.tools["aaps_history_status"]()
    assert result.db_present is False
    assert "not found" in result.note


@pytest.mark.asyncio
async def test_tool_aaps_setting_at_db_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AAPS_INGEST_DB_PATH", str(tmp_path / "nope.sqlite"))
    import importlib

    import nightscout_mcp.aaps_history as ah_module
    importlib.reload(ah_module)
    importlib.reload(ah_tools)

    reg = _Reg()
    ah_tools.register(reg, _client_stub)
    result = await reg.tools["aaps_setting_at"](key="DynISFAdjust", time_iso="2026-05-24T00:00:00Z")
    assert result.value is None
    assert result.found_in_snapshot is False
    assert "not found" in result.note


@pytest.mark.asyncio
async def test_tool_aaps_setting_at_resolves_from_snapshot(monkeypatch, tmp_path: Path):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "a",
             "prefs": {"DynISFAdjust": 30, "autosens_min": 0.7}},
        ],
    )
    monkeypatch.setenv("AAPS_INGEST_DB_PATH", str(db))
    import importlib

    import nightscout_mcp.aaps_history as ah_module
    importlib.reload(ah_module)
    importlib.reload(ah_tools)

    reg = _Reg()
    ah_tools.register(reg, _client_stub)
    result = await reg.tools["aaps_setting_at"](
        key="DynISFAdjust", time_iso="2026-05-24T12:00:00Z"
    )
    assert result.value == 30
    assert result.found_in_snapshot is True
    assert result.snapshot_captured_at == "2026-05-23T23:55:00Z"


@pytest.mark.asyncio
async def test_tool_aaps_settings_diff_returns_change_events(
    monkeypatch, tmp_path: Path
):
    db = _make_db(
        tmp_path,
        snapshots=[
            {"captured_at": "2026-05-23T23:55:00Z", "file_hash": "a",
             "prefs": {"k": "v"}},
        ],
        events=[
            {"snapshot_id": 1, "key": "DynISFAdjust", "change_type": "changed",
             "previous_value": 35, "new_value": 30,
             "detected_at": "2026-05-23T10:29:45Z"},
        ],
    )
    monkeypatch.setenv("AAPS_INGEST_DB_PATH", str(db))
    import importlib

    import nightscout_mcp.aaps_history as ah_module
    importlib.reload(ah_module)
    importlib.reload(ah_tools)

    reg = _Reg()
    ah_tools.register(reg, _client_stub)
    diff = await reg.tools["aaps_settings_diff"](
        from_iso="2026-05-23T00:00:00Z", to_iso="2026-05-24T00:00:00Z"
    )
    assert diff.change_count == 1
    assert diff.changes[0].key == "DynISFAdjust"
    assert diff.changes[0].previous_value == 35
    assert diff.changes[0].new_value == 30
