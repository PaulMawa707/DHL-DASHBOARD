"""MiX page callbacks registered on the main Dash app (not the pages module)."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
from dash import Input, Output, html

import components as C
from data import get_mix_asset_dropdown_options, get_mix_health_cached, mix_integration_enabled
from mix_health import (
    ALL_ISSUES,
    ISSUE_NON_DOWNLOADING,
    ISSUE_NO_GPS,
    ISSUE_NO_RPM,
    ISSUE_SPEED_SPIKE,
    order_health_table_columns,
)


def _filter_health(df: pd.DataFrame, issues, assets) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if assets:
        out = out[out["AssetName"].astype(str).isin(assets) | out["Registration"].astype(str).isin(assets)]
    if issues:
        mask = pd.Series(False, index=out.index)
        for issue in issues:
            if issue == ISSUE_NO_RPM and "RpmFault7d" in out.columns:
                mask = mask | out["RpmFault7d"].fillna(False).astype(bool)
            else:
                mask = mask | out["Issues"].astype(str).str.contains(str(issue), regex=False, na=False)
        out = out[mask]
    return out.sort_values(["IssueCount", "AgeHours"], ascending=[False, False])


def _health_table_columns(df: pd.DataFrame) -> list[dict]:
    labels = {
        "RpmFault7d": "No-engine-RPM fault (7d)",
        "RpmFaultCount7d": "Fault events (7d)",
        "LastRpmFaultTime": "Last fault time",
        "TachoRpmF2": "Tacho RPM F2 (live)",
        "MaxTachoRpmF2": "Max tacho RPM F2",
        "TachoRpmStdDev": "Tacho RPM std dev",
    }
    return [{"name": labels.get(c, c), "id": c} for c in df.columns]


def register_mix_callbacks(app) -> None:
    @app.callback(
        Output("mix-health-asset", "options"),
        Input("load-tick", "n_intervals"),
        Input("refresh-token", "data"),
        prevent_initial_call=False,
    )
    def _populate_health_assets(_tick, _token):
        return get_mix_asset_dropdown_options()

    @app.callback(
        Output("mix-health-kpis", "children"),
        Output("mix-health-chart-graph", "figure"),
        Output("mix-health-table", "data"),
        Output("mix-health-table", "columns"),
        Input("load-tick", "n_intervals"),
        Input("refresh-token", "data"),
        Input("mix-health-issue", "value"),
        Input("mix-health-asset", "value"),
        Input("mix-health-chart", "value"),
        prevent_initial_call=False,
    )
    def _update_health(_tick, _token, issues, assets, which_chart):
        if not mix_integration_enabled():
            return [], C.EMPTY_FIG, [], []

        df = get_mix_health_cached()
        if df is None:
            return (
                [
                    html.Div(
                        "Analysing MiX asset health — first run can take a few minutes for all DHL assets.",
                        style={"color": "#6B7280", "fontWeight": 600},
                    )
                ],
                C.loading_fig("Running MiX health checks…"),
                [],
                [],
            )
        if df.empty:
            return ([html.Div("No health data.", style={"color": "#D40511"})], C.EMPTY_FIG, [], [])

        view = _filter_health(df, issues, assets)
        flagged = view[view["IssueCount"] > 0] if not issues else view

        issue_counts = {name: 0 for name in ALL_ISSUES}
        for blob in flagged.get("Issues", pd.Series(dtype=str)).fillna(""):
            for name in ALL_ISSUES:
                if name == ISSUE_NO_RPM:
                    continue
                if name in str(blob):
                    issue_counts[name] += 1
        if "RpmFault7d" in df.columns:
            issue_counts[ISSUE_NO_RPM] = int(df["RpmFault7d"].fillna(False).astype(bool).sum())

        kpis = [
            C.kpi_card("DHL assets", f"{len(df):,}"),
            C.kpi_card("With issues", f"{int((df['IssueCount'] > 0).sum()):,}", accent=C.DHL_RED),
            C.kpi_card(ISSUE_NON_DOWNLOADING, f"{issue_counts[ISSUE_NON_DOWNLOADING]:,}", accent="#B45309"),
            C.kpi_card(ISSUE_NO_GPS, f"{issue_counts[ISSUE_NO_GPS]:,}", accent="#7C3AED"),
            C.kpi_card(ISSUE_SPEED_SPIKE, f"{issue_counts[ISSUE_SPEED_SPIKE]:,}", accent=C.DHL_YELLOW),
            C.kpi_card(
                "No-engine-RPM faults (7d)",
                f"{issue_counts[ISSUE_NO_RPM]:,}",
                accent="#6B7280",
                sub="MiX diagnostic events",
            ),
        ]

        if which_chart == "flagged_pie":
            clean = int((df["IssueCount"] == 0).sum())
            bad = len(df) - clean
            pie_df = pd.DataFrame({"Status": ["With issues", "OK"], "Count": [bad, clean]})
            chart = px.pie(pie_df, names="Status", values="Count", color_discrete_sequence=[C.DHL_RED, "#2E8B57"])
            chart.update_layout(**C.DEFAULT_LAYOUT, title="Assets with health issues")
        else:
            bar_df = pd.DataFrame({"Issue": list(issue_counts.keys()), "Assets": list(issue_counts.values())})
            bar_df = bar_df[bar_df["Assets"] > 0]
            chart = (
                px.bar(bar_df, x="Issue", y="Assets", color_discrete_sequence=[C.DHL_RED])
                if not bar_df.empty
                else C.EMPTY_FIG
            )
            if not bar_df.empty:
                chart.update_layout(**C.DEFAULT_LAYOUT, title="Assets by issue type", xaxis_title="")

        table_df = flagged if not issues else view
        table_df = table_df[table_df["IssueCount"] > 0] if not issues else table_df
        table_df = order_health_table_columns(table_df)
        return kpis, chart, table_df.to_dict("records"), _health_table_columns(table_df)
