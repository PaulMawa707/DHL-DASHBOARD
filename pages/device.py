"""Per-device drilldown page: filter one vehicle, see real-time faults + alarms."""

from __future__ import annotations

import dash
import pandas as pd
import plotly.express as px
from dash import Input, Output, callback, dash_table, dcc, html

import components as C
from data import TARGET_ALARMS, get_alarms_cached, get_dhl_devices_cached, get_realtime_cached, parse_channels

dash.register_page(__name__, path="/device", name="Device Drilldown", order=5)


# Voltage warning thresholds (commonly observed for these devices)
LOW_DEV_VOLTAGE = 9.0
LOW_BAT_VOLTAGE = 11.0


def layout():
    return html.Div(
        [
            html.H1("Device Drilldown", className="page-title"),
            html.Div(
                "Pick a vehicle to see every fault on it — current real-time issues plus the last 24h of alarms.",
                className="page-subtitle",
            ),
            html.Div(
                className="filter-row",
                children=[
                    dcc.Dropdown(id="dd-device", placeholder="Search and select a vehicle...", searchable=True),
                ],
            ),
            html.Div(id="dd-summary", className="kpi-row"),
            html.Div(id="dd-faults-card"),
            html.Div(
                className="chart-grid",
                children=[
                    html.Div(dcc.Graph(id="dd-alarm-bar"), className="chart-card"),
                    html.Div(dcc.Graph(id="dd-alarm-time"), className="chart-card"),
                    html.Div(dcc.Graph(id="dd-map"), className="chart-card full"),
                ],
            ),
            html.Div(
                className="table-card",
                children=[
                    html.Div("Alarm events for this vehicle", style={"fontWeight": 600, "marginBottom": "8px"}),
                    dash_table.DataTable(
                        id="dd-table",
                        page_size=15,
                        style_table={"overflowX": "auto"},
                        style_cell={"padding": "6px 10px", "fontFamily": "Inter, Segoe UI, Arial, sans-serif", "fontSize": 12},
                        style_header={"fontWeight": 600, "backgroundColor": "#F3F4F6"},
                        sort_action="native",
                        export_format="csv",
                    ),
                ],
            ),
        ]
    )


