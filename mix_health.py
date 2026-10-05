"""MiX DHL asset health rules (non-downloading, GPS, speed, RPM, spikes)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from mix_client import (
    _load_site_targets_cache,
    _normalize_mix_id,
    _parse_event_age_hours,
    _site_targets_from_health_snapshot,
    _safe_get,
    api_base_url,
    ensure_bearer_token,
    fetch_assets_for_groups_batched,
    fetch_comm_check_positions,
    fetch_latest_positions,
    group_ids,
    resolve_group_targets,
)
from mix_events import (
    fetch_events_for_type_ids,
    fetch_library_events,
    resolve_freewheel_event_type_ids,
    resolve_rpm_fault_event_type_ids,
)

log = logging.getLogger(__name__)

ISSUE_NON_DOWNLOADING = "Non downloading"
ISSUE_NO_GPS = "No GPS data"
# MiX diagnostic library events — from event history, not live telemetry.
ISSUE_NO_RPM = "Diagnostic: no engine RPM (7d)"
ISSUE_FREEWHEELING = "Freewheeling (7d)"

ALL_ISSUES = [
    ISSUE_NON_DOWNLOADING,
    ISSUE_NO_GPS,
    ISSUE_NO_RPM,
    ISSUE_FREEWHEELING,
]

# Shown first in the Asset health issues table (event columns before position fields).
_HEALTH_TABLE_COLUMN_ORDER = [
    "AssetName",
    "Registration",
    "Make",
    "GroupName",
    "RpmFault7d",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "Freewheel7d",
    "FreewheelCount7d",
    "LastFreewheelTime",
    "Issues",
    "IssueCount",
    "AgeHours",
    "EventTime",
    "GpsSource",
    "Satellites",
    "Latitude",
    "Longitude",
    "AssetId",
    "GroupId",
    "LastUpdated",
]

_HEALTH_COLUMNS = [
    "AssetId",
    "AssetName",
    "Registration",
    "Make",
    "GroupId",
    "GroupName",
    "EventTime",
    "AgeHours",
    "GpsSource",
    "Satellites",
    "Latitude",
    "Longitude",
    "RpmFault7d",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "Freewheel7d",
    "FreewheelCount7d",
    "LastFreewheelTime",
    "Issues",
    "IssueCount",
    "ScanMode",
    "LastUpdated",
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "0") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def empty_health_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_HEALTH_COLUMNS)


def _fetch_group_assets(api_url: str, token: str, group_id: int) -> list[dict[str, Any]]:
    from mix_client import _api_headers, _mix_http

    url = f"{api_url.rstrip('/')}/api/assets/group/{group_id}"
    resp = _mix_http("get", url, headers=_api_headers(token), timeout=45)
    if resp.status_code != 200:
        log.warning("MiX assets/group/%s failed: %s", group_id, resp.status_code)
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _valid_gps(lat: Any, lon: Any, source: str, sats: Any) -> bool:
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False
    if abs(la) < 0.0001 and abs(lo) < 0.0001:
        return False
    src = str(source or "").strip().lower()
    if src and src not in ("gps", "avl"):
        return False
    try:
        if int(float(sats)) <= 0:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _classify_health_issues(
    *,
    age_h: float | None,
    lat: Any,
    lon: Any,
    source: str,
    sats: Any,
    rpm_fault_7d: bool,
    freewheel_7d: bool,
    stale_h: float,
    check_comm: bool,
    check_rpm: bool,
    check_freewheel: bool,
) -> list[str]:
    issues: list[str] = []

    if check_comm:
        if age_h is None or age_h > stale_h:
            issues.append(ISSUE_NON_DOWNLOADING)
        elif not _valid_gps(lat, lon, source, sats):
            issues.append(ISSUE_NO_GPS)

    if check_rpm and rpm_fault_7d:
        issues.append(ISSUE_NO_RPM)
    if check_freewheel and freewheel_7d:
        issues.append(ISSUE_FREEWHEELING)

    return issues


def _inventory_scan_mode(*, comm: bool, rpm: bool, freewheel: bool = False) -> str:
    parts = ["inventory"]
    if comm:
        parts.append("comm")
    if rpm:
        parts.append("rpm")
    if freewheel:
        parts.append("freewheel")
    return "+".join(parts)


def _events_by_asset(events: list[dict[str, Any]], wanted_ids: set[int]) -> dict[str, dict[str, Any]]:
    """Count matching events per asset and keep the latest timestamp."""
    out: dict[str, dict[str, Any]] = {}
    if not wanted_ids:
        return out
    for ev in events:
        etid = ev.get("EventTypeId")
        if etid is None or int(etid) not in wanted_ids:
            continue
        aid = str(ev.get("AssetId", ""))
        if not aid:
            continue
        ts = ev.get("StartDateTime") or ev.get("EndDateTime") or ""
        row = out.get(aid)
        if row is None:
            out[aid] = {"count": 1, "last": ts, "category": ev.get("EventCategory", "")}
        else:
            row["count"] = int(row.get("count", 0)) + 1
            if str(ts) > str(row.get("last", "")):
                row["last"] = ts
                row["category"] = ev.get("EventCategory", row.get("category", ""))
    return out


def build_health_dataframe() -> pd.DataFrame:
    """Analyse DHL MiX assets for communication / GPS / speed / RPM issues."""
    from data import put_mix_asset_catalog

    api_url = api_base_url()
    token = ensure_bearer_token()
    org_id = _normalize_mix_id(_env("MIX_PARENT_ORG_ID") or _env("MIX_ORGANISATION_ID"))
    site_prefix = _env("MIX_SITE_PREFIX", "DHL")
    try:
        targets = resolve_group_targets()
    except RuntimeError as exc:
        log.warning("MiX health: site resolution failed (%s)", exc)
        targets = (
            _load_site_targets_cache(org_id, site_prefix)
            if org_id
            else None
        ) or _site_targets_from_health_snapshot(site_prefix) or []
    if not targets and org_id:
        log.warning(
            "MiX health: site list unavailable — using org-level asset inventory for org %s",
            org_id,
        )
    log.info("MiX health: resolved %s site group(s) for asset pull", len(targets))
    group_name_by_id = {int(t["GroupId"]): t.get("Name", "") for t in targets}
    gids = [int(t["GroupId"]) for t in targets]

    try:
        auto_light_threshold = int(_env("MIX_HEALTH_LIGHT_AUTO_THRESHOLD", "25") or "25")
    except ValueError:
        auto_light_threshold = 25
    light = _env_truthy("MIX_HEALTH_LIGHT", "0")
    if len(targets) > auto_light_threshold and not _env_truthy("MIX_HEALTH_FORCE_DEEP", "0"):
        light = True
    rpm_diag = _env_truthy("MIX_HEALTH_RPM_DIAG", "1" if light else "0")
    freewheel_diag = _env_truthy("MIX_HEALTH_FREEWHEEL_DIAG", "1" if light else "0")
    comm_check = _env_truthy("MIX_HEALTH_COMM_CHECK", "1" if light else "0")
    if light:
        bits = ["assets"]
        if comm_check:
            bits.append("comm+gps")
        if rpm_diag:
            bits.append("RPM diagnostics")
        if freewheel_diag:
            bits.append("freewheeling")
        log.info(
            "MiX health: light mode for %s site(s) — %s (skip tacho/speed)",
            len(targets),
            " + ".join(bits),
        )

    pos_by_asset: dict[str, dict[str, Any]] = {}
    if not light:
        positions = fetch_latest_positions()
        for row in positions:
            aid = _safe_get(row, "AssetId", "assetId")
            if aid:
                pos_by_asset[aid] = row
    elif comm_check and targets:
        for row in fetch_comm_check_positions(targets, api_url=api_url, token=token):
            aid = _safe_get(row, "AssetId", "assetId")
            if aid:
                pos_by_asset[aid] = row

    asset_rows = fetch_assets_for_groups_batched(
        api_url,
        token,
        gids,
        org_id=_normalize_mix_id(_env("MIX_PARENT_ORG_ID") or _env("MIX_ORGANISATION_ID")),
    )
    if not asset_rows and gids:
        log.warning("MiX health: org/bulk asset fetch empty; retrying per site group")
        asset_rows = []
        seen_asset_ids: set[str] = set()
        for gid in gids:
            for asset in _fetch_group_assets(api_url, token, gid):
                aid = str(asset.get("AssetId", asset.get("assetId", "")))
                if aid:
                    if aid in seen_asset_ids:
                        continue
                    seen_asset_ids.add(aid)
                asset_rows.append(asset)

    if not asset_rows:
        log.warning("MiX health: no assets returned for groups %s", gids)
        return empty_health_dataframe()

    stale_h = _env_float("MIX_NON_DOWNLOADING_HOURS", 6.0)

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out_rows: list[dict[str, Any]] = []

    rpm_fault_by_asset: dict[str, dict[str, Any]] = {}
    freewheel_by_asset: dict[str, dict[str, Any]] = {}
    run_rpm_diag = rpm_diag if light else True
    run_freewheel_diag = freewheel_diag if light else True
    if run_rpm_diag or run_freewheel_diag:
        library = fetch_library_events()
        rpm_ids = resolve_rpm_fault_event_type_ids(library) if run_rpm_diag else []
        freewheel_ids = resolve_freewheel_event_type_ids(library) if run_freewheel_diag else []
        combined_ids = sorted(set(rpm_ids + freewheel_ids))
        window_days = _env_int("MIX_RPM_FAULT_DAYS", 7)
        if run_freewheel_diag:
            window_days = max(window_days, _env_int("MIX_FREEWHEEL_DAYS", 7))
        events = fetch_events_for_type_ids(
            combined_ids,
            days=window_days,
            group_ids=gids or None,
            org_id=org_id,
            log_label="health diagnostics",
        )
        rpm_fault_by_asset = _events_by_asset(events, set(rpm_ids))
        freewheel_by_asset = _events_by_asset(events, set(freewheel_ids))
        if run_rpm_diag:
            log.info(
                "MiX health: %s asset(s) with no-engine-RPM diagnostic event(s) in lookback window",
                len(rpm_fault_by_asset),
            )
        if run_freewheel_diag:
            log.info(
                "MiX health: %s asset(s) with freewheeling event(s) in lookback window",
                len(freewheel_by_asset),
            )

    catalog_rows: list[dict[str, Any]] = []

    for asset in asset_rows:
        aid = str(asset.get("AssetId", asset.get("assetId", "")))
        if not aid:
            continue
        gid = asset.get("SiteId") or asset.get("GroupId") or asset.get("siteId") or gids[0]
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            gid_int = gids[0]
        pos = pos_by_asset.get(aid, {})

        event_time = _safe_get(pos, "Timestamp", "EventTime")
        age_h = _parse_event_age_hours(event_time) if event_time else None

        lat = pos.get("Latitude", "")
        lon = pos.get("Longitude", "")
        source = _safe_get(pos, "Source", "source")
        sats = pos.get("NumberOfSatellites", "")

        fault = rpm_fault_by_asset.get(aid, {})
        rpm_fault_7d = bool(fault)
        rpm_fault_count = int(fault.get("count", 0)) if fault else 0
        last_rpm_fault = fault.get("last", "") if fault else ""
        fw = freewheel_by_asset.get(aid, {})
        freewheel_7d = bool(fw)
        freewheel_count = int(fw.get("count", 0)) if fw else 0
        last_freewheel = fw.get("last", "") if fw else ""

        check_comm = comm_check if light else bool(pos_by_asset)
        issues = _classify_health_issues(
            age_h=age_h,
            lat=lat,
            lon=lon,
            source=source,
            sats=sats,
            rpm_fault_7d=rpm_fault_7d,
            freewheel_7d=freewheel_7d,
            stale_h=stale_h,
            check_comm=check_comm,
            check_rpm=run_rpm_diag,
            check_freewheel=run_freewheel_diag,
        )
        if light:
            scan_mode = _inventory_scan_mode(
                comm=comm_check, rpm=run_rpm_diag, freewheel=run_freewheel_diag
            )
        else:
            scan_mode = "full"

        asset_name = _safe_get(asset, "Description", "description")
        registration = _safe_get(asset, "RegistrationNumber", "registrationNumber")
        group_name = group_name_by_id.get(gid_int, str(gid_int))
        catalog_rows.append(
            {
                "AssetId": aid,
                "AssetName": asset_name,
                "Registration": registration,
                "GroupName": group_name,
                "Make": _safe_get(asset, "Make", "make"),
            }
        )

        out_rows.append(
            {
                "AssetId": aid,
                "AssetName": asset_name,
                "Registration": registration,
                "Make": _safe_get(asset, "Make", "make"),
                "GroupId": str(gid_int),
                "GroupName": group_name,
                "EventTime": event_time,
                "AgeHours": round(age_h, 2) if age_h is not None else None,
                "GpsSource": source,
                "Satellites": sats,
                "Latitude": lat,
                "Longitude": lon,
                "RpmFault7d": rpm_fault_7d,
                "RpmFaultCount7d": rpm_fault_count,
                "LastRpmFaultTime": last_rpm_fault,
                "Freewheel7d": freewheel_7d,
                "FreewheelCount7d": freewheel_count,
                "LastFreewheelTime": last_freewheel,
                "Issues": "; ".join(issues),
                "IssueCount": len(issues),
                "ScanMode": scan_mode,
                "LastUpdated": run_ts,
            }
        )

    if not out_rows:
        return empty_health_dataframe()

    df = pd.DataFrame(out_rows)
    put_mix_asset_catalog(pd.DataFrame(catalog_rows))
    flagged = int((df["IssueCount"] > 0).sum())
    log.info("MiX health: %s assets analysed, %s with issues", len(df), flagged)

    # Push names into the positions cache once health metadata is ready.
    try:
        from data import _apply_mix_asset_catalog, _backfill_mix_positions_metadata_from_health, cache_peek, cache_put

        pos = cache_peek("mix_positions")
        if isinstance(pos, pd.DataFrame) and not pos.empty:
            cache_put(
                "mix_positions",
                _backfill_mix_positions_metadata_from_health(_apply_mix_asset_catalog(pos.copy())),
            )
    except Exception as e:  # noqa: BLE001
        log.debug("MiX health: positions name sync skipped: %s", e)

    return df[_HEALTH_COLUMNS]


def order_health_table_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Prefer 7-day RPM fault report columns before position fields."""
    if df is None or df.empty:
        return df
    cols = [c for c in _HEALTH_TABLE_COLUMN_ORDER if c in df.columns]
    rest = [c for c in df.columns if c not in cols]
    return df[cols + rest]


