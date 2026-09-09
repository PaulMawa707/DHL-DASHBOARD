"""Build template context for each dashboard page."""

from __future__ import annotations

from html import escape
import os
import re
from typing import Any
from urllib.parse import quote

import pandas as pd

import components as C
from data import (
    RT_VIDEO_LOST_CHANNEL_MAX,
    TARGET_ALARMS,
    _DISK_LOSS_ALARM_LABEL,
    alarms_kpi_label,
    canonical_alarm_name,
    find_mix_asset_for_vss_device,
    get_alarms_cached,
    get_dhl_devices_cached,
    get_mix_health_cached,
    get_realtime_cached,
    last_mix_error,
    last_saved_refresh_display,
    load_alarms_last_24h,
    load_dhl_devices,
    load_mix_health,
    load_realtime_status,
    mix_integration_enabled,
    normalize_vehicle_registration,
    parse_channels,
)
from vss_client import active_base_url, last_vss_error, try_token_without_login
try:
    from web.vss_proxy import _stream_wss_hostname, vss_embed_proxy_enabled
except ImportError:  # Production image may not include the camera proxy yet.
    def vss_embed_proxy_enabled() -> bool:
        return False

    def _stream_wss_hostname() -> str:
        return ""
from mix_health import ALL_ISSUES
from web.alerts import annotate_alarms, annotate_realtime, build_device_severity, severity_counts
from web.charts import figure_html

DHL_RED = C.DHL_RED
DHL_YELLOW = C.DHL_YELLOW


def _sync_load_on_page() -> bool:
    return os.environ.get("DHL_SYNC_LOAD_ON_PAGE", "0").strip().lower() in ("1", "true", "yes", "on")


def _devices_df() -> pd.DataFrame | None:
    df = get_dhl_devices_cached()
    if df is not None or not _sync_load_on_page():
        return df
    try:
        return load_dhl_devices()
    except Exception:
        return get_dhl_devices_cached()


def _realtime_df() -> pd.DataFrame | None:
    df = get_realtime_cached()
    if df is not None or not _sync_load_on_page():
        return df
    try:
        return load_realtime_status()
    except Exception:
        return get_realtime_cached()


def _alarms_df() -> pd.DataFrame | None:
    df = get_alarms_cached()
    if df is not None or not _sync_load_on_page():
        return df
    try:
        return load_alarms_last_24h()
    except Exception:
        return get_alarms_cached()


def _mix_df() -> pd.DataFrame | None:
    df = get_mix_health_cached()
    if df is not None or not _sync_load_on_page():
        return df
    try:
        return load_mix_health()
    except Exception:
        return get_mix_health_cached()


def kpi_dict(
    title: str,
    value: str | int | float,
    *,
    accent: str = DHL_RED,
    border_accent: str | None = None,
    sub: str | None = None,
) -> dict:
    border = border_accent or accent
    return {"title": title, "value": str(value), "accent": accent, "border_accent": border, "sub": sub or ""}


