"""MiX Telematics — DHL asset health on MiX ZA."""

from __future__ import annotations

import dash
from dash import Input, Output, callback, dash_table, dcc, html

from data import mix_integration_enabled

dash.register_page(__name__, path="/mix", name="MiX Health", order=2)


def _setup_notice() -> html.Div:
    return html.Div(
        [
            html.P("MiX telematics is not configured yet.", style={"fontWeight": 600}),
            html.P("Set MIX_ENABLED=1 and add accounts.json, then restart the dashboard."),
        ],
        style={"background": "#FFF8E6", "border": "1px solid #FFCC00", "borderRadius": "8px", "padding": "16px"},
    )


def layout():
    from mix_health import ALL_ISSUES

    return html.Div(
        [
            html.H1("MiX Telematics", className="page-title"),
            html.Div(
                "DHL assets on MiX ZA — 7-day no-engine-RPM diagnostic events, GPS, download, and speed flags.",
                className="page-subtitle",
            ),
            html.Div(id="mix-setup-notice"),
            html.Div(
                className="filter-row",
                children=[
                    dcc.Dropdown(
                        id="mix-health-issue",
                        multi=True,
                        placeholder="Issue type (all)",
                        options=[{"label": i, "value": i} for i in ALL_ISSUES],
                    ),
                    dcc.Dropdown(id="mix-health-asset", multi=True, placeholder="Asset (all)"),
                    dcc.Dropdown(
                        id="mix-health-chart",
                        clearable=False,
                        value="issues_bar",
                        options=[
                            {"label": "Issues by type (bar)", "value": "issues_bar"},
                            {"label": "Assets with issues (count)", "value": "flagged_pie"},
                        ],
                    ),
                ],
            ),
            html.Div(id="mix-health-kpis", className="kpi-row"),
            html.Div(
                className="chart-grid",
                children=[html.Div(dcc.Graph(id="mix-health-chart-graph"), className="chart-card full")],
            ),
            html.Div(
                className="table-card",
                children=[
                    html.Div(
                        "Flagged assets — use Issue type filter (e.g. no-engine-RPM diagnostic)",
                        style={"fontWeight": 600, "marginBottom": "8px"},
                    ),
                    dash_table.DataTable(
                        id="mix-health-table",
                        page_size=15,
                        style_table={"overflowX": "auto"},
                        style_cell={
                            "padding": "6px 10px",
                            "fontFamily": "Inter, Segoe UI, Arial, sans-serif",
                            "fontSize": 12,
                            "whiteSpace": "normal",
                            "height": "auto",
                        },
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
    Output("mix-setup-notice", "children"),
    Input("load-tick", "n_intervals"),
)
def _show_setup_notice(_n):
    if mix_integration_enabled():
        return None
    return _setup_notice()