_RPM_FAULT_LIST_COLUMNS = [
    "AssetName",
    "Registration",
    "Make",
    "GroupName",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "AgeHours",
    "Issues",
    "AssetId",
]


def rpm_fault_assets_dataframe(df: pd.DataFrame | None) -> pd.DataFrame:
    """Assets with MiX 'Diagnostic: no engine RPM' event(s) in the 7-day window."""
    if df is None or df.empty or "RpmFault7d" not in df.columns:
        return pd.DataFrame(columns=_RPM_FAULT_LIST_COLUMNS)
    out = df[df["RpmFault7d"].fillna(False).astype(bool)].copy()
    if out.empty:
        return pd.DataFrame(columns=_RPM_FAULT_LIST_COLUMNS)
    out = out.sort_values(
        ["RpmFaultCount7d", "LastRpmFaultTime"],
        ascending=[False, False],
        na_position="last",
    )
    cols = [c for c in _RPM_FAULT_LIST_COLUMNS if c in out.columns]
    return out[cols]


_FREEWHEEL_LIST_COLUMNS = [
    "AssetName",
    "Registration",
    "Make",
    "GroupName",
    "FreewheelCount7d",
    "LastFreewheelTime",
    "AgeHours",
    "Issues",
    "AssetId",
]


def freewheel_assets_dataframe(df: pd.DataFrame | None) -> pd.DataFrame:
    """Assets with MiX Freewheeling event(s) in the 7-day window."""
    if df is None or df.empty or "Freewheel7d" not in df.columns:
        return pd.DataFrame(columns=_FREEWHEEL_LIST_COLUMNS)
    out = df[df["Freewheel7d"].fillna(False).astype(bool)].copy()
    if out.empty:
        return pd.DataFrame(columns=_FREEWHEEL_LIST_COLUMNS)
    out = out.sort_values(
        ["FreewheelCount7d", "LastFreewheelTime"],
        ascending=[False, False],
        na_position="last",
    )
    cols = [c for c in _FREEWHEEL_LIST_COLUMNS if c in out.columns]
    return out[cols]
