"""Reader for the aaps-history-ingest local SQLite store.

Pure data-access functions; no MCP coupling. Tool wrappers in
src/nightscout_mcp/tools/aaps_history.py call into these.

The SQLite DB lives outside this repo and is populated by the
separate aaps-history-ingest service:
  https://github.com/ColebyPearson/aaps-history-ingest (private)

The DB path is resolved via env var `AAPS_INGEST_DB_PATH` with a
sensible default of `C:\\Repos\\aaps-history-ingest\\data\\ingest.sqlite`.
If the file does not exist, every query returns an empty result with
a `db_missing=True` flag — this means the MCP can be installed and
used without the ingest pipeline; the tools just have nothing to
report. Once the ingest pipeline has at least one snapshot, the tools
start returning data automatically.

Tables read:
  settings_snapshots(id, captured_at, file_hash, aaps_version,
                     device_name_hashed, n_keys, full_json, ingested_at)
  settings_change_events(id, snapshot_id, key, change_type,
                         previous_value, new_value, detected_at)

Optional tables (populated when log ingestion lands — Track 4):
  log_user_entries(id, ts, event_type, source, raw_text, values_json)
  log_errors(id, ts, level, category, message)
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "AAPS_INGEST_DB_PATH",
        r"C:\Repos\aaps-history-ingest\data\ingest.sqlite",
    )
)


@dataclass
class StoreLocator:
    """Resolved DB path + existence info."""

    path: Path
    exists: bool


def resolve_db(path: Path | None = None) -> StoreLocator:
    """Return whether the AAPS history DB is present at the configured path."""
    p = Path(path) if path else DEFAULT_DB_PATH
    return StoreLocator(path=p, exists=p.is_file())


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class SnapshotRow:
    id: int
    captured_at: str
    file_hash: str
    aaps_version: str | None
    n_keys: int


@dataclass
class ChangeEventRow:
    detected_at: str
    snapshot_captured_at: str
    aaps_version: str | None
    key: str
    change_type: str  # 'added' | 'removed' | 'changed'
    previous_value: Any  # already JSON-decoded
    new_value: Any  # already JSON-decoded


@dataclass
class SettingValueAt:
    """Most-recent value of a setting at or before a given timestamp."""

    key: str
    value: Any | None  # JSON-decoded; None if key missing in the snapshot
    found_in_snapshot: bool
    snapshot_captured_at: str | None
    aaps_version_at_snapshot: str | None


@dataclass
class HistoryReport:
    key: str
    days_back: int
    events: list[ChangeEventRow] = field(default_factory=list)
    current_value: Any | None = None
    current_snapshot_captured_at: str | None = None


@dataclass
class DiffReport:
    from_iso: str
    to_iso: str
    changes: list[ChangeEventRow] = field(default_factory=list)


# --- Snapshot + change-event reads ----------------------------------------


def list_snapshots(db_path: Path | None = None, limit: int = 30) -> list[SnapshotRow]:
    """List the most recent snapshots, newest first."""
    loc = resolve_db(db_path)
    if not loc.exists:
        return []
    conn = _connect(loc.path)
    try:
        rows = conn.execute(
            """
            SELECT id, captured_at, file_hash, aaps_version, n_keys
            FROM settings_snapshots
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        SnapshotRow(
            id=r["id"],
            captured_at=r["captured_at"],
            file_hash=r["file_hash"],
            aaps_version=r["aaps_version"],
            n_keys=r["n_keys"] or 0,
        )
        for r in rows
    ]


