"""MiX event queries — library event types and group event history."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from mix_client import (
    _api_headers,
    _throttle_mix_api,
    api_base_url,
    ensure_bearer_token,
    group_ids,
    resolve_organisation_id,
)

log = logging.getLogger(__name__)

_RPM_FAULT_COLUMNS = [
    "AssetId",
    "AssetName",
    "Registration",
    "GroupId",
    "GroupName",
    "EventTypeId",
    "EventCategory",
    "EventDescription",
    "LastFaultTime",
    "FaultCount7d",
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def fetch_library_events(organisation_id: int | None = None) -> list[dict[str, Any]]:
    """All library event definitions for the organisation (includes EventTypeId + Description)."""
    org_id = organisation_id if organisation_id is not None else resolve_organisation_id()
    api = api_base_url()
    token = ensure_bearer_token()
    url = f"{api.rstrip('/')}/api/libraryevents/organisation/{org_id}"
    _throttle_mix_api()
    resp = requests.get(url, headers=_api_headers(token), timeout=45)
    if resp.status_code == 204:
        return []
    if resp.status_code != 200:
        log.warning("MiX library events failed (%s): %s", resp.status_code, resp.text[:200])
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def resolve_rpm_fault_event_type_ids(
    library: list[dict[str, Any]] | None = None,
) -> list[int]:
    """Find EventTypeIds for 'Diagnostic fault no engine RPM' (and similar) in the library."""
    explicit = _env("MIX_RPM_FAULT_EVENT_TYPE_IDS")
    if explicit:
        out: list[int] = []
        for part in explicit.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        if out:
            return out

    patterns_raw = _env(
        "MIX_RPM_FAULT_EVENT_MATCH",
        "fault no engine rpm,diagnostic fault no engine rpm,no engine rpm",
    )
    patterns = [p.strip().lower() for p in patterns_raw.split(",") if p.strip()]
    if not patterns:
        patterns = ["diagnostic fault no engine rpm"]

    rows = library if library is not None else fetch_library_events()
    ids: list[int] = []
    for row in rows:
        blob = " ".join(
            str(row.get(k, "") or "")
            for k in ("Description", "EventType", "ValueName", "DisplayUnits")
        ).lower()
        if any(p in blob for p in patterns):
            eid = row.get("EventTypeId")
            if eid is not None:
                ids.append(int(eid))
                continue
        # Exact description match (case-insensitive) when patterns are specific phrases.
        desc = str(row.get("Description") or row.get("EventType") or "").strip().lower()
        if desc and desc in patterns:
            eid = row.get("EventTypeId")
            if eid is not None:
                ids.append(int(eid))
    ids = sorted(set(ids))
    if ids:
        log.info("MiX: matched %s RPM-fault event type id(s): %s", len(ids), ids[:10])
    else:
        log.warning(
            "MiX: no RPM-fault event types matched patterns %s — set MIX_RPM_FAULT_EVENT_TYPE_IDS",
            patterns,
        )
    return ids


def fetch_group_asset_events(
    group_id: int,
    *,
    from_dt: datetime,
    to_dt: datetime,
    event_type_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Events for all assets in a group (MiX max 7 days per request)."""
    api = api_base_url()
    token = ensure_bearer_token()
    fr = from_dt.strftime("%Y%m%d%H%M%S")
    to = to_dt.strftime("%Y%m%d%H%M%S")
    url = f"{api.rstrip('/')}/api/events/groups/entitytype/Asset/from/{fr}/to/{to}"
    body: dict[str, Any] = {
        "EntityIds": [int(group_id)],
        "EventTypeIds": event_type_ids or [],
        "MenuId": "",
    }
    _throttle_mix_api()
    resp = requests.post(url, headers=_api_headers(token), json=body, timeout=120)
    if resp.status_code == 429:
        wait = float(_env("MIX_EVENTS_429_SLEEP_SEC", "8") or "8")
        log.warning("MiX group events rate-limited (429) — retrying in %ss", wait)
        time.sleep(max(2.0, wait))
        _throttle_mix_api()
        resp = requests.post(url, headers=_api_headers(token), json=body, timeout=120)
    if resp.status_code == 204:
        return []
    if resp.status_code != 200:
        log.warning("MiX group events %s..%s failed (%s)", fr, to, resp.status_code)
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_rpm_fault_events(days: int | None = None) -> list[dict[str, Any]]:
    """Fetch 'no engine RPM' diagnostic events for the last N days (7-day API chunks)."""
    window_days = days if days is not None else _env_int("MIX_RPM_FAULT_DAYS", 7)
    window_days = max(1, min(window_days, 28))
    event_type_ids = resolve_rpm_fault_event_type_ids()
    if not event_type_ids:
        return []

    to_dt = datetime.now(timezone.utc)
    fr_dt = to_dt - timedelta(days=window_days)
    merged: list[dict[str, Any]] = []

    for gid in group_ids():
        chunk_end = to_dt
        while chunk_end > fr_dt:
            chunk_start = max(fr_dt, chunk_end - timedelta(days=7))
            rows = fetch_group_asset_events(
                gid,
                from_dt=chunk_start,
                to_dt=chunk_end,
                event_type_ids=event_type_ids,
            )
            merged.extend(rows)
            chunk_end = chunk_start
    log.info("MiX: %s RPM-fault event row(s) in last %s day(s)", len(merged), window_days)
    return merged


def build_no_rpm_fault_assets_dataframe(
    asset_lookup: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """Assets with 'Diagnostic fault no engine RPM' (or matched library events) in the lookback window."""
    events = fetch_rpm_fault_events()
    if not events:
        return pd.DataFrame(columns=_RPM_FAULT_COLUMNS)

    lib = {int(r["EventTypeId"]): r for r in fetch_library_events() if r.get("EventTypeId") is not None}
    by_asset: dict[str, dict[str, Any]] = {}

    for ev in events:
        aid = str(ev.get("AssetId", ""))
        if not aid:
            continue
        ts = ev.get("StartDateTime") or ev.get("EndDateTime") or ""
        etid = ev.get("EventTypeId")
        lib_row = lib.get(int(etid)) if etid is not None else {}
        desc = str(lib_row.get("Description") or lib_row.get("EventType") or ev.get("EventCategory") or "")
        row = by_asset.get(aid)
        if row is None:
            by_asset[aid] = {
                "AssetId": aid,
                "EventTypeId": etid,
                "EventCategory": ev.get("EventCategory", ""),
                "EventDescription": desc,
                "LastFaultTime": ts,
                "FaultCount7d": 1,
            }
        else:
            row["FaultCount7d"] = int(row.get("FaultCount7d", 0)) + 1
            if str(ts) > str(row.get("LastFaultTime", "")):
                row["LastFaultTime"] = ts
                row["EventCategory"] = ev.get("EventCategory", row.get("EventCategory", ""))
                row["EventDescription"] = desc or row.get("EventDescription", "")

    if not by_asset:
        return pd.DataFrame(columns=_RPM_FAULT_COLUMNS)

    asset_lookup = asset_lookup or {}
    out_rows: list[dict[str, Any]] = []
    for aid, row in by_asset.items():
        info = asset_lookup.get(aid, {})
        out_rows.append(
            {
                **row,
                "AssetName": str(info.get("Description") or info.get("description") or ""),
                "Registration": str(
                    info.get("RegistrationNumber") or info.get("registrationNumber") or ""
                ),
                "GroupId": str(info.get("SiteId") or info.get("GroupId") or group_ids()[0]),
                "GroupName": "",
            }
        )

    return pd.DataFrame(out_rows)[_RPM_FAULT_COLUMNS]