@callback(
    Output("dd-device", "options"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
)
def _populate_devices(_token, _n_load):
    df = get_realtime_cached()
    if df is None or df.empty:
        base = get_dhl_devices_cached()
        if base is None or base.empty:
            return []
        out = (
            base[["DeviceID", "DeviceName", "Fleet"]]
            .fillna("")
            .astype(str)
            .drop_duplicates()
            .sort_values(["Fleet", "DeviceName"])
        )
        return [
            {"label": f"{r['DeviceName']}  ({r['DeviceID']})  -  {r['Fleet']}", "value": r["DeviceID"]}
            for _, r in out.iterrows()
        ]
    out = (
        df[["DeviceID", "DeviceName", "Fleet"]]
        .fillna("")
        .astype(str)
        .drop_duplicates()
        .sort_values(["Fleet", "DeviceName"])
    )
    return [
        {"label": f"{r['DeviceName']}  ({r['DeviceID']})  -  {r['Fleet']}", "value": r["DeviceID"]}
        for _, r in out.iterrows()
    ]


def _fault_pill(label: str, ok: bool) -> html.Span:
    color = "#2E8B57" if ok else C.DHL_RED
    bg = "#E8F5EC" if ok else "#FCE6E8"
    return html.Span(
        label,
        style={
            "display": "inline-block",
            "padding": "4px 10px",
            "marginRight": "6px",
            "marginBottom": "6px",
            "borderRadius": "12px",
            "backgroundColor": bg,
            "color": color,
            "fontSize": "12px",
            "fontWeight": 600,
            "border": f"1px solid {color}",
        },
    )


def _list_item(text: str, *, level: str = "warn") -> html.Li:
    color = {"warn": C.DHL_RED, "info": "#3B3B3B", "ok": "#2E8B57"}.get(level, C.DHL_RED)
    return html.Li(text, style={"color": color, "marginBottom": "4px"})


def _build_faults_card(rt_row: pd.Series | None, a_dev: pd.DataFrame) -> html.Div:
    if rt_row is None and (a_dev is None or a_dev.empty):
        return html.Div()

    pills: list[html.Span] = []
    items: list[html.Li] = []

    if rt_row is not None:
        modules = [
            ("Mobile network", rt_row.get("MobileNetwork") == "Working"),
            ("GPS", rt_row.get("GPSModule") == "Working"),
            ("G-Sensor", rt_row.get("GsensorModule") == "Working"),
            ("Wi-Fi", rt_row.get("WifiModule") == "Working"),
            ("Video lost (ch)", rt_row.get("NotRecordingFlag") == "Working"),
        ]
        for label, ok in modules:
            pills.append(_fault_pill(f"{label}: {'OK' if ok else 'FAULT'}", ok))

        not_recording = parse_channels(str(rt_row.get("recordstateFormatter") or ""))
        video_lost = parse_channels(str(rt_row.get("videoloststateFormatter") or ""))
        camera_covered = parse_channels(str(rt_row.get("videomaskstateFormatter") or ""))
        if video_lost:
            items.append(
                _list_item(
                    f"Channels with video lost (videoloststateFormatter): "
                    f"{', '.join(f'CH{c}' for c in sorted(set(video_lost)))}"
                )
            )
        if not_recording:
            items.append(
                _list_item(
                    f"Recording formatter (recordstateFormatter): "
                    f"{', '.join(f'CH{c}' for c in sorted(set(not_recording)))}",
                    level="info",
                )
            )
        if camera_covered:
            items.append(_list_item(f"Camera covered on: {', '.join(f'CH{c}' for c in sorted(set(camera_covered)))}"))

        # Make debugging easier: show the raw VSS formatter strings that drive the flags above.
        raw_rec = str(rt_row.get("recordstateFormatter") or "").strip()
        raw_lost = str(rt_row.get("videoloststateFormatter") or "").strip()
        raw_mask = str(rt_row.get("videomaskstateFormatter") or "").strip()
        if raw_rec or raw_lost or raw_mask:
            items.append(_list_item(f"Raw(record): {raw_rec or '(empty)'}", level="info"))
            items.append(_list_item(f"Raw(video lost): {raw_lost or '(empty)'}", level="info"))
            items.append(_list_item(f"Raw(camera covered): {raw_mask or '(empty)'}", level="info"))

        status = str(rt_row.get("StatusType") or "")
        if status and status != "Normal":
            items.append(_list_item(f"Status: {status}", level="warn"))
        age_hours = rt_row.get("AgeHours")
        if pd.notna(age_hours):
            try:
                age = float(age_hours)
                if age > 24:
                    items.append(_list_item(f"Last status was {age:.1f}h ago (no recent report).", level="warn"))
            except (TypeError, ValueError):
                pass

        try:
            dev_v = float(rt_row.get("devVoltage")) if rt_row.get("devVoltage") not in (None, "") else None
        except (TypeError, ValueError):
            dev_v = None
        try:
            bat_v = float(rt_row.get("batVoltage")) if rt_row.get("batVoltage") not in (None, "") else None
        except (TypeError, ValueError):
            bat_v = None
        if dev_v is not None and dev_v < LOW_DEV_VOLTAGE:
            items.append(_list_item(f"Device voltage low: {dev_v:.1f}V (threshold {LOW_DEV_VOLTAGE:.1f}V)"))
        if bat_v is not None and bat_v < LOW_BAT_VOLTAGE:
            items.append(_list_item(f"Backup battery voltage low: {bat_v:.1f}V (threshold {LOW_BAT_VOLTAGE:.1f}V)"))

    alarm_summary: list[html.Span] = []
    if a_dev is not None and not a_dev.empty:
        counts = a_dev["AlarmName"].value_counts()
        for name, n in counts.items():
            color = C.DHL_RED if int(n) else "#3B3B3B"
            alarm_summary.append(
                html.Span(
                    f"{name}: {int(n)}",
                    style={
                        "display": "inline-block",
                        "padding": "4px 10px",
                        "marginRight": "6px",
                        "marginBottom": "6px",
                        "borderRadius": "12px",
                        "backgroundColor": "#FCE6E8",
                        "color": color,
                        "fontSize": "12px",
                        "fontWeight": 600,
                        "border": f"1px solid {color}",
                    },
                )
            )

    if not pills and not items and not alarm_summary:
        items.append(_list_item("No active faults or alarms in the last 24h.", level="ok"))

    return html.Div(
        className="table-card",
        style={"marginBottom": "14px"},
        children=[
            html.Div("Faults & alarms for this vehicle", style={"fontWeight": 600, "marginBottom": "8px"}),
            html.Div([
                html.Div("Module health", style={"fontWeight": 600, "marginBottom": "6px", "color": "#6B7280", "fontSize": "12px"}),
                html.Div(pills) if pills else html.Div("(no realtime status)", style={"color": "#6B7280"}),
            ], style={"marginBottom": "10px"}),
            html.Div([
                html.Div("Alarms in last 24h (target types only)", style={"fontWeight": 600, "marginBottom": "6px", "color": "#6B7280", "fontSize": "12px"}),
                html.Div(alarm_summary) if alarm_summary else html.Div("(no alarms in the last 24h)", style={"color": "#2E8B57"}),
            ], style={"marginBottom": "10px"}),
            html.Div([
                html.Div("Other detected issues", style={"fontWeight": 600, "marginBottom": "6px", "color": "#6B7280", "fontSize": "12px"}),
                html.Ul(items) if items else html.Div("(none)", style={"color": "#2E8B57"}),
            ]),
        ],
    )


@callback(
    Output("dd-summary", "children"),
    Output("dd-faults-card", "children"),
    Output("dd-alarm-bar", "figure"),
    Output("dd-alarm-time", "figure"),
    Output("dd-map", "figure"),
    Output("dd-table", "data"),
    Output("dd-table", "columns"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
    Input("dd-device", "value"),
)
def _update_drilldown(_token, _n_load, device_id):
    if not device_id:
        return (
            [html.Div("Select a vehicle above to see all of its faults and alarms.", style={"color": "#6B7280"})],
            html.Div(),
            C.EMPTY_FIG,
            C.EMPTY_FIG,
            C.EMPTY_FIG,
            [],
            [],
        )

    rt = get_realtime_cached()
    alarms = get_alarms_cached()
    if rt is None and alarms is None:
        return (
            [
                html.Div(
                    "Loading vehicle data — this page refreshes every few seconds.",
                    style={"color": "#6B7280", "fontWeight": 600},
                )
            ],
            html.Div(),
            C.loading_fig("Loading…"),
            C.loading_fig("Loading…"),
            C.loading_fig("Loading…"),
            [],
            [],
        )
    rt_still_loading = rt is None
    if rt is None:
        rt = pd.DataFrame()
    if alarms is None:
        alarms = pd.DataFrame()

    if rt.empty or "DeviceID" not in rt.columns:
        rt_match = pd.DataFrame()
    else:
        rt_match = rt[rt["DeviceID"].astype(str) == str(device_id)]
    if alarms.empty or "DeviceID" not in alarms.columns:
        a_dev = pd.DataFrame()
    else:
        a_dev = alarms[alarms["DeviceID"].astype(str) == str(device_id)]

    rt_row = rt_match.iloc[0] if not rt_match.empty else None

    if rt_row is None:
        sub = "Live status still loading…" if rt_still_loading else "No realtime row for this device yet"
        kpis = [C.kpi_card("Vehicle", device_id, accent=C.DHL_RED, sub=sub)]
    else:
        status = str(rt_row.get("StatusType") or "Unknown")
        kpis = [
            C.kpi_card("Vehicle", str(rt_row.get("DeviceName") or device_id), sub=str(device_id)),
            C.kpi_card("Fleet", str(rt_row.get("Fleet") or "-")),
            C.kpi_card(
                "Status",
                status,
                accent=C.DHL_RED if status != "Normal" else "#2E8B57",
            ),
            C.kpi_card(
                "Last seen",
                str(rt_row.get("Time") or "-"),
                sub=(f"AgeHours: {rt_row.get('AgeHours'):.2f}" if pd.notna(rt_row.get("AgeHours")) else ""),
            ),
            C.kpi_card("Battery (V)", str(rt_row.get("batVoltage") or "-")),
            C.kpi_card("Device (V)", str(rt_row.get("devVoltage") or "-")),
        ]

    faults_card = _build_faults_card(rt_row, a_dev)

    if a_dev.empty:
        bar = C.EMPTY_FIG
        line = C.EMPTY_FIG
        map_fig = C.EMPTY_FIG
        table_data: list[dict] = []
    else:
        counts = (
            a_dev["AlarmName"]
            .value_counts()
            .reindex(TARGET_ALARMS, fill_value=0)
            .loc[lambda s: s > 0]
        )
        if counts.empty:
            bar = C.EMPTY_FIG
        else:
            bar = px.bar(
                x=counts.values, y=counts.index, orientation="h",
                color_discrete_sequence=[C.DHL_RED],
            )
            bar.update_layout(
                **C.DEFAULT_LAYOUT,
                title="Alarms in last 24h (target types)",
                xaxis_title="Count",
                yaxis_title="",
                yaxis=dict(autorange="reversed"),
            )

        per_hour = a_dev.dropna(subset=["AlarmTime"]).copy()
        per_hour["Hour"] = per_hour["AlarmTime"].dt.floor("h")
        grp = per_hour.groupby(["Hour", "AlarmName"]).size().reset_index(name="Count")
        line = px.area(grp, x="Hour", y="Count", color="AlarmName") if not grp.empty else C.EMPTY_FIG
        if not grp.empty:
            line.update_layout(**C.DEFAULT_LAYOUT, title="Alarms per hour", xaxis_title="", yaxis_title="Alarms")

        map_fig = C.alarm_map(a_dev)

        table_data = (
            a_dev.assign(AlarmTime=lambda d: d["AlarmTime"].dt.strftime("%Y-%m-%d %H:%M:%S"))
            [["AlarmTime", "AlarmName", "Speed", "Lat", "Lon"]]
            .to_dict("records")
        )

    columns = [{"name": c, "id": c} for c in ["AlarmTime", "AlarmName", "Speed", "Lat", "Lon"]]
    return kpis, faults_card, bar, line, map_fig, table_data, columns
