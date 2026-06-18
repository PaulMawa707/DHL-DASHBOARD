"""Alarms (last 24h) page."""

from __future__ import annotations

import dash
import pandas as pd
from dash import Input, Output, callback, dash_table, dcc, html

import components as C
from data import get_alarms_cached

dash.register_page(__name__, path="/alarms", name="Alarms (24h)", order=4)


def _options(values):
    return [{"label": str(v), "value": str(v)} for v in sorted({str(x) for x in values if str(x).strip()})]


def layout():
    return html.Div(
        [
            html.H1("Alarms - Last 24 hours", className="page-title"),
            html.Div("Every alarm event raised across the DHL fleet in the last 24 hours.", className="page-subtitle"),
            html.Div(
                className="filter-row",
                children=[
                    dcc.Dropdown(id="al-fleet", multi=True, placeholder="Fleet (all)"),
                    dcc.Dropdown(id="al-type", multi=True, placeholder="Alarm type (all)"),
                    dcc.Dropdown(
                        id="al-chart",
                        clearable=False,
                        value="type_pie",
                        options=[
                            {"label": "Alarms by type (pie)", "value": "type_pie"},
                            {"label": "Alarms per hour (area)", "value": "per_hour"},
                            {"label": "Top devices by alarm count (bar)", "value": "top_devices"},
                            {"label": "Fleet x Alarm Type (heatmap)", "value": "heatmap"},
                            {"label": "Alarm locations (map)", "value": "map"},
                        ],
                    ),
                ],
            ),
            html.Div(id="al-kpis", className="kpi-row"),
            html.Div(
                className="chart-grid",
                children=[
                    html.Div(dcc.Graph(id="al-main-chart"), className="chart-card full"),
                ],
            ),
            html.Div(
                className="table-card",
                children=[
                    html.Div("Raw alarm events", style={"fontWeight": 600, "marginBottom": "8px"}),
                    dash_table.DataTable(
                        id="al-table",
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
    Output("al-fleet", "options"),
    Output("al-type", "options"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
)
def _populate_alarm_filters(_token, _n_load):
    df = get_alarms_cached()
    if df is None or df.empty:
        return [], []
    return _options(df["Fleet"].dropna()), _options(df["AlarmName"].dropna())


def _filtered(df: pd.DataFrame, fleet, alarm_type) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if fleet:
        out = out[out["Fleet"].astype(str).isin(fleet)]
    if alarm_type:
        out = out[out["AlarmName"].astype(str).isin(alarm_type)]
    return out


@callback(
    Output("al-kpis", "children"),
    Output("al-main-chart", "figure"),
    Output("al-table", "data"),
    Output("al-table", "columns"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
    Input("al-fleet", "value"),
    Input("al-type", "value"),
    Input("al-chart", "value"),
)
def _update_alarms(_token, _n_load, fleet, alarm_type, which_chart):
    df = get_alarms_cached()
    if df is None:
        return (
            [
                html.Div(
                    "Loading 24h alarms — this page will populate automatically.",
                    style={"color": "#6B7280", "fontWeight": 600},
                )
            ],
            C.loading_fig("Fetching alarm events from VSS…"),
            [],
            [],
        )

    f = _filtered(df, fleet, alarm_type)
    if f is None:
        f = pd.DataFrame()

    total = int(len(f))
    devices_with_alarm = int(f["DeviceID"].nunique()) if total else 0
    distinct_types = int(f["AlarmName"].nunique()) if total else 0
    last_seen = f["AlarmTime"].max().strftime("%Y-%m-%d %H:%M:%S") if total and pd.notna(f["AlarmTime"].max()) else "-"

    kpis = [
        C.kpi_card("Alarm events", f"{total:,}"),
        C.kpi_card("Devices alarming", f"{devices_with_alarm:,}", accent=C.DHL_RED),
        C.kpi_card("Distinct alarm types", f"{distinct_types:,}", accent=C.DHL_YELLOW),
        C.kpi_card("Most recent event", last_seen, accent="#3B3B3B"),
    ]

    if which_chart == "type_pie":
        fig = C.alarm_type_pie(f)
    elif which_chart == "per_hour":
        fig = C.alarms_per_hour_line(f)
    elif which_chart == "top_devices":
        fig = C.top_devices_by_alarms(f)
    elif which_chart == "heatmap":
        fig = C.fleet_alarm_heatmap(f)
    elif which_chart == "map":
        fig = C.alarm_map(f)
    else:
        fig = C.EMPTY_FIG

    table_cols = ["AlarmTime", "DeviceName", "DeviceID", "Fleet", "AlarmName", "Speed", "PlateNo", "Lat", "Lon"]
    table_cols = [c for c in table_cols if c in f.columns]
    data = f[table_cols].assign(AlarmTime=lambda d: d["AlarmTime"].dt.strftime("%Y-%m-%d %H:%M:%S")).to_dict("records") if not f.empty else []
    columns = [{"name": c, "id": c} for c in table_cols]
    return kpis, fig, data, columns
