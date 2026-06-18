"""Overview page: high-level KPIs + the four headline charts."""

from __future__ import annotations

import dash
from dash import Input, Output, callback, dcc, html

import components as C
from data import (
    alarms_kpi_label,
    get_alarms_cached,
    get_dhl_devices_cached,
    get_mix_health_cached,
    get_realtime_cached,
    mix_integration_enabled,
)

dash.register_page(__name__, path="/", name="Overview", order=1)


def _safe_load():
    """Read data from the warm cache only — never block the page render.

    The dashboard pre-warms the cache at startup. If anything is still loading,
    we return None and the UI will say so.
    """
    return get_dhl_devices_cached(), get_realtime_cached(), get_alarms_cached()


def layout():
    return html.Div(
        [
            html.H1("Fleet Overview", className="page-title"),
            html.Div(
                "VSS fleet health (devices, status, alarms). MiX asset health is on the MiX Health page."
                if mix_integration_enabled()
                else "Live snapshot of the entire DHL fleet.",
                className="page-subtitle",
            ),
            html.Div(id="overview-kpis", className="kpi-row"),
            html.Div(
                className="chart-grid",
                children=[
                    html.Div(dcc.Graph(id="overview-online-pie"), className="chart-card"),
                    html.Div(dcc.Graph(id="overview-status-donut"), className="chart-card"),
                    html.Div(dcc.Graph(id="overview-top-vehicles"), className="chart-card"),
                    html.Div(dcc.Graph(id="overview-alarm-types"), className="chart-card"),
                ],
            ),
        ]
    )


@callback(
    Output("overview-kpis", "children"),
    Output("overview-online-pie", "figure"),
    Output("overview-status-donut", "figure"),
    Output("overview-top-vehicles", "figure"),
    Output("overview-alarm-types", "figure"),
    Input("refresh-token", "data"),
    Input("load-tick", "n_intervals"),
    Input("age-hours-threshold", "value"),
)
def _update_overview(_token, _n_load, age_hours_threshold):
    age_hours_threshold = float(age_hours_threshold or 6)
    try:
        devices, rt, alarms = _safe_load()
    except Exception as e:
        msg = html.Div(f"Failed to load data: {e}", style={"color": "#D40511"})
        return [msg], C.EMPTY_FIG, C.EMPTY_FIG, C.EMPTY_FIG, C.EMPTY_FIG

    banners: list = []
    if devices is None and rt is None and alarms is None:
        msg = html.Div(
            "Loading data from VSS — first load can take a bit. "
            "Charts will appear progressively as each dataset finishes.",
            style={"color": C.DHL_RED, "fontWeight": 600},
        )
        return [msg], C.EMPTY_FIG, C.EMPTY_FIG, C.EMPTY_FIG, C.EMPTY_FIG
    if alarms is None and (devices is not None or rt is not None):
        banners.append(
            html.Div("24h alarms still loading — charts update automatically.", style={"color": "#6B7280"})
        )
    if rt is None and devices is not None:
        banners.append(
            html.Div("Live status still loading — device list is ready.", style={"color": "#6B7280"})
        )

    total_devices = len(devices) if devices is not None else 0

    if rt is None or rt.empty:
        online = offline = unknown = 0
    else:
        import pandas as pd
        age = pd.to_numeric(rt.get("AgeHours"), errors="coerce")
        online = int((age.notna() & (age <= age_hours_threshold)).sum())
        offline = int((age.notna() & (age > age_hours_threshold)).sum())
        unknown = int(age.isna().sum())

    devices_with_alarm = 0 if alarms is None or alarms.empty else int(alarms["DeviceID"].nunique())
    total_alarms = 0 if alarms is None or alarms.empty else int(len(alarms))

    mix_kpis: list = []
    if mix_integration_enabled():
        mix_df = get_mix_health_cached()
        if mix_df is None:
            mix_kpis.append(
                C.kpi_card("MiX assets", "…", accent="#F59E0B", sub="loading — open MiX Health")
            )
        elif mix_df.empty:
            mix_kpis.append(
                C.kpi_card("MiX assets", "0", accent="#F59E0B", sub="no health data yet — MiX Health page")
            )
        else:
            flagged = int((mix_df["IssueCount"] > 0).sum()) if "IssueCount" in mix_df.columns else 0
            mix_kpis.append(
                C.kpi_card(
                    "MiX assets",
                    f"{len(mix_df):,}",
                    accent="#F59E0B",
                    sub=f"{flagged:,} with issues — see MiX Health",
                )
            )

    kpis = banners + [
        C.kpi_card("Total devices (VSS)", f"{total_devices:,}"),
        C.kpi_card("Online", f"{online:,}", accent="#2E8B57", sub=f"<= {age_hours_threshold:g}h since last status"),
        C.kpi_card("Offline", f"{offline:,}", accent=C.DHL_RED, sub=f"> {age_hours_threshold:g}h or no signal"),
        C.kpi_card("Status unknown", f"{unknown:,}", accent="#999"),
        C.kpi_card(alarms_kpi_label(), f"{total_alarms:,}", accent=C.DHL_YELLOW),
        C.kpi_card("Devices alarming", f"{devices_with_alarm:,}", accent=C.DHL_RED),
    ] + mix_kpis

    if rt is not None and not rt.empty:
        pie = C.online_offline_pie(rt, age_hours_threshold)
        donut = C.status_type_donut(rt)
    elif devices is not None:
        pie = C.loading_fig("Fetching live status for each device…")
        donut = C.loading_fig("Fetching live status for each device…")
    else:
        pie = C.EMPTY_FIG
        donut = C.EMPTY_FIG

    if alarms is not None and not alarms.empty:
        top_v = C.top_devices_by_alarms(alarms, top_n=10)
        al_pie = C.alarm_type_pie(alarms)
    elif devices is not None or rt is not None:
        top_v = C.loading_fig("Loading 24h alarm history…")
        al_pie = C.loading_fig("Loading 24h alarm history…")
    else:
        top_v = C.EMPTY_FIG
        al_pie = C.EMPTY_FIG

    return (kpis, pie, donut, top_v, al_pie)