def setting_at(
    key: str, time_iso: str, db_path: Path | None = None
) -> SettingValueAt:
    """Return the value of `key` as it was at `time_iso`.

    Looks up the snapshot with the latest `captured_at` <= time_iso and
    extracts `key` from its full_json blob.
    """
    loc = resolve_db(db_path)
    if not loc.exists:
        return SettingValueAt(
            key=key,
            value=None,
            found_in_snapshot=False,
            snapshot_captured_at=None,
            aaps_version_at_snapshot=None,
        )
    conn = _connect(loc.path)
    try:
        row = conn.execute(
            """
            SELECT captured_at, aaps_version, full_json
            FROM settings_snapshots
            WHERE captured_at <= ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (time_iso,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return SettingValueAt(
            key=key,
            value=None,
            found_in_snapshot=False,
            snapshot_captured_at=None,
            aaps_version_at_snapshot=None,
        )
    prefs = json.loads(row["full_json"])
    return SettingValueAt(
        key=key,
        value=prefs.get(key),
        found_in_snapshot=key in prefs,
        snapshot_captured_at=row["captured_at"],
        aaps_version_at_snapshot=row["aaps_version"],
    )


def setting_history(
    key: str, days_back: int = 365, db_path: Path | None = None
) -> HistoryReport:
    """Return all change events for a single setting within the window."""
    loc = resolve_db(db_path)
    report = HistoryReport(key=key, days_back=days_back)
    if not loc.exists:
        return report
    cutoff_iso = _iso_n_days_ago(days_back)
    conn = _connect(loc.path)
    try:
        rows = conn.execute(
            """
            SELECT e.detected_at, s.captured_at AS snapshot_captured_at,
                   s.aaps_version,
                   e.change_type, e.previous_value, e.new_value
            FROM settings_change_events e
            JOIN settings_snapshots s ON s.id = e.snapshot_id
            WHERE e.key = ?
              AND e.detected_at >= ?
            ORDER BY e.detected_at ASC
            """,
            (key, cutoff_iso),
        ).fetchall()

        latest_snap = conn.execute(
            """
            SELECT captured_at, full_json
            FROM settings_snapshots
            ORDER BY captured_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    for r in rows:
        report.events.append(
            ChangeEventRow(
                detected_at=r["detected_at"],
                snapshot_captured_at=r["snapshot_captured_at"],
                aaps_version=r["aaps_version"],
                key=key,
                change_type=r["change_type"],
                previous_value=_safe_json(r["previous_value"]),
                new_value=_safe_json(r["new_value"]),
            )
        )

    if latest_snap:
        latest_prefs = json.loads(latest_snap["full_json"])
        report.current_value = latest_prefs.get(key)
        report.current_snapshot_captured_at = latest_snap["captured_at"]

    return report


def settings_diff(
    from_iso: str, to_iso: str, db_path: Path | None = None
) -> DiffReport:
    """Return all change events with `detected_at` in [from_iso, to_iso]."""
    loc = resolve_db(db_path)
    report = DiffReport(from_iso=from_iso, to_iso=to_iso)
    if not loc.exists:
        return report
    conn = _connect(loc.path)
    try:
        rows = conn.execute(
            """
            SELECT e.detected_at, s.captured_at AS snapshot_captured_at,
                   s.aaps_version, e.key, e.change_type,
                   e.previous_value, e.new_value
            FROM settings_change_events e
            JOIN settings_snapshots s ON s.id = e.snapshot_id
            WHERE e.detected_at >= ?
              AND e.detected_at <  ?
            ORDER BY e.detected_at ASC, e.key ASC
            """,
            (from_iso, to_iso),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        report.changes.append(
            ChangeEventRow(
                detected_at=r["detected_at"],
                snapshot_captured_at=r["snapshot_captured_at"],
                aaps_version=r["aaps_version"],
                key=r["key"],
                change_type=r["change_type"],
                previous_value=_safe_json(r["previous_value"]),
                new_value=_safe_json(r["new_value"]),
            )
        )
    return report


# --- Log queries (Track 4 — graceful empty until that table exists) -------


@dataclass
class LogUserEntry:
    ts: str
    event_type: str
    source: str | None
    raw_text: str | None
    values: dict[str, Any] | None


def log_user_entries(
    start_iso: str,
    end_iso: str,
    event_types: list[str] | None = None,
    db_path: Path | None = None,
) -> tuple[list[LogUserEntry], bool]:
    """Return USER ENTRY rows from the log archive.

    Returns (entries, log_table_present). When the log_user_entries table
    has not been populated by Track 4 yet, returns ([], False) so the tool
    wrapper can surface a clear message.
    """
    loc = resolve_db(db_path)
    if not loc.exists:
        return ([], False)
    conn = _connect(loc.path)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='log_user_entries'"
        ).fetchone()
        if not present:
            return ([], False)
        query = (
            "SELECT ts, event_type, source, raw_text, values_json "
            "FROM log_user_entries WHERE ts >= ? AND ts < ? "
        )
        params: list[Any] = [start_iso, end_iso]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            query += f"AND event_type IN ({placeholders}) "
            params.extend(event_types)
        query += "ORDER BY ts ASC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    out: list[LogUserEntry] = []
    for r in rows:
        out.append(
            LogUserEntry(
                ts=r["ts"],
                event_type=r["event_type"],
                source=r["source"],
                raw_text=r["raw_text"],
                values=_safe_json(r["values_json"]) if r["values_json"] else None,
            )
        )
    return (out, True)


# --- Helpers ---------------------------------------------------------------


def _iso_n_days_ago(days_back: int) -> str:
    from datetime import UTC, timedelta

    return (datetime.now(UTC) - timedelta(days=max(1, int(days_back)))).isoformat()


def _safe_json(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