def df_to_table_html(
    df: pd.DataFrame | None,
    columns: list[str] | None = None,
    *,
    max_rows: int = 500,
    page_size: int = 10,
) -> str:
    if df is None:
        return '<p class="muted-msg">Loading from VSS — data will appear shortly.</p>'
    if df.empty:
        return '<p class="muted-msg">No rows match the current filters.</p>'
    out = df.copy()
    if columns:
        columns = [c for c in columns if c in out.columns]
        out = out[columns]
    if "AlarmTime" in out.columns:
        out = out.assign(AlarmTime=lambda d: pd.to_datetime(d["AlarmTime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S"))
    out = out.fillna("")
    display = out.head(max_rows).reset_index(drop=True)
    display.insert(0, "#", range(1, len(display) + 1))
    table = display.to_html(classes="data-table report-data-table", index=False, border=0, escape=True)
    table = table.replace("<thead>", '<thead class="sortable-head">', 1)
    table = re.sub(
        r"<th>",
        '<th class="sortable-th" role="columnheader" tabindex="0">',
        table,
    )
    table = table.replace(
        '<th class="sortable-th" role="columnheader" tabindex="0">',
        '<th class="sortable-th sort-num" role="columnheader" tabindex="0" data-sort-type="num">',
        1,
    )
    table = table.replace("<td>Critical</td>", '<td><span class="alert-badge alert-badge-critical">Critical</span></td>')
    table = table.replace("<td>High</td>", '<td><span class="alert-badge alert-badge-high">High</span></td>')
    shown = len(display)
    total = len(out)
    clipped = total > shown
    clipped_msg = f"Showing first {shown:,} of {total:,} rows." if clipped else f"{total:,} rows available."
    return f"""
<div class="report-table" data-page-size="{int(page_size)}">
  <div class="table-toolbar">
    <div class="table-search-wrap">
      <span class="table-search-icon" aria-hidden="true">⌕</span>
      <input type="search" class="table-search" placeholder="Filter rows..." aria-label="Filter table rows">
    </div>
    <div class="table-toolbar-actions">
      <button type="button" class="table-export-xlsx" aria-label="Download table as Excel">
        <svg class="table-export-icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
          <path fill="currentColor" d="M10 2.5a.75.75 0 0 1 .75.75v7.19l2.22-2.22a.75.75 0 1 1 1.06 1.06l-3.5 3.5a.75.75 0 0 1-1.06 0l-3.5-3.5a.75.75 0 0 1 1.06-1.06l2.22 2.22V3.25A.75.75 0 0 1 10 2.5Zm-6 11.25a.75.75 0 0 1 .75.75v1.25h10.5V14.5a.75.75 0 0 1 1.5 0v2a.75.75 0 0 1-.75.75H4.75a.75.75 0 0 1-.75-.75v-2a.75.75 0 0 1 .75-.75Z"/>
        </svg>
        Download Excel
      </button>
      <label class="table-page-size-label">
        Rows
        <select class="table-page-size" aria-label="Rows per page">
          <option value="10" {"selected" if page_size == 10 else ""}>10</option>
          <option value="25" {"selected" if page_size == 25 else ""}>25</option>
          <option value="50" {"selected" if page_size == 50 else ""}>50</option>
        </select>
      </label>
    </div>
  </div>
  <div class="table-wrap">{table}</div>
  <div class="table-pagination">
    <span class="table-count">{escape(clipped_msg)}</span>
    <div class="table-page-controls">
      <button type="button" class="table-prev">Prev</button>
      <span class="table-page-label">Page 1 / 1</span>
      <button type="button" class="table-next">Next</button>
    </div>
  </div>
</div>
""".strip()


def _parse_multi(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _video_lost_active_mask(df: pd.DataFrame, age_hours: float) -> pd.Series:
    """True only when a device reports video lost while ignition is on and online.

    Offline units often keep a stale "video lost" flag from the last report,
    which should not count as a live camera fault.
    """
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    lost = df.get("NotRecordingFlag", pd.Series("", index=df.index)).astype(str).eq("Not Working")
    ign = df.get("Ignition", pd.Series("", index=df.index)).astype(str).str.strip().str.lower().eq("on")
    age = pd.to_numeric(df.get("AgeHours"), errors="coerce") if "AgeHours" in df.columns else pd.Series(float("nan"), index=df.index)
    online = age.notna() & (age <= float(age_hours))
    return lost & ign & online


def _with_active_video_lost(df: pd.DataFrame, age_hours: float) -> pd.DataFrame:
    """Copy of ``df`` with video-lost flags cleared unless ignition is on and online."""
    if df is None or df.empty:
        return df
    out = df.copy()
    active = _video_lost_active_mask(out, age_hours)
    if "NotRecordingFlag" in out.columns:
        out.loc[~active, "NotRecordingFlag"] = "Working"
    if "videoloststateFormatter" in out.columns:
        out.loc[~active, "videoloststateFormatter"] = ""
    if "VideoLostChannels" in out.columns:
        out.loc[~active, "VideoLostChannels"] = ""
    for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1):
        col = f"VideoLost_Ch{n}"
        if col in out.columns:
            out.loc[~active, col] = "Working"
    return out


def _filter_realtime(
    df: pd.DataFrame | None,
    *,
    fleets: list[str],
    statuses: list[str],
    ignitions: list[str],
    ch_filter: str,
    age_hours: float = 6.0,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if fleets:
        out = out[out["Fleet"].astype(str).isin(fleets)]
    if statuses:
        out = out[out["StatusType"].astype(str).isin(statuses)]
    if ignitions and "Ignition" in out.columns:
        out = out[out["Ignition"].astype(str).isin(ignitions)]
    cf = (ch_filter or "all").strip().lower()
    if cf not in ("", "all", "none"):
        try:
            n = int(cf)
        except ValueError:
            n = 0
        if 1 <= n <= RT_VIDEO_LOST_CHANNEL_MAX:
            col = f"VideoLost_Ch{n}"
            if col in out.columns:
                flagged = _with_active_video_lost(out, age_hours)
                out = flagged[flagged[col].astype(str) == "Not Working"]
    return out


def _filter_alarms(df: pd.DataFrame | None, *, fleets: list[str], alarm_types: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if fleets:
        out = out[out["Fleet"].astype(str).isin(fleets)]
    if alarm_types:
        out = out[out["AlarmName"].astype(str).isin(alarm_types)]
    return out


def _normalize_alarm_frame(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Restore dtypes lost when alarm snapshots are serialized to JSON."""
    if df is None:
        return None
    out = df.copy()
    if "AlarmTime" in out.columns:
        out["AlarmTime"] = pd.to_datetime(out["AlarmTime"], errors="coerce")
    for col in ("Lat", "Lon", "Speed"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    for col in ("DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName", "PlateNo"):
        if col in out.columns:
            out[col] = out[col].fillna("").astype(str)
    if "AlarmName" in out.columns:
        out["AlarmName"] = out["AlarmName"].map(canonical_alarm_name)
    return out


def _device_severity_map(*, age_hours: float = 6.0) -> dict:
    return build_device_severity(_alarms_df(), _realtime_df(), age_hours=age_hours)


def _severity_kpis(severity_by_device: dict) -> list[dict]:
    high, critical = severity_counts(severity_by_device)
    return [
        kpi_dict("High alerts", f"{high:,}", accent="#F59E0B", border_accent="#F59E0B", sub="First watchlist trigger"),
        kpi_dict(
            "Critical alerts",
            f"{critical:,}",
            accent=DHL_RED,
            border_accent=DHL_RED,
            sub="Same vehicle, more than one of: power / video / offline / SD / storage",
        ),
    ]


def _fleet_options(*, df: pd.DataFrame | None = None) -> list[str]:
    if df is not None and not df.empty and "Fleet" in df.columns:
        return sorted({str(x) for x in df["Fleet"].dropna() if str(x).strip()})
    devices = _devices_df()
    if devices is not None and not devices.empty and "Fleet" in devices.columns:
        return sorted({str(x) for x in devices["Fleet"].dropna() if str(x).strip()})
    return []


def _vss_unavailable_fig(context: str) -> str:
    err = last_vss_error()
    if err:
        msg = f"{context} — VSS session expired. Click Refresh data or restart the app."
    else:
        msg = context
    return figure_html(C.loading_fig(msg))


def nav_items(*, active: str, mix_enabled: bool) -> list[dict]:
    items = [
        {"id": "overview", "label": "Overview", "href": "/dashboard", "icon": "grid"},
        {"id": "realtime", "label": "Real-Time Status", "href": "/dashboard/realtime", "icon": "radio"},
        {"id": "camera", "label": "Camera", "href": "/dashboard/camera", "icon": "camera"},
        {"id": "alarms", "label": "Alarms (24h)", "href": "/dashboard/alarms", "icon": "bell"},
        {"id": "device", "label": "Device Drilldown", "href": "/dashboard/device", "icon": "search"},
        {"id": "logs", "label": "Logs", "href": "/dashboard/logs", "icon": "document"},
    ]
    if mix_enabled:
        items.insert(1, {"id": "mix", "label": "MiX Health", "href": "/dashboard/mix", "icon": "satellite"})
    for item in items:
        item["active"] = item["id"] == active
    return items


def logs_context() -> dict[str, Any]:
    import operation_log

    sessions = operation_log.fetch_sessions(limit=15)
    return {
        "title": "Pipeline Logs",
        "subtitle": "Sessions grouped by login and Refresh data runs.",
        "sessions": sessions,
        "latest_id": operation_log.latest_event_id(),
        "active_session_id": "",
    }


def db_data_banner() -> str:
    """Banner text showing last saved refresh time from Neon."""
    info = last_saved_refresh_display()
    if info and info.get("at_display"):
        return f"Showing data last refreshed {info['at_display']} (EAT) — click Refresh data to update."
    return "Showing last saved data from database — click Refresh data to update."


def overview_context(*, age_hours: float = 6.0) -> dict[str, Any]:
    devices = _devices_df()
    rt = _realtime_df()
    alarms = _alarms_df()

    banners: list[str] = []
    vss_err = last_vss_error()
    has_any_data = devices is not None or rt is not None or alarms is not None
    if has_any_data:
        banners.append(db_data_banner())
    elif vss_err:
        banners.append(f"VSS connection issue: {vss_err}")
    else:
        banners.append("No saved data in database yet — click Refresh data to load from VSS and MiX.")

    total_devices = len(devices) if devices is not None else 0
    if rt is None or rt.empty:
        online = offline = unknown = 0
    else:
        age = pd.to_numeric(rt.get("AgeHours"), errors="coerce")
        online = int((age.notna() & (age <= age_hours)).sum())
        offline = int((age.notna() & (age > age_hours)).sum())
        unknown = int(age.isna().sum())

    devices_with_alarm = 0 if alarms is None or alarms.empty else int(alarms["DeviceID"].nunique())
    total_alarms = 0 if alarms is None or alarms.empty else int(len(alarms))

    kpis = [
        kpi_dict("Total devices (VSS)", f"{total_devices:,}", border_accent="#3B82F6"),
        kpi_dict("Online", f"{online:,}", accent="#2E8B57", border_accent="#2E8B57", sub=f"<= {age_hours:g}h since last status"),
        kpi_dict("Offline", f"{offline:,}", accent=DHL_RED, border_accent=DHL_RED, sub=f"> {age_hours:g}h or no signal"),
        kpi_dict("Status unknown", f"{unknown:,}", accent="#999", border_accent="#9CA3AF"),
        kpi_dict(alarms_kpi_label(), f"{total_alarms:,}", accent=DHL_YELLOW, border_accent=DHL_YELLOW),
        kpi_dict("Devices alarming", f"{devices_with_alarm:,}", accent=DHL_RED, border_accent="#DC2626"),
    ]
    kpis.extend(_severity_kpis(_device_severity_map(age_hours=age_hours)))

    if mix_integration_enabled():
        mix_df = _mix_df()
        if mix_df is None:
            kpis.append(kpi_dict("MiX assets", "…", accent="#F59E0B", sub="loading"))
        elif mix_df.empty:
            kpis.append(kpi_dict("MiX assets", "0", accent="#F59E0B"))
        else:
            flagged = int((mix_df["IssueCount"] > 0).sum()) if "IssueCount" in mix_df.columns else 0
            kpis.append(
                kpi_dict("MiX assets", f"{len(mix_df):,}", accent="#F59E0B", sub=f"{flagged:,} with issues")
            )

    charts: list[str] = []
    if rt is not None and not rt.empty:
        charts.append(figure_html(C.online_offline_pie(rt, age_hours)))
        charts.append(figure_html(C.status_type_donut(rt)))
    elif devices is not None:
        if vss_err and rt is None:
            charts.append(figure_html(C.loading_fig("Live status unavailable — use Refresh data")))
            charts.append(figure_html(C.loading_fig("Live status unavailable — use Refresh data")))
        else:
            charts.append(figure_html(C.loading_fig("Fetching live status…")))
            charts.append(figure_html(C.loading_fig("Fetching live status…")))
    else:
        charts.append(figure_html(C.EMPTY_FIG))
        charts.append(figure_html(C.EMPTY_FIG))

    if alarms is not None and not alarms.empty:
        charts.append(figure_html(C.top_devices_by_alarms(alarms, top_n=10)))
        charts.append(figure_html(C.alarm_type_pie(alarms)))
    elif devices is not None or rt is not None:
        if vss_err and alarms is None:
            charts.append(figure_html(C.loading_fig("Alarms unavailable — use Refresh data")))
            charts.append(figure_html(C.loading_fig("Alarms unavailable — use Refresh data")))
        else:
            charts.append(figure_html(C.loading_fig("Loading alarm history…")))
            charts.append(figure_html(C.loading_fig("Loading alarm history…")))
    else:
        charts.append(figure_html(C.EMPTY_FIG))
        charts.append(figure_html(C.EMPTY_FIG))

    return {
        "title": "Fleet Overview",
        "subtitle": "Live snapshot of fleet health — devices, status, and alarms.",
        "banners": banners,
        "kpis": kpis,
        "charts": charts,
        "age_hours": age_hours,
    }


def realtime_context(
    *,
    age_hours: float = 6.0,
    fleets: list[str] | None = None,
    statuses: list[str] | None = None,
    ignitions: list[str] | None = None,
    ch_filter: str = "all",
    chart: str = "online_pie",
) -> dict[str, Any]:
    df = _realtime_df()
    fleets = _parse_multi(fleets)
    statuses = _parse_multi(statuses)
    ignitions = _parse_multi(ignitions)

    fleet_opts: list[str] = []
    status_opts: list[str] = []
    ignition_opts: list[str] = []
    if df is not None and not df.empty:
        fleet_opts = _fleet_options(df=df)
        status_opts = sorted({str(x) for x in df["StatusType"].dropna() if str(x).strip()})
        if "Ignition" in df.columns:
            ignition_opts = sorted({str(x) for x in df["Ignition"].dropna() if str(x).strip()})
    else:
        fleet_opts = _fleet_options()

    if df is None:
        devices = _devices_df()
        dev_count = len(devices) if devices is not None else 0
        kpis = [
            kpi_dict("Devices (cached)", f"{dev_count:,}", border_accent="#3B82F6"),
        ] if dev_count else []
        return {
            "title": "Real-Time Device Status",
            "subtitle": "Most recent reported state for every tracked device.",
            "loading": True,
            "kpis": kpis,
            "chart_html": _vss_unavailable_fig("Fetching live device status"),
            "table_html": df_to_table_html(None),
            "fleet_opts": fleet_opts,
            "status_opts": status_opts,
            "ignition_opts": ignition_opts,
            "fleets": fleets,
            "statuses": statuses,
            "ignitions": ignitions,
            "ch_filter": ch_filter,
            "chart": chart,
            "age_hours": age_hours,
        }

    f = _filter_realtime(
        df,
        fleets=fleets,
        statuses=statuses,
        ignitions=ignitions,
        ch_filter=ch_filter,
        age_hours=age_hours,
    )
    age = pd.to_numeric(f.get("AgeHours"), errors="coerce") if not f.empty else pd.Series(dtype=float)
    total = len(f)
    online = int((age.notna() & (age <= age_hours)).sum()) if total else 0
    offline = int((age.notna() & (age > age_hours)).sum()) if total else 0
    unknown = int(age.isna().sum()) if total else 0
    video_lost = int(_video_lost_active_mask(f, age_hours).sum()) if total else 0
    chart_df = _with_active_video_lost(f, age_hours)

    kpis = [
        kpi_dict("Devices shown", f"{total:,}", border_accent="#3B82F6"),
        kpi_dict("Online", f"{online:,}", accent="#2E8B57", border_accent="#2E8B57"),
        kpi_dict("Offline", f"{offline:,}", accent=DHL_RED, border_accent=DHL_RED),
        kpi_dict(
            "Video lost (ch)",
            f"{video_lost:,}",
            accent=DHL_YELLOW,
            border_accent=DHL_YELLOW,
            sub="Ignition on, online only",
        ),
        kpi_dict("Status unknown", f"{unknown:,}", accent="#999", border_accent="#9CA3AF"),
    ]
    kpis.extend(_severity_kpis(_device_severity_map(age_hours=age_hours)))

    chart_map = {
        "online_pie": lambda: C.online_offline_pie(f, age_hours),
        "status_donut": lambda: C.status_type_donut(f),
        "modules": lambda: C.module_health_bar(chart_df),
        "channels": lambda: C.channel_health_bar(chart_df),
        "age_hist": lambda: C.age_hours_histogram(f),
        "signal_box": lambda: C.signal_box_by_status(f),
    }
    fig = chart_map.get(chart, chart_map["online_pie"])()

    f = annotate_realtime(f, _device_severity_map(age_hours=age_hours))

    table_cols = [
        "Severity",
        "AlertKinds",
        "DeviceName",
        "DeviceID",
        "Fleet",
        "StatusType",
        "AgeHours",
        "Ignition",
        "MobileNetwork",
        "GPSModule",
        "NotRecordingFlag",
        "devVoltage",
        "batVoltage",
        "MobileSignalStrength",
    ]
    table_cols = [c for c in table_cols if c in f.columns]

    return {
        "title": "Real-Time Device Status",
        "subtitle": "Most recent reported state for every tracked device.",
        "loading": False,
        "kpis": kpis,
        "chart_html": figure_html(fig),
        "table_html": df_to_table_html(f, table_cols),
        "fleet_opts": fleet_opts,
        "status_opts": status_opts,
        "ignition_opts": ignition_opts,
        "fleets": fleets,
        "statuses": statuses,
        "ignitions": ignitions,
        "ch_filter": ch_filter,
        "chart": chart,
        "age_hours": age_hours,
    }


def alarms_context(
    *,
    fleets: list[str] | None = None,
    alarm_types: list[str] | None = None,
    severity: str = "all",
    chart: str = "type_pie",
) -> dict[str, Any]:
    df = _normalize_alarm_frame(_alarms_df())
    fleets = _parse_multi(fleets)
    alarm_types = _parse_multi(alarm_types)
    severity = str(severity or "all").strip().lower()
    if severity == "all":
        try:
            from flask import request as flask_request
            severity = str(flask_request.args.get("severity", "all") or "all").strip().lower()
        except Exception:
            severity = "all"
    if severity not in ("high", "critical"):
        severity = "all"

    fleet_opts: list[str] = []
    type_opts: list[str] = []
    if df is not None and not df.empty:
        fleet_opts = _fleet_options(df=df)
        type_opts = sorted(
            {canonical_alarm_name(str(x)) for x in df["AlarmName"].dropna() if str(x).strip()}
            | set(TARGET_ALARMS)
        )
    else:
        fleet_opts = _fleet_options()

    if df is None:
        return {
            "title": "Alarms — Last 24 hours",
            "subtitle": "Watchlist: power loss, video loss, offline, SD card missing, storage error.",
            "loading": True,
            "kpis": [],
            "chart_html": _vss_unavailable_fig("Fetching alarms"),
            "table_html": df_to_table_html(None),
            "fleet_opts": fleet_opts,
            "type_opts": type_opts,
            "fleets": fleets,
            "alarm_types": alarm_types,
            "severity": severity,
            "chart": chart,
        }

    sev_map = _device_severity_map()
    f = _filter_alarms(df, fleets=fleets, alarm_types=alarm_types)
    f = annotate_alarms(f, sev_map)
    if severity == "high":
        f = f[f["Severity"].astype(str) == "High"]
    elif severity == "critical":
        f = f[f["Severity"].astype(str) == "Critical"]
    total = int(len(f))
    devices_with_alarm = int(f["DeviceID"].nunique()) if total else 0
    distinct_types = int(f["AlarmName"].nunique()) if total else 0
    last_seen = "-"
    if total and "AlarmTime" in f.columns and pd.notna(f["AlarmTime"].max()):
        last_seen = f["AlarmTime"].max().strftime("%Y-%m-%d %H:%M:%S")

    kpis = [
        kpi_dict("Alarm events", f"{total:,}", border_accent="#3B82F6"),
        kpi_dict("Devices alarming", f"{devices_with_alarm:,}", accent=DHL_RED, border_accent=DHL_RED),
        kpi_dict("Distinct alarm types", f"{distinct_types:,}", accent=DHL_YELLOW, border_accent=DHL_YELLOW),
        kpi_dict("Most recent event", last_seen, accent="#3B3B3B", border_accent="#6B7280"),
    ]
    kpis.extend(_severity_kpis(sev_map))

    chart_map = {
        "type_pie": lambda: C.alarm_type_pie(f),
        "per_hour": lambda: C.alarms_per_hour_line(f),
        "top_devices": lambda: C.top_devices_by_alarms(f),
        "heatmap": lambda: C.fleet_alarm_heatmap(f),
        "map": lambda: C.alarm_map(f),
    }
    fig = chart_map.get(chart, chart_map["type_pie"])()

    table_cols = [
        "Severity",
        "Alert",
        "AlarmTime",
        "DeviceName",
        "DeviceID",
        "Fleet",
        "AlarmName",
        "Speed",
        "PlateNo",
        "Lat",
        "Lon",
    ]
    return {
        "title": "Alarms — Last 24 hours",
        "subtitle": "First watchlist trigger is High; a second of power / video / offline / SD / storage on the same vehicle is Critical.",
        "loading": False,
        "kpis": kpis,
        "chart_html": figure_html(fig),
        "table_html": df_to_table_html(f, table_cols),
        "fleet_opts": fleet_opts,
        "type_opts": type_opts,
        "fleets": fleets,
        "alarm_types": alarm_types,
        "severity": severity,
        "chart": chart,
    }


def _device_picker_options(*, dhl_only: bool = True) -> list[dict]:
    """Device picker options — DHL fleet assets from the dashboard cache."""
    devices = _devices_df()
    rt_df = _realtime_df() if not dhl_only else None
    options: list[dict] = []
    if rt_df is not None and not rt_df.empty:
        src = rt_df[["DeviceID", "DeviceName", "Fleet"]].fillna("").astype(str).drop_duplicates()
    elif devices is not None and not devices.empty:
        src = devices[["DeviceID", "DeviceName", "Fleet"]].fillna("").astype(str).drop_duplicates()
    else:
        return options

    src = src.sort_values(["Fleet", "DeviceName"])
    for _, row in src.iterrows():
        options.append(
            {
                "id": row["DeviceID"],
                "label": f"{row['DeviceName']} ({row['DeviceID']}) — {row['Fleet']}",
            }
        )
    return options


def _real_video_url(
    *,
    token: str,
    device_id: str,
    channel: int,
    base_url: str,
    embed_origin: str = "",
) -> str:
    """VSS §5.1 RealVideo.html — one channel per iframe (wnum=1, panel=0)."""
    q = (
        f"token={quote(token, safe='')}"
        f"&deviceId={quote(str(device_id), safe='')}"
        f"&chs={int(channel)}"
        f"&stream=0"
        f"&wnum=1"
        f"&panel=0"
        f"&buffer=2000"
    )
    if embed_origin:
        return f"{embed_origin.rstrip('/')}/vss/apiPage/RealVideo.html?{q}"
    base = (base_url or "").rstrip("/")
    return f"{base}/vss/apiPage/RealVideo.html?{q}"


def _camera_embed_iframes(*, page_scheme: str, vss_base_url: str, use_vss_proxy: bool) -> bool:
    """Browsers block HTTP VSS iframes on HTTPS pages unless we proxy VSS on the same origin."""
    if os.environ.get("DHL_CAMERA_FORCE_IFRAME", "0").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if use_vss_proxy and str(page_scheme or "").lower() == "https":
        return True
    vss_https = str(vss_base_url or "").lower().startswith("https://")
    page_https = str(page_scheme or "").lower() == "https"
    return not (page_https and not vss_https)


def camera_context(
    *,
    device_id: str | None = None,
    page_scheme: str = "http",
    request_host: str = "",
) -> dict[str, Any]:
    rt_df = _realtime_df()
    options = _device_picker_options(dhl_only=True)

    rt_row = None
    kpis: list[dict] = []
    channels: list[dict] = []
    token_ok = False
    token_error = ""
    vss_base = active_base_url()
    use_vss_proxy = vss_embed_proxy_enabled() and str(page_scheme or "").lower() == "https" and bool(request_host)
    embed_origin = f"{page_scheme}://{request_host}" if use_vss_proxy else ""
    embed_iframes = _camera_embed_iframes(
        page_scheme=page_scheme,
        vss_base_url=vss_base,
        use_vss_proxy=use_vss_proxy,
    )

    tok_pair = try_token_without_login()
    token = tok_pair[0] if tok_pair else ""

    if not token:
        token_error = last_vss_error() or "VSS token not available — click Refresh data or wait for the next token refresh."
    else:
        token_ok = True

    if device_id:
        if rt_df is not None and not rt_df.empty:
            match = rt_df[rt_df["DeviceID"].astype(str) == str(device_id)]
            if not match.empty:
                rt_row = match.iloc[0]

        if rt_row is not None:
            kpis.append(kpi_dict("Status", str(rt_row.get("StatusType") or "Unknown")))
            kpis.append(kpi_dict("Fleet", str(rt_row.get("Fleet") or "-")))
            age = rt_row.get("AgeHours")
            if pd.notna(age):
                kpis.append(kpi_dict("Last report (h)", f"{float(age):.1f}"))
            lost = str(rt_row.get("VideoLostChannels") or "").strip()
            ign_on = str(rt_row.get("Ignition") or "").strip().lower() == "on"
            online = pd.notna(age) and float(age) <= 6.0
            if lost and ign_on and online:
                kpis.append(kpi_dict("Video lost (RT)", lost, accent=DHL_RED, border_accent=DHL_RED))
            else:
                kpis.append(kpi_dict("Video lost (RT)", "None", accent="#16A34A", border_accent="#16A34A"))

        for ch in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1):
            col = f"VideoLost_Ch{ch}"
            if rt_row is not None and col in rt_row.index:
                status = str(rt_row.get(col) or "Unknown")
                ok = status == "Working"
            else:
                status = "Unknown"
                ok = True

            feed_url = (
                _real_video_url(
                    token=token,
                    device_id=str(device_id),
                    channel=ch,
                    base_url=vss_base,
                    embed_origin=embed_origin,
                )
                if token_ok
                else ""
            )
            open_url = (
                _real_video_url(token=token, device_id=str(device_id), channel=ch, base_url=vss_base)
                if token_ok
                else ""
            )
            channels.append(
                {
                    "channel": ch,
                    "label": f"CH{ch}",
                    "status": status,
                    "ok": ok,
                    "url": feed_url,
                    "open_url": open_url,
                }
            )

    device_selected_label = ""
    if device_id:
        for opt in options:
            if str(opt.get("id")) == str(device_id):
                device_selected_label = str(opt.get("label") or "")
                break

    return {
        "title": "Camera",
        "subtitle": "Search a DHL fleet vehicle and view live VSS feeds on CH1–CH4 (same device list as Real-Time Status).",
        "device_id": device_id or "",
        "device_selected_label": device_selected_label,
        "device_options": options,
        "kpis": kpis,
        "channels": channels,
        "token_ok": token_ok,
        "token_error": token_error,
        "embed_iframes": embed_iframes,
        "use_vss_proxy": use_vss_proxy,
        "vss_base_url": vss_base,
        "vss_stream_port": os.environ.get("VSS_STREAM_PORT", "33122"),
        "vss_stream_ws_port": os.environ.get("VSS_STREAM_WS_PORT", "36301"),
        "vss_stream_host": _stream_wss_hostname(),
    }


def device_context(*, device_id: str | None = None) -> dict[str, Any]:
    rt_df = _realtime_df()
    devices = _devices_df()
    alarms = _normalize_alarm_frame(_alarms_df())

    options = _device_picker_options(dhl_only=False)

    rt_row = None
    a_dev = pd.DataFrame()
    kpis: list[dict] = []
    vss_faults: list[dict] = []
    mix_faults: list[dict] = []
    mix_match: dict[str, str] = {}

    if device_id:
        mix_df = _mix_df() if mix_integration_enabled() else None
        mix_row = None

        if rt_df is not None and not rt_df.empty:
            match = rt_df[rt_df["DeviceID"].astype(str) == str(device_id)]
            if not match.empty:
                rt_row = match.iloc[0]
        if alarms is not None and not alarms.empty:
            a_dev = alarms[alarms["DeviceID"].astype(str) == str(device_id)].copy()

        dev_row = None
        if devices is not None and not devices.empty:
            dev_match = devices[devices["DeviceID"].astype(str) == str(device_id)]
            if not dev_match.empty:
                dev_row = dev_match.iloc[0]

        device_name = ""
        if rt_row is not None:
            device_name = str(rt_row.get("DeviceName") or "")
        elif dev_row is not None:
            device_name = str(dev_row.get("DeviceName") or "")

        if device_name and mix_df is not None:
            mix_row = find_mix_asset_for_vss_device(device_name=device_name, mix_df=mix_df)
            if mix_row is not None:
                mix_match = {
                    "asset_name": str(mix_row.get("AssetName") or ""),
                    "registration": str(mix_row.get("Registration") or ""),
                    "group": str(mix_row.get("GroupName") or ""),
                }

        if rt_row is not None:
            kpis.append(kpi_dict("Status", str(rt_row.get("StatusType") or "Unknown")))
            kpis.append(kpi_dict("Fleet", str(rt_row.get("Fleet") or "-")))
            age = rt_row.get("AgeHours")
            if pd.notna(age):
                kpis.append(kpi_dict("Age (hours)", f"{float(age):.1f}"))
            if mix_match:
                kpis.append(
                    kpi_dict(
                        "MiX match",
                        mix_match.get("registration") or mix_match.get("asset_name") or "Matched",
                        accent="#F59E0B",
                        border_accent="#F59E0B",
                    )
                )
        elif dev_row is not None:
            kpis.append(kpi_dict("Device", str(dev_row.get("DeviceName") or device_id)))
            kpis.append(kpi_dict("Fleet", str(dev_row.get("Fleet") or "-")))
            kpis.append(kpi_dict("Device ID", str(dev_row.get("DeviceID") or device_id)))
            if mix_match:
                kpis.append(
                    kpi_dict(
                        "MiX match",
                        mix_match.get("registration") or mix_match.get("asset_name") or "Matched",
                        accent="#F59E0B",
                        border_accent="#F59E0B",
                    )
                )
        elif not a_dev.empty:
            kpis.append(kpi_dict("Alarms (24h)", f"{len(a_dev):,}", accent=DHL_RED))

        vss_faults = _build_vss_faults(rt_row, a_dev)
        if rt_row is None and dev_row is not None:
            vss_err = last_vss_error()
            if vss_err:
                vss_faults.insert(
                    0,
                    {"label": "VSS live status unavailable (session expired)", "ok": False},
                )
            elif rt_df is None:
                vss_faults.insert(0, {"label": "VSS live status still loading", "ok": True})

        if mix_integration_enabled():
            mix_faults = _build_mix_faults(mix_row, matched=mix_row is not None)

    device_selected_label = ""
    if device_id:
        for opt in options:
            if str(opt.get("id")) == str(device_id):
                device_selected_label = str(opt.get("label") or "")
                break

    return {
        "title": "Device Drilldown",
        "subtitle": "Search or pick a vehicle to see VSS and MiX health checks for that asset.",
        "device_id": device_id or "",
        "device_selected_label": device_selected_label,
        "device_options": options,
        "kpis": kpis,
        "vss_faults": vss_faults,
        "mix_faults": mix_faults,
        "mix_match": mix_match,
        "mix_enabled": mix_integration_enabled(),
        "faults": vss_faults + mix_faults,
    }


_VSS_MODULE_CHECKS = (
    ("Mobile network", "MobileNetwork"),
    ("GPS module", "GPSModule"),
    ("G-Sensor", "GsensorModule"),
    ("Wi-Fi", "WifiModule"),
    ("Recording / video", "NotRecordingFlag"),
    ("Disk / storage", "DiskLossFlag"),
)


def _build_vss_faults(rt_row: pd.Series | None, a_dev: pd.DataFrame) -> list[dict]:
    faults: list[dict] = []

    for label, field in _VSS_MODULE_CHECKS:
        if rt_row is not None:
            ok = str(rt_row.get(field) or "") == "Working"
            faults.append({"label": f"VSS {label}", "ok": ok})
        else:
            faults.append({"label": f"VSS {label}", "ok": True})

    if rt_row is not None:
        disk_detail = str(rt_row.get("DiskLossDetail") or "").strip()
        if disk_detail:
            faults.append({"label": f"VSS disk loss detail: {disk_detail}", "ok": False})

        for ch in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1):
            col = f"VideoLost_Ch{ch}"
            if col in rt_row.index:
                ok = str(rt_row.get(col) or "") == "Working"
                faults.append({"label": f"VSS video CH{ch}", "ok": ok})

        status = str(rt_row.get("StatusType") or "")
        if status:
            faults.append(
                {
                    "label": f"VSS status: {status}",
                    "ok": status in ("Normal", "Stale"),
                }
            )

    alarm_counts: dict[str, int] = {}
    if a_dev is not None and not a_dev.empty and "AlarmName" in a_dev.columns:
        for name, count in a_dev["AlarmName"].value_counts().items():
            alarm_counts[canonical_alarm_name(str(name))] = int(count)

    severity_rec = build_device_severity(
        a_dev,
        pd.DataFrame([rt_row]) if rt_row is not None else None,
    )
    device_id = str(rt_row.get("DeviceID") or "") if rt_row is not None else ""
    if not device_id and a_dev is not None and not a_dev.empty:
        device_id = str(a_dev.iloc[0].get("DeviceID") or "")
    rec = severity_rec.get(device_id) or next(iter(severity_rec.values()), {})
    if rec.get("severity"):
        kinds = ", ".join(rec.get("kinds") or [])
        faults.insert(
            0,
            {
                "label": f"Alert severity: {rec['severity']}" + (f" ({kinds})" if kinds else ""),
                "ok": False,
            },
        )

    for alarm_name in TARGET_ALARMS:
        count = alarm_counts.get(alarm_name, 0)
        label = f"VSS alarm (24h): {alarm_name}"
        if count:
            detail = ""
            if (
                alarm_name == _DISK_LOSS_ALARM_LABEL
                and a_dev is not None
                and not a_dev.empty
                and "DiskLossDetail" in a_dev.columns
            ):
                details = [
                    str(v).strip()
                    for v in a_dev.loc[a_dev["AlarmName"] == alarm_name, "DiskLossDetail"]
                    if str(v).strip()
                ]
                if details:
                    detail = f" — {details[0]}"
            label = f"{label} ({count}x){detail}"
        faults.append({"label": label, "ok": count == 0})

    return faults


def _build_mix_faults(mix_row: pd.Series | None, *, matched: bool) -> list[dict]:
    active: set[str] = set()
    if mix_row is not None:
        active = {part.strip() for part in str(mix_row.get("Issues") or "").split(";") if part.strip()}

    faults: list[dict] = []
    for issue in ALL_ISSUES:
        if not matched:
            faults.append({"label": f"MiX: {issue} (no asset match)", "ok": True})
            continue
        faults.append({"label": f"MiX: {issue}", "ok": issue not in active})

    if matched and mix_row is not None:
        pass  # ALL_ISSUES covers comm / GPS / RPM flags

    return faults


def _build_faults(rt_row: pd.Series, a_dev: pd.DataFrame) -> list[dict]:
    return _build_vss_faults(rt_row, a_dev)


def mix_context(*, issues: list[str] | None = None) -> dict[str, Any]:
    issues = _parse_multi(issues)
    if not mix_integration_enabled():
        return {
            "title": "MiX Telematics",
            "subtitle": "MiX integration is disabled.",
            "enabled": False,
            "notice": "Set MIX_ENABLED=1 and add accounts.json, then restart the dashboard.",
            "kpis": [],
            "chart_html": "",
            "table_html": "",
            "issue_opts": [],
            "issues": issues,
        }

    df = _mix_df()
    mix_err = last_mix_error()
    if df is None:
        return {
            "title": "MiX Telematics",
            "subtitle": "Diageo DHL sites on MiX — health flags and diagnostics.",
            "enabled": True,
            "notice": mix_err or "",
            "loading": not mix_err,
            "kpis": [kpi_dict("MiX assets", "…", accent="#F59E0B", border_accent="#F59E0B", sub="loading")] if not mix_err else [],
            "chart_html": figure_html(C.loading_fig("Loading MiX health…")) if not mix_err else "",
            "table_html": df_to_table_html(None) if not mix_err else "",
            "issue_opts": list(ALL_ISSUES),
            "issues": issues,
        }

    f = df.copy()
    if issues and "Issues" in f.columns:
        mask = f["Issues"].astype(str).apply(lambda s: any(i in s for i in issues))
        f = f[mask]

    flagged = int((f["IssueCount"] > 0).sum()) if "IssueCount" in f.columns and not f.empty else 0
    inventory_scan = (
        not f.empty
        and "ScanMode" in f.columns
        and f["ScanMode"].astype(str).str.lower().str.startswith("inventory").all()
    )
    notice = ""
    if inventory_scan:
        scan_modes = (
            f["ScanMode"].astype(str).str.lower().tolist() if "ScanMode" in f.columns else []
        )
        has_comm = any("comm" in mode for mode in scan_modes)
        has_rpm = any("rpm" in mode for mode in scan_modes)
        if has_comm and has_rpm:
            notice = (
                "Inventory + comm + RPM scan — flags 'Non downloading', 'No GPS data', "
                "and 'Diagnostic: no engine RPM (7d)'."
            )
        elif has_comm:
            notice = (
                "Inventory + comm scan — flags 'Non downloading' and 'No GPS data' from latest positions."
            )
        elif has_rpm:
            notice = (
                "Inventory + RPM diagnostic scan — only 'Diagnostic: no engine RPM (7d)' is evaluated "
                "from MiX event history."
            )
        else:
            notice = (
                "Inventory scan only — GPS, speed, and RPM health flags are not evaluated yet. "
                "Counts show registered assets, not confirmed faults."
            )
    kpis = [
        kpi_dict("Assets shown", f"{len(f):,}", border_accent="#3B82F6"),
        kpi_dict("With issues", f"{flagged:,}", accent=DHL_RED if flagged else "#2E8B57", border_accent=DHL_RED if flagged else "#2E8B57"),
    ]

    issue_opts = list(ALL_ISSUES)
    if "Issues" in df.columns:
        for raw in df["Issues"].dropna().astype(str):
            for part in raw.split(";"):
                part = part.strip()
                if part and part not in issue_opts:
                    issue_opts.append(part)
    # Keep canonical issue types first, then any legacy values from older snapshots.
    issue_opts.sort(key=lambda x: (x not in ALL_ISSUES, x))

    chart_html = figure_html(C.EMPTY_FIG)
    if not f.empty and "IssueCount" in f.columns:
        import plotly.express as px

        if "Issues" in f.columns:
            rows = []
            for _, row in f.iterrows():
                for issue in str(row.get("Issues") or "").split(";"):
                    issue = issue.strip()
                    if issue:
                        rows.append({"Issue": issue})
            if rows:
                counts = pd.DataFrame(rows)["Issue"].value_counts().reset_index()
                counts.columns = ["Issue", "Count"]
                fig = px.bar(counts, x="Issue", y="Count", color_discrete_sequence=[DHL_RED])
                fig.update_layout(margin=dict(l=20, r=20, t=40, b=80))
                chart_html = figure_html(fig)

    show_cols = [c for c in ["AssetName", "Registration", "IssueCount", "Issues", "LastSeen"] if c in f.columns]
    return {
        "title": "MiX Telematics",
        "subtitle": "Diageo DHL sites on MiX — health flags and diagnostics.",
        "enabled": True,
        "notice": notice,
        "loading": False,
        "kpis": kpis,
        "chart_html": chart_html,
        "table_html": df_to_table_html(f, show_cols),
        "issue_opts": issue_opts,
        "issues": issues,
    }
