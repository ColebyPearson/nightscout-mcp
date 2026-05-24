"""AAPS history MCP tools — Track 5.

Surfaces the aaps-history-ingest local SQLite store to the LLM client.
The ingest pipeline (separate repo) decrypts daily AAPS settings exports
and writes snapshot + change-event tables into a local SQLite file. These
tools read from it; they never write.

Tools added (5):
- aaps_history_status            Reports DB presence + snapshot inventory
- aaps_setting_at                Most-recent value of a setting at a time
- aaps_setting_history           Timeline of every change to one setting
- aaps_settings_diff             All changes between two timestamps
- aaps_log_user_entries          USER ENTRY rows from log archive (when populated)

When the ingest DB is not present (e.g. on a fresh nightscout-mcp install
without the ingest service running), every tool returns a structured
"empty + note" response explaining how to enable the feature. This means
the MCP is fully usable without the ingest service — these tools just
have nothing to surface until it's set up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import aaps_history as ah
from ..client import NightscoutClient
from ..models import (
    AapsHistoryStoreStatus,
    AapsLogUserEntries,
    AapsLogUserEntry,
    AapsSettingChangeEvent,
    AapsSettingHistory,
    AapsSettingsDiff,
    AapsSettingValue,
)


def _no_db_note(path: str) -> str:
    return (
        f"AAPS ingest DB not found at {path}. "
        "Set up the companion aaps-history-ingest service (see "
        "https://github.com/ColebyPearson/aaps-history-ingest) and run "
        "`python -m ingest setpass` then `python -m ingest run`, or override "
        "the path via env var AAPS_INGEST_DB_PATH."
    )


def _to_event(row: ah.ChangeEventRow) -> AapsSettingChangeEvent:
    return AapsSettingChangeEvent(
        detected_at=row.detected_at,
        snapshot_captured_at=row.snapshot_captured_at,
        aaps_version=row.aaps_version,
        key=row.key,
        change_type=row.change_type,
        previous_value=row.previous_value,
        new_value=row.new_value,
    )


def register(mcp: Any, get_client: Callable[[], NightscoutClient]) -> None:
    """Attach the AAPS history tools to a FastMCP instance.

    `get_client` is unused (these tools talk to a local SQLite store, not
    Nightscout), but kept in the signature to match the existing tool-module
    convention.
    """
    _ = get_client  # unused; future tools may compose with Nightscout

    @mcp.tool()
    async def aaps_history_status() -> AapsHistoryStoreStatus:
        """Report the state of the AAPS ingest SQLite store.

        Useful for confirming that the companion aaps-history-ingest service
        is running and producing snapshots. Returns the DB path, whether the
        file exists, how many snapshots are stored, and the date range
        covered.
        """
        loc = ah.resolve_db()
        if not loc.exists:
            return AapsHistoryStoreStatus(
                db_path=str(loc.path),
                db_present=False,
                snapshot_count=0,
                latest_snapshot_iso=None,
                earliest_snapshot_iso=None,
                latest_aaps_version=None,
                note=_no_db_note(str(loc.path)),
            )
        rows = ah.list_snapshots(limit=10_000)
        if not rows:
            return AapsHistoryStoreStatus(
                db_path=str(loc.path),
                db_present=True,
                snapshot_count=0,
                latest_snapshot_iso=None,
                earliest_snapshot_iso=None,
                latest_aaps_version=None,
                note="DB present but no snapshots stored yet.",
            )
        return AapsHistoryStoreStatus(
            db_path=str(loc.path),
            db_present=True,
            snapshot_count=len(rows),
            latest_snapshot_iso=rows[0].captured_at,
            earliest_snapshot_iso=rows[-1].captured_at,
            latest_aaps_version=rows[0].aaps_version,
            note=f"{len(rows)} snapshot(s) available.",
        )

    @mcp.tool()
    async def aaps_setting_at(key: str, time_iso: str) -> AapsSettingValue:
        """Return the value of an AAPS setting at a point in time.

        Reads the most recent snapshot captured at or before `time_iso` and
        extracts the named `key`.

        Args:
            key: AAPS preference key, e.g. "DynISFAdjust", "autosens_min",
                 "LocalProfile_isf_0".
            time_iso: ISO 8601 timestamp like "2026-05-23T23:58:56Z".
                      Most-recent snapshot at or before this time is used.

        Returns the value (decoded from JSON when it was a JSON-shaped
        preference, otherwise a plain string), plus metadata about which
        snapshot supplied the answer.
        """
        loc = ah.resolve_db()
        if not loc.exists:
            return AapsSettingValue(
                key=key,
                queried_time_iso=time_iso,
                value=None,
                found_in_snapshot=False,
                snapshot_captured_at=None,
                aaps_version_at_snapshot=None,
                note=_no_db_note(str(loc.path)),
            )
        result = ah.setting_at(key, time_iso)
        if not result.snapshot_captured_at:
            note = (
                "No snapshot exists at or before the queried time. "
                "Earliest snapshot may be later than this query."
            )
        elif not result.found_in_snapshot:
            note = (
                f"Snapshot from {result.snapshot_captured_at} found, but the "
                f"key '{key}' was not in it."
            )
        else:
            note = f"Value resolved from snapshot captured {result.snapshot_captured_at}."
        return AapsSettingValue(
            key=key,
            queried_time_iso=time_iso,
            value=result.value,
            found_in_snapshot=result.found_in_snapshot,
            snapshot_captured_at=result.snapshot_captured_at,
            aaps_version_at_snapshot=result.aaps_version_at_snapshot,
            note=note,
        )

    @mcp.tool()
    async def aaps_setting_history(
        key: str, days_back: int = 365
    ) -> AapsSettingHistory:
        """Return the timeline of every detected change to a single AAPS setting.

        Args:
            key: AAPS preference key.
            days_back: window size in days. Default 365.

        Useful for "when did Adjustment Factor change?" or "how often has
        autosens_min been adjusted in the past year?" — answerable as soon
        as 2+ snapshots exist with a difference in the key's value.
        """
        loc = ah.resolve_db()
        if not loc.exists:
            return AapsSettingHistory(
                key=key,
                days_back=max(1, int(days_back)),
                event_count=0,
                events=[],
                current_value=None,
                current_snapshot_captured_at=None,
                note=_no_db_note(str(loc.path)),
            )
        history = ah.setting_history(key, days_back)
        events = [_to_event(e) for e in history.events]
        if not events:
            note = (
                f"No change events for '{key}' in the last {days_back} days. "
                "Either the value has been stable or fewer than 2 snapshots "
                "are stored."
            )
        else:
            note = f"{len(events)} change event(s) recorded."
        return AapsSettingHistory(
            key=key,
            days_back=max(1, int(days_back)),
            event_count=len(events),
            events=events,
            current_value=history.current_value,
            current_snapshot_captured_at=history.current_snapshot_captured_at,
            note=note,
        )

    @mcp.tool()
    async def aaps_settings_diff(from_iso: str, to_iso: str) -> AapsSettingsDiff:
        """All AAPS settings that changed between two timestamps.

        Args:
            from_iso: window start, ISO 8601. Inclusive.
            to_iso:   window end,   ISO 8601. Exclusive.

        Returns a per-key list of change events in the window. Useful for
        "what did I change last week?" or audit-trail reconciliation when
        the endo asks about a specific date range.
        """
        loc = ah.resolve_db()
        if not loc.exists:
            return AapsSettingsDiff(
                from_iso=from_iso,
                to_iso=to_iso,
                change_count=0,
                changes=[],
                note=_no_db_note(str(loc.path)),
            )
        diff = ah.settings_diff(from_iso, to_iso)
        events = [_to_event(e) for e in diff.changes]
        if not events:
            note = "No settings changes detected in the window."
        else:
            keys = sorted({e.key for e in events})
            note = (
                f"{len(events)} change event(s) across {len(keys)} distinct key(s): "
                + ", ".join(keys[:10])
                + (", ..." if len(keys) > 10 else "")
            )
        return AapsSettingsDiff(
            from_iso=from_iso,
            to_iso=to_iso,
            change_count=len(events),
            changes=events,
            note=note,
        )

    @mcp.tool()
    async def aaps_log_user_entries(
        start_iso: str,
        end_iso: str,
        event_types: list[str] | None = None,
    ) -> AapsLogUserEntries:
        """Return USER ENTRY records from the AAPS log archive in the window.

        Args:
            start_iso: window start, ISO 8601. Inclusive.
            end_iso:   window end,   ISO 8601. Exclusive.
            event_types: optional filter list, e.g. ["BOLUS", "CARBS",
                         "TEMP_BASAL", "PROFILE_SWITCH", "SMB"].

        Reads the log_user_entries table that the ingest pipeline populates
        once auto-log-export is wired (Track 4). Until then this tool
        returns an empty result with a note explaining the state.
        """
        loc = ah.resolve_db()
        if not loc.exists:
            return AapsLogUserEntries(
                start_iso=start_iso,
                end_iso=end_iso,
                event_types_filter=event_types,
                log_archive_available=False,
                entry_count=0,
                entries=[],
                note=_no_db_note(str(loc.path)),
            )
        rows, present = ah.log_user_entries(start_iso, end_iso, event_types)
        if not present:
            return AapsLogUserEntries(
                start_iso=start_iso,
                end_iso=end_iso,
                event_types_filter=event_types,
                log_archive_available=False,
                entry_count=0,
                entries=[],
                note=(
                    "Log archive table not yet populated. Install the "
                    "ActionLogsExport AAPS patch (Track 2) + enable the "
                    "log_ingest module of aaps-history-ingest (Track 4) "
                    "to start collecting USER ENTRY data."
                ),
            )
        entries = [
            AapsLogUserEntry(
                ts=r.ts,
                event_type=r.event_type,
                source=r.source,
                raw_text=r.raw_text,
                values=r.values,
            )
            for r in rows
        ]
        return AapsLogUserEntries(
            start_iso=start_iso,
            end_iso=end_iso,
            event_types_filter=event_types,
            log_archive_available=True,
            entry_count=len(entries),
            entries=entries,
            note=f"{len(entries)} USER ENTRY row(s) in window.",
        )
