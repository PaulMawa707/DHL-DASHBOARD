"""Real-Time Device Status page."""

from __future__ import annotations

import dash
import pandas as pd
from dash import Input, Output, callback, dash_table, dcc, html

import components as C
from data import RT_VIDEO_LOST_CHANNEL_MAX, get_realtime_cached

dash.register_page(__name__, path="/realtime", name="Real-Time Status", order=3)


def _options(values):
    return [{"label": str(v), "value": str(v)} for v in sorted({str(x) for x in values if str(x).strip()})]


def layout():
    return html.Div(
        [
            html.H1("Real-Time Device Status", className="page-title"),
            html.Div(
                "Most recent reported state for every DHL device. "
                "Channel / KPI 'video lost' uses videoloststateFormatter. "
                "Pick 'Video lost on CH1–CH4 only' to list assets where that camera channel is in the lost list.",
                className="page-subtitle",
            ),
            html.Div(
                className="filter-row",
                children=[
                    dcc.Dropdown(id="rt-fleet", multi=True, placeholder="Fleet (all)"),
                    dcc.Dropdown(id="rt-status", multi=True, placeholder="Status type (all)"),
                    dcc.Dropdown(
                        id="rt-ignition",
                        multi=True,
                        placeholder="Ignition (all)",
                    ),
                    dcc.Dropdown(
                        id="rt-channel-video-lost",
                        clearable=False,
                        value="all",
                        options=[{"label": "Video lost: all channels", "value": "all"}]
                        + [
                            {"label": f"Video lost on CH{n} only", "value": str(n)}
                            for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1)
                        ],
                        placeholder="Filter by channel (video lost)",
                    ),
                    dcc.Dropdown(
                        id="rt-chart",
                        clearable=False,
                        value="online_pie",
                        options=[
                            {"label": "Online vs Offline (pie)", "value": "online_pie"},
                            {"label": "Status type breakdown (donut)", "value": "status_donut"},
                            {"label": "Module health (Mobile / GPS / G-Sensor / Wi-Fi / Video lost)", "value": "modules"},
                            {"label": "Camera channels (video lost / covered)", "value": "channels"},
                            {"label": "Status age (hours since last report)", "value": "age_hist"},
                            {"label": "Mobile signal by status", "value": "signal_box"},
                        ],
                    ),
                ],
            ),
            html.Div(id="rt-kpis", className="kpi-row"),
            html.Div(
                className="chart-grid",
                children=[
                    html.Div(dcc.Graph(id="rt-main-chart"), className="chart-card full"),
                ],
            ),
            html.Div(
                className="table-card",
                children=[
                    html.Div("Device-level table", style={"fontWeight": 600, "marginBottom": "8px"}),
                    dash_table.DataTable(
                        id="rt-table",
                        page_size=15,
                        style_table={"overflowX": "auto"},
                        style_cell={"padding": "6px 10px", "fontFamily": "Inter, Segoe UI, Arial, sans-serif", "fontSize": 12},
                        style_header={"fontWeight": 600, "backgroundColor": "#F3F4F6"},
                        sort_action="native",
                        filter_action="native",
                        export_format="csv",
                    ),
                ],
            ),
        ]
    )


