"""DHL Fleet Health Dashboard (Plotly Dash, multi-page)."""

from __future__ import annotations

import logging
import os


def _load_env_file(path: str) -> None:
    """Load KEY=value lines from a .env file (stdlib only; no python-dotenv).

    Values from this file **override** the same keys already in the process environment
    so a stale ``$env:DHL_FAST_MODE=1`` in PowerShell cannot ignore your project ``.env``.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import threading
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from dash import Dash, Input, Output, State, dcc, html, page_container, page_registry

from data import (
    _env_truthy,
    alarm_query_hours,
    bust_cache_for_refresh,
    cache_freshness,
    cache_get,
    cache_latest_data_iso,
    clear_all_dashboard_caches,
    fast_mode,
    invalidate_stale_mix_caches,
    last_bust_cache_iso,
    load_alarms_last_24h,
    load_dhl_devices,
    load_mix_positions,
    load_mix_health,
    load_realtime_status,
    mix_integration_enabled,
    refresh_alarms_last_24h,
    refresh_dhl_devices,
    refresh_mix_positions,
    refresh_mix_health,
    refresh_realtime_status,
)
from mix_ui_callbacks import register_mix_callbacks
from vss_client import (
    ensure_token,
    get_current_token,
    keepalive_ping,
    try_token_without_login,
    validate_or_renew_token,
    vss_no_login_mode,
)

# Bump when MiX/VSS refresh behaviour changes (shown in sidebar to confirm server restart).
APP_BUILD = "mix-health-2026-06b"

# Shown in the sidebar when VSS/bootstrap fails (so demos are not a silent blank screen).
_LAST_VSS_ERROR: str | None = None


def _set_vss_error(msg: str) -> None:
    global _LAST_VSS_ERROR
    _LAST_VSS_ERROR = (msg or "").strip() or None


def _clear_vss_error_if_devices_loaded() -> None:
    global _LAST_VSS_ERROR
    if cache_get("dhl_devices") is not None:
        _LAST_VSS_ERROR = None


def _sync_bootstrap_devices() -> None:
    """Optionally block until the device list is cached before the HTTP server starts.

    **Default is off** — Dash binds immediately and data loads in the background (prewarm).
    Set ``DHL_SYNC_BOOTSTRAP=1`` only if you want to wait for devices first (e.g. a live demo).
    """
    global _LAST_VSS_ERROR
    v = os.environ.get("DHL_SYNC_BOOTSTRAP", "0").strip().lower()
    if v in ("0", "false", "no", "off", ""):
        log.info(
            "bootstrap: non-blocking — open the UI now; device list and charts fill via background prewarm "
            "(set DHL_SYNC_BOOTSTRAP=1 to wait for devices before the server listens)."
        )
        return
    try:
        ensure_token()
        try:
            timeout = float(os.environ.get("DHL_BOOTSTRAP_TIMEOUT_SEC", "120"))
        except ValueError:
            timeout = 120.0
        log.info(
            "bootstrap: sync wait for device list (max %.0fs) — DHL_SYNC_BOOTSTRAP=1; "
            "then Dash will print its URL.",
            timeout,
        )
        if timeout <= 0:
            load_dhl_devices()
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(load_dhl_devices)
                try:
                    fut.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    log.warning(
                        "bootstrap: device list still running after %ss — server will start; "
                        "data will appear when the background load finishes (check logs / sidebar).",
                        int(timeout),
                    )
                    _set_vss_error(
                        f"Device list is still loading (>{int(timeout)}s). "
                        "The app is up — wait for sidebar timestamps or click Refresh data."
                    )
        df = cache_get("dhl_devices")
        n = len(df) if df is not None else 0
        if n:
            log.info("bootstrap: device list ready (%s devices)", n)
            _LAST_VSS_ERROR = None
        elif timeout > 0:
            log.info("bootstrap: device count not available yet (load may still be running)")
    except Exception as e:  # noqa: BLE001
        msg = f"VSS error (device list): {e}"
        log.error("bootstrap: %s", msg)
        _set_vss_error(msg)


def _sync_bootstrap_mix() -> None:
    """Block briefly so MiX positions are cached before the first /mix page render."""
    if not mix_integration_enabled():
        return
    v = os.environ.get("MIX_SYNC_BOOTSTRAP", "0").strip().lower()
    if v in ("0", "false", "no", "off"):
        log.info(
            "bootstrap-mix: non-blocking — MiX fills via background prewarm "
            "(set MIX_SYNC_BOOTSTRAP=1 to wait for positions before the UI loads)."
        )
        return
    try:
        timeout = float(os.environ.get("MIX_BOOTSTRAP_TIMEOUT_SEC", "90"))
    except ValueError:
        timeout = 90.0
    log.info("bootstrap-mix: waiting for MiX positions (max %.0fs)", timeout)
    try:
        if timeout <= 0:
            df = load_mix_positions()
            log.info("bootstrap-mix: %s positions cached", len(df))
            return
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(load_mix_positions)
            try:
                df = fut.result(timeout=timeout)
                named = 0
                if "AssetName" in df.columns:
                    named = int(df["AssetName"].astype(str).str.strip().ne("").sum())
                log.info("bootstrap-mix: %s positions cached (%s with names)", len(df), named)
            except concurrent.futures.TimeoutError:
                log.warning(
                    "bootstrap-mix: still loading after %ss — open MiX Positions; data appears when ready",
                    int(timeout),
                )
    except Exception as e:  # noqa: BLE001
        log.warning("bootstrap-mix: %s — server will start; retry via Refresh data", e)

try:
    REFRESH_MINUTES = max(1, int(os.environ.get("DHL_AUTO_REFRESH_MINUTES", "60").strip() or "60"))
except ValueError:
    REFRESH_MINUTES = 60
KEEPALIVE_MINUTES = 20
# Local `python app.py` default (Dash convention); override with DHL_DASH_PORT.
DEFAULT_DASH_PORT = 8050

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dhl-dashboard")

if _env_truthy("DHL_CLEAR_CACHE_ON_START"):
    clear_all_dashboard_caches(include_disk_catalog=True)
    log.info("startup: cleared all dashboard caches (DHL_CLEAR_CACHE_ON_START=1)")
else:
    invalidate_stale_mix_caches()

# Flask/Werkzeug logs every Dash poll as "POST /_dash-update-component" — very noisy.
# Set DHL_DASH_ACCESS_LOG=1 to restore INFO-level HTTP access lines.
if os.environ.get("DHL_DASH_ACCESS_LOG", "0").strip().lower() not in ("1", "true", "yes", "on"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

if fast_mode():
    log.info(
        "Fast path (default): alarm_query_hours=%s — set DHL_FAST_MODE=0 for full crawl + 24h alarms.",
        alarm_query_hours(),
    )
else:
    log.info(
        "DHL_FAST_MODE=0 — full fleet device discovery + 24h alarms (slower; use .vss_token.txt to avoid 10082)."
    )

if mix_integration_enabled():
    from mix_client import mix_config_summary

    log.info("MiX integration enabled (%s)", mix_config_summary())
else:
    log.info("MiX integration disabled — set MIX_ENABLED=1 and add accounts.json to enable MiX Health page.")

_prewarm_mix_lock = threading.Lock()
_prewarm_mix_running = False
_prewarm_mix_pending = False
_prewarm_vss_lock = threading.Lock()
_prewarm_vss_running = False
_prewarm_vss_pending = False

# Re-check caches often so charts appear soon after VSS returns (lower = snappier UI).
UI_POLL_MS = max(250, int(os.environ.get("DHL_UI_POLL_MS", "1000")))
# -1 = unlimited (Dash default): keep polling so charts update when VSS finishes after many minutes.
try:
    UI_POLL_MAX_TICKS = int(os.environ.get("DHL_UI_POLL_MAX_TICKS", "-1").strip())
except ValueError:
    UI_POLL_MAX_TICKS = -1
if UI_POLL_MAX_TICKS == 0:
    UI_POLL_MAX_TICKS = -1


def _prewarm_mix(*, stale_while_revalidate: bool) -> None:
    """Load MiX asset health (positions tab removed — health-only prewarm)."""
    if not mix_integration_enabled():
        return
    load_mix_health_fn = refresh_mix_health if stale_while_revalidate else load_mix_health
    label = "background refresh" if stale_while_revalidate else "startup"
    try:
        log.info("prewarm: mix_health starting (%s)", label)
        load_mix_health_fn()
        log.info("prewarm: mix_health done")
    except Exception as e:  # noqa: BLE001
        log.warning("prewarm: mix_health failed: %s", e)


def _prewarm_vss(*, keep_devices: bool, stale_while_revalidate: bool) -> None:
    """Load VSS-backed caches (devices → realtime → alarms)."""
    token_ctx = vss_no_login_mode() if stale_while_revalidate else nullcontext()
    with token_ctx:
        if stale_while_revalidate:
            cached = get_current_token() or try_token_without_login()
            if not cached:
                log.warning(
                    "prewarm: refresh — no in-memory VSS token; skipping VSS reload (no login)"
                )
                return
            token, _pid = cached
            log.info("prewarm: VSS token %s... (reuse in-memory, no login)", token[:12])
        else:
            cached = try_token_without_login()
            try:
                if cached:
                    token, _pid = cached
                    log.info("prewarm: VSS token %s... (file/env, no login)", token[:12])
                else:
                    token, _pid = ensure_token(login_max_wait_seconds=45)
                    log.info("prewarm: VSS token %s... (login)", token[:12])
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: VSS token unavailable: %s — skipping VSS realtime/alarms", e)
                _set_vss_error(f"VSS error (token): {e}")
                need_devices = cache_get("dhl_devices") is None
                if need_devices:
                    try:
                        log.info("prewarm: dhl_devices starting (snapshot/offline path)")
                        load_dhl_devices()
                        log.info("prewarm: dhl_devices done")
                    except Exception as e2:  # noqa: BLE001
                        log.warning("prewarm: dhl_devices failed: %s", e2)
                _clear_vss_error_if_devices_loaded()
                return

        ok, msg = validate_or_renew_token(allow_reauth=not stale_while_revalidate)
        if not ok:
            log.warning("prewarm: VSS token check: %s — continuing with data loads anyway", msg)
        else:
            log.info("prewarm: VSS token validated")

        load_devices = refresh_dhl_devices if stale_while_revalidate else load_dhl_devices
        load_realtime = refresh_realtime_status if stale_while_revalidate else load_realtime_status
        load_alarms = refresh_alarms_last_24h if stale_while_revalidate else load_alarms_last_24h

        need_devices = cache_get("dhl_devices") is None or (
            stale_while_revalidate and not keep_devices
        )
        if need_devices:
            try:
                log.info("prewarm: dhl_devices starting")
                load_devices()
                log.info("prewarm: dhl_devices done")
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: dhl_devices failed: %s", e)
                _set_vss_error(f"VSS error (devices): {e}")
        else:
            log.info("prewarm: dhl_devices already cached, skipping")

        login_throttled = False
        for name, fn in (
            ("realtime_status", load_realtime),
            ("alarms_last_24h", load_alarms),
        ):
            if login_throttled and name == "alarms_last_24h":
                log.warning("prewarm: skipping %s — login still rate-limited (10082)", name)
                continue
            try:
                log.info("prewarm: %s starting", name)
                fn()
                log.info("prewarm: %s done", name)
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: %s failed: %s", name, e)
                _set_vss_error(f"VSS error ({name}): {e}")
                if "10082" in str(e):
                    login_throttled = True
        _clear_vss_error_if_devices_loaded()


def _kick_mix_prewarm(*, stale_while_revalidate: bool = False) -> None:
    """Start MiX cache load in its own thread (never blocked by VSS)."""
    global _prewarm_mix_running, _prewarm_mix_pending

    with _prewarm_mix_lock:
        if _prewarm_mix_running:
            _prewarm_mix_pending = True
            log.info("prewarm-mix: already running — queued another run")
            return
        _prewarm_mix_running = True

    def _runner() -> None:
        global _prewarm_mix_running, _prewarm_mix_pending
        pending = False
        swr = stale_while_revalidate
        try:
            _prewarm_mix(stale_while_revalidate=swr)
        finally:
            with _prewarm_mix_lock:
                _prewarm_mix_running = False
                pending = _prewarm_mix_pending
                _prewarm_mix_pending = False
            if pending:
                _kick_mix_prewarm(stale_while_revalidate=swr)

    threading.Thread(target=_runner, daemon=True, name="dhl-prewarm-mix").start()


def _kick_vss_prewarm(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    """Start VSS cache load in its own thread (independent of MiX)."""
    global _prewarm_vss_running, _prewarm_vss_pending

    with _prewarm_vss_lock:
        if _prewarm_vss_running:
            _prewarm_vss_pending = True
            log.info("prewarm-vss: already running — queued another run")
            return
        _prewarm_vss_running = True

    def _runner() -> None:
        global _prewarm_vss_running, _prewarm_vss_pending
        pending = False
        kd = keep_devices
        swr = stale_while_revalidate
        try:
            if swr:
                log.info("prewarm-vss: starting [background refresh]")
            else:
                log.info("prewarm-vss: starting")
            _prewarm_vss(keep_devices=kd, stale_while_revalidate=swr)
            log.info("prewarm-vss: finished")
        finally:
            with _prewarm_vss_lock:
                _prewarm_vss_running = False
                pending = _prewarm_vss_pending
                _prewarm_vss_pending = False
            if pending:
                _kick_vss_prewarm(keep_devices=kd, stale_while_revalidate=swr)

    threading.Thread(target=_runner, daemon=True, name="dhl-prewarm-vss").start()


def _prewarm_cache(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    """Populate caches in the background.

    MiX and VSS run on **separate threads** so MiX positions appear even when VSS
    is slow, unreachable, or rate-limited.
    """
    _kick_mix_prewarm(stale_while_revalidate=stale_while_revalidate)
    _kick_vss_prewarm(keep_devices=keep_devices, stale_while_revalidate=stale_while_revalidate)


def _keepalive_loop() -> None:
    """Reset the VSS inactivity timer every KEEPALIVE_MINUTES.

    Without this, the token could expire during long gaps between data
    auto-refreshes and we would have to re-login (which can hit 10082). With it,
    the same token stays valid as long as the dashboard process is running.
    """
    interval = max(60, KEEPALIVE_MINUTES * 60)
    while True:
        try:
            time.sleep(interval)
            ok = keepalive_ping(allow_reauth=False)
            tok = get_current_token()
            tok_str = (tok[0][:12] + "...") if tok else "(no token)"
            log.info("keepalive: token %s alive=%s", tok_str, ok)
        except Exception as e:  # noqa: BLE001
            log.warning("keepalive: %s", e)


_sync_bootstrap_devices()
_sync_bootstrap_mix()
_prewarm_cache()
threading.Thread(target=_keepalive_loop, daemon=True, name="dhl-keepalive").start()

app = Dash(
    __name__,
    use_pages=True,
    suppress_callback_exceptions=True,
    title="DHL Fleet Health",
)
server = app.server


def _nav_link(name: str, href: str) -> html.A:
    cls = "nav-link nav-link-mix" if href == "/mix" else "nav-link"
    return html.A(name, href=href, className=cls)


def _mix_page() -> dict | None:
    return page_registry.get("pages.mix") or page_registry.get("mix")


def _sidebar_children() -> list:
    pages = sorted(page_registry.values(), key=lambda p: p.get("order", 0))
    vss_pages = [p for p in pages if p.get("path") != "/mix"]
    mix_page = _mix_page()
    mix_on = mix_page is not None

    children: list = [
        html.Div("DHL Fleet Health", className="brand"),
        html.Div(
            "VSS telematics + MiX asset health" if mix_on else "Real-time vehicle telematics",
            className="brand-sub",
        ),
        html.Hr(),
        html.Div("VSS fleet", className="nav-section-label"),
        html.Nav(className="nav", children=[_nav_link(p["name"], p["path"]) for p in vss_pages]),
    ]
    if mix_on:
        children.extend(
            [
                html.Div("MiX telematics (ZA)", className="nav-section-label"),
                html.Nav(
                    className="nav",
                    children=[_nav_link(mix_page["name"], mix_page["path"])],
                ),
            ]
        )
    children.extend(
        [
            html.Hr(),
            html.Div(
                className="control-block",
                children=[
                    html.Label("Online threshold (hours)", className="control-label"),
                    dcc.Slider(
                        id="age-hours-threshold",
                        min=1,
                        max=72,
                        step=1,
                        value=6,
                        marks={1: "1h", 6: "6h", 12: "12h", 24: "24h", 48: "48h", 72: "72h"},
                        tooltip={"placement": "bottom", "always_visible": False},
                    ),
                    html.Div(
                        "Devices with last status within this many hours are 'Online'.",
                        className="control-hint",
                    ),
                ],
            ),
            html.Div(
                className="control-block",
                children=[
                    html.Button("Refresh data", id="refresh-btn", n_clicks=0, className="refresh-btn"),
                    html.Div(id="refresh-info", className="refresh-info"),
                ],
            ),
        ]
    )
    return children


sidebar = html.Div(className="sidebar", children=_sidebar_children())


main = html.Div(
    className="main",
    children=[
        html.Div(id="page-banner", className="page-banner"),
        page_container,
    ],
)


app.layout = html.Div(
    className="app-shell",
    children=[
        sidebar,
        main,
        dcc.Interval(id="auto-refresh", interval=REFRESH_MINUTES * 60 * 1000, n_intervals=0),
        dcc.Interval(
            id="load-tick",
            interval=max(500, UI_POLL_MS),
            n_intervals=0,
            max_intervals=UI_POLL_MAX_TICKS,
        ),
        dcc.Store(id="refresh-token", data=0),
    ],
)


register_mix_callbacks(app)


def _freshness_sidebar() -> html.Div:
    fresh = cache_freshness()
    _clear_vss_error_if_devices_loaded()
    lines: list = []
    if _LAST_VSS_ERROR:
        lines.append(
            html.Div(
                _LAST_VSS_ERROR,
                className="freshness-line",
                style={"color": "#D40511", "fontWeight": 600, "marginBottom": "8px"},
            )
        )
    bust_iso = last_bust_cache_iso()
    latest_iso = cache_latest_data_iso()
    lines.extend(
        [
            html.Div(
                f"Latest data loaded: {latest_iso or 'not yet'}",
                className="freshness-line",
            ),
            html.Div(
                f"Last refresh (cache cleared): {bust_iso or '— (startup only until first refresh)'}",
                className="freshness-line",
            ),
            html.Div(f"DHL devices: {fresh['dhl_devices']}", className="freshness-line"),
            html.Div(f"Realtime: {fresh['realtime_status']}", className="freshness-line"),
            html.Div(f"Alarms 24h: {fresh['alarms_24h']}", className="freshness-line"),
            html.Div(f"MiX health: {fresh.get('mix_health', 'not loaded')}", className="freshness-line"),
            html.Div(f"Build: {APP_BUILD}", className="freshness-line", style={"opacity": 0.75}),
            html.Div(
                (
                    "Auto-refresh every 1 hour"
                    if REFRESH_MINUTES == 60
                    else (
                        f"Auto-refresh every {REFRESH_MINUTES // 60} hours"
                        if REFRESH_MINUTES % 60 == 0
                        else f"Auto-refresh every {REFRESH_MINUTES} min"
                    )
                ),
                className="freshness-hint",
            ),
        ]
    )
    return html.Div(lines)


@app.callback(
    Output("refresh-token", "data"),
    Input("refresh-btn", "n_clicks"),
    Input("auto-refresh", "n_intervals"),
    State("refresh-token", "data"),
    prevent_initial_call=False,
)
def _bump_refresh_token(n_clicks: int, _n_intervals: int, token: int):
    from dash import ctx

    keep_devices = _env_truthy("DHL_DEVICES_FROM_SNAPSHOT")
    if ctx.triggered_id == "refresh-btn" and n_clicks:
        invalidate_stale_mix_caches()
        bust_cache_for_refresh(
            keep_devices=keep_devices,
            clear_mix_disk_catalog=_env_truthy("DHL_FULL_CACHE_CLEAR"),
        )
        _prewarm_cache(keep_devices=keep_devices, stale_while_revalidate=True)
    elif ctx.triggered_id == "auto-refresh" and _n_intervals:
        _prewarm_cache(keep_devices=keep_devices, stale_while_revalidate=True)
    return (token or 0) + 1


@app.callback(
    Output("refresh-info", "children"),
    Input("refresh-btn", "n_clicks"),
    Input("auto-refresh", "n_intervals"),
    Input("load-tick", "n_intervals"),
    prevent_initial_call=False,
)
def _refresh_info_sidebar(_n_clicks: int, _n_intervals: int, _load_tick: int):
    return _freshness_sidebar()


if __name__ == "__main__":
    # Reloader is OFF by default: it restarts the Python process on every file
    # save, which kills any prewarm cycle in flight and forces it to start over.
    # If you actually want hot reload while developing, set DHL_DASH_DEBUG=1.
    debug = os.environ.get("DHL_DASH_DEBUG", "0") == "1"
    host = os.environ.get("DHL_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DHL_DASH_PORT", str(DEFAULT_DASH_PORT)))
    log.info("Dash UI: http://%s:%s/ (set DHL_DASH_HOST / DHL_DASH_PORT to change)", host, port)
    app.run(
        debug=debug,
        use_reloader=debug,
        host=host,
        port=port,
    )