@callback(
    Output("rt-fleet", "options"),
    Output("rt-status", "options"),
    Output("rt-ignition", "options"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
)
def _populate_filters(_token, _n_load):
    df = get_realtime_cached()
    if df is None or df.empty:
        return [], [], []
    return _options(df["Fleet"].dropna()), _options(df["StatusType"].dropna()), _options(df.get("Ignition", pd.Series(dtype=str)).dropna())


def _filtered(df: pd.DataFrame, fleet, status, ignition, ch_filter) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if fleet:
        out = out[out["Fleet"].astype(str).isin(fleet)]
    if status:
        out = out[out["StatusType"].astype(str).isin(status)]
    if ignition and "Ignition" in out.columns:
        out = out[out["Ignition"].astype(str).isin(ignition)]
    cf = (ch_filter if ch_filter is not None else "all")
    if str(cf).strip().lower() not in ("", "all", "none"):
        try:
            n = int(str(cf).strip())
        except ValueError:
            n = 0
        if 1 <= n <= RT_VIDEO_LOST_CHANNEL_MAX:
            col = f"VideoLost_Ch{n}"
            if col in out.columns:
                out = out[out[col].astype(str) == "Not Working"]
    return out


@callback(
    Output("rt-kpis", "children"),
    Output("rt-main-chart", "figure"),
    Output("rt-table", "data"),
    Output("rt-table", "columns"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
    Input("age-hours-threshold", "value"),
    Input("rt-fleet", "value"),
    Input("rt-status", "value"),
    Input("rt-ignition", "value"),
    Input("rt-channel-video-lost", "value"),
    Input("rt-chart", "value"),
)
def _update_realtime(_token, _n_load, age_threshold, fleet, status, ignition, ch_filter, which_chart):
    df = get_realtime_cached()
    if df is None:
        return (
            [
                html.Div(
                    "Loading realtime status — the page refreshes every few seconds until data is ready.",
                    style={"color": "#6B7280", "fontWeight": 600},
                )
            ],
            C.loading_fig("Fetching live device status from VSS…"),
            [],
            [],
        )

    age_threshold = float(age_threshold or 6)
    f = _filtered(df, fleet, status, ignition, ch_filter)
    if f is None:
        f = pd.DataFrame()

    age = pd.to_numeric(f.get("AgeHours"), errors="coerce") if not f.empty else pd.Series(dtype=float)
    total = len(f)
    online = int((age.notna() & (age <= age_threshold)).sum()) if total else 0
    offline = int((age.notna() & (age > age_threshold)).sum()) if total else 0
    unknown = int(age.isna().sum()) if total else 0
    video_lost_devices = int((f.get("NotRecordingFlag", pd.Series(dtype=str)) == "Not Working").sum()) if total else 0

    kpis = [
        C.kpi_card("Devices in view", f"{total:,}"),
        C.kpi_card("Online", f"{online:,}", accent="#2E8B57"),
        C.kpi_card("Offline", f"{offline:,}", accent=C.DHL_RED),
        C.kpi_card("Status unknown", f"{unknown:,}", accent="#999"),
        C.kpi_card("Video lost (any CH)", f"{video_lost_devices:,}", accent=C.DHL_YELLOW),
    ]

    if which_chart == "online_pie":
        fig = C.online_offline_pie(f, age_threshold)
    elif which_chart == "status_donut":
        fig = C.status_type_donut(f)
    elif which_chart == "modules":
        fig = C.module_health_bar(f)
    elif which_chart == "channels":
        fig = C.channel_health_bar(f)
    elif which_chart == "age_hist":
        fig = C.age_hours_histogram(f)
    elif which_chart == "signal_box":
        fig = C.signal_box_by_status(f)
    else:
        fig = C.EMPTY_FIG

    table_cols_pref = [
        "DeviceName", "DeviceID", "Fleet", "StatusType", "Ignition", "AgeHours",
        "MobileNetwork", "GPSModule", "GsensorModule", "WifiModule",
        "NotRecordingFlag", "VideoLostChannels", "recordstateFormatter", "videoloststateFormatter",
        "videomaskstateFormatter", "signalValue", "devVoltage", "batVoltage", "Time",
    ]
    table_cols = [c for c in table_cols_pref if c in f.columns]
    data = f[table_cols].to_dict("records") if not f.empty else []
    _rt_col_names = {
        "NotRecordingFlag": "Video lost (any CH)",
        "VideoLostChannels": "Video lost CH list",
        "videoloststateFormatter": "videoloststateFormatter (raw)",
        "recordstateFormatter": "recordstateFormatter (raw)",
    }
    columns = [{"name": _rt_col_names.get(c, c), "id": c} for c in table_cols]

    return kpis, fig, data, columns
