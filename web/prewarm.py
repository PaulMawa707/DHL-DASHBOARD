"""Background VSS/MiX cache prewarm (ported from app.py)."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import nullcontext

import operation_log

from data import (
    cache_age_seconds,
    cache_get,
    load_alarms_last_24h,
    load_dhl_devices,
    load_mix_health,
    mix_integration_enabled,
    refresh_alarms_last_24h,
    refresh_dhl_devices,
    refresh_mix_health,
    refresh_realtime_status,
    load_realtime_status,
    seed_device_cache_from_snapshot,
)
from vss_client import (
    active_base_url,
    ensure_token,
    get_current_token,
    keepalive_ping,
    last_vss_error,
    last_vss_profile,
    last_vss_token_source,
    refresh_token_if_expired,
    try_token_without_login,
    validate_or_renew_token,
    vss_no_login_mode,
)

log = logging.getLogger("dhl-flask")

try:
    REFRESH_MINUTES = max(1, int(os.environ.get("DHL_AUTO_REFRESH_MINUTES", "60").strip() or "60"))
except ValueError:
    REFRESH_MINUTES = 60

KEEPALIVE_MINUTES = 20
TOKEN_CHECK_MINUTES = 30


def realtime_auto_refresh_seconds() -> int:
    """How stale realtime status may get before it is re-pulled; 0 disables it."""
    try:
        minutes = int(os.environ.get("DHL_REALTIME_AUTO_REFRESH_MINUTES", "30").strip() or "30")
    except ValueError:
        minutes = 30
    if minutes <= 0:
        return 0
    return max(60, minutes * 60)


def realtime_data_age_seconds() -> float | None:
    """Age of the realtime snapshot in seconds (None when nothing is loaded).

    Snapshots hydrated from Neon carry their original ``updated_at``, so this
    stays accurate across serverless cold starts.
    """
    return cache_age_seconds("realtime_status")


_prewarm_mix_lock = threading.Lock()
_prewarm_mix_running = False
_prewarm_mix_pending = False
_prewarm_vss_lock = threading.Lock()
_prewarm_vss_running = False
_prewarm_vss_pending = False


def _prewarm_mix(*, stale_while_revalidate: bool) -> None:
    if not mix_integration_enabled():
        return
    load_fn = refresh_mix_health if stale_while_revalidate else load_mix_health
    label = "background refresh" if stale_while_revalidate else "startup"
    try:
        log.info("prewarm: mix_health starting (%s)", label)
        operation_log.log_event("mix_data", "fetch_mix_health", "running", f"MiX health fetch starting ({label})")
        load_fn()
        log.info("prewarm: mix_health done")
        operation_log.log_event("mix_data", "fetch_mix_health", "ok", f"MiX health fetch complete ({label})")
    except Exception as e:  # noqa: BLE001
        log.warning("prewarm: mix_health failed: %s", e)
        operation_log.log_event("mix_data", "fetch_mix_health", "error", f"MiX health fetch failed: {e}")


def _prewarm_vss(*, keep_devices: bool, stale_while_revalidate: bool) -> None:
    token_ctx = vss_no_login_mode() if stale_while_revalidate else nullcontext()
    with token_ctx:
        if stale_while_revalidate:
            cached = get_current_token() or try_token_without_login()
            if not cached:
                log.warning("prewarm: refresh — no in-memory VSS token; skipping VSS reload")
                return
            token, _pid = cached
            log.info("prewarm: VSS token %s... (reuse in-memory)", token[:12])
        else:
            try:
                token, _pid = ensure_token(login_max_wait_seconds=120, allow_10082_retry=True)
                log.info(
                    "prewarm: VSS token %s... (%s) profile=%s base=%s",
                    token[:12],
                    last_vss_token_source() or "unknown",
                    last_vss_profile() or "?",
                    active_base_url(),
                )
            except Exception as e:  # noqa: BLE001
                err = last_vss_error() or str(e)
                log.warning("prewarm: VSS token unavailable: %s", err)
                return

        ok, msg = validate_or_renew_token(allow_reauth=not stale_while_revalidate)
        if not ok:
            log.warning("prewarm: VSS token check: %s", msg)
            return
        else:
            log.info("prewarm: VSS token validated")

        load_devices = refresh_dhl_devices if stale_while_revalidate else load_dhl_devices
        load_realtime = refresh_realtime_status if stale_while_revalidate else load_realtime_status
        load_alarms = refresh_alarms_last_24h if stale_while_revalidate else load_alarms_last_24h

        need_devices = cache_get("dhl_devices") is None or (stale_while_revalidate and not keep_devices)
        if need_devices:
            try:
                log.info("prewarm: dhl_devices starting")
                operation_log.log_event("vss_data", "fetch_dhl_devices", "running", "VSS device list fetch starting")
                load_devices()
                log.info("prewarm: dhl_devices done")
                operation_log.log_event("vss_data", "fetch_dhl_devices", "ok", "VSS device list fetch complete")
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: dhl_devices failed: %s", e)
                operation_log.log_event("vss_data", "fetch_dhl_devices", "error", f"VSS device list failed: {e}")
        else:
            log.info("prewarm: dhl_devices already cached")

        for name, fn, cache_key in (
            ("realtime_status", load_realtime, "fetch_realtime"),
            ("alarms_last_24h", load_alarms, "fetch_alarms"),
        ):
            try:
                log.info("prewarm: %s starting", name)
                operation_log.log_event("vss_data", cache_key, "running", f"VSS {name} fetch starting")
                fn()
                log.info("prewarm: %s done", name)
                operation_log.log_event("vss_data", cache_key, "ok", f"VSS {name} fetch complete")
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: %s failed: %s", name, e)
                operation_log.log_event("vss_data", cache_key, "error", f"VSS {name} failed: {e}")


def _kick_mix_prewarm(*, stale_while_revalidate: bool = False) -> None:
    global _prewarm_mix_running, _prewarm_mix_pending

    with _prewarm_mix_lock:
        if _prewarm_mix_running:
            _prewarm_mix_pending = True
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
    global _prewarm_vss_running, _prewarm_vss_pending

    with _prewarm_vss_lock:
        if _prewarm_vss_running:
            _prewarm_vss_pending = True
            return
        _prewarm_vss_running = True

    def _runner() -> None:
        global _prewarm_vss_running, _prewarm_vss_pending
        pending = False
        kd = keep_devices
        swr = stale_while_revalidate
        try:
            _prewarm_vss(keep_devices=kd, stale_while_revalidate=swr)
        finally:
            with _prewarm_vss_lock:
                _prewarm_vss_running = False
                pending = _prewarm_vss_pending
                _prewarm_vss_pending = False
            if pending:
                _kick_vss_prewarm(keep_devices=kd, stale_while_revalidate=swr)

    threading.Thread(target=_runner, daemon=True, name="dhl-prewarm-vss").start()


def prewarm_cache(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    _kick_mix_prewarm(stale_while_revalidate=stale_while_revalidate)
    _kick_vss_prewarm(keep_devices=keep_devices, stale_while_revalidate=stale_while_revalidate)


def prewarm_cache_sync(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    """Run prewarm in the current thread (manual Refresh data only)."""
    _prewarm_mix(stale_while_revalidate=stale_while_revalidate)
    _prewarm_vss(keep_devices=keep_devices, stale_while_revalidate=stale_while_revalidate)


_auto_realtime_lock = threading.Lock()


def auto_refresh_realtime(*, force: bool = False) -> dict[str, object]:
    """Re-pull realtime status when it is stale, reusing the stored VSS token.

    Browser polls never call apiLogin (VSS throttles with 10082). The forced
    cron path may log in once when the stored token is missing or expired, using
    VSS credentials already on Vercel, then save the new token to Neon.
    """
    interval = realtime_auto_refresh_seconds()
    if not interval and not force:
        return {"refreshed": False, "reason": "disabled"}

    age = realtime_data_age_seconds()
    if not force and age is not None and age < interval:
        return {"refreshed": False, "reason": "fresh", "age_seconds": int(age)}

    if not _auto_realtime_lock.acquire(blocking=False):
        return {"refreshed": False, "reason": "busy", "age_seconds": int(age) if age is not None else None}

    try:
        token_ok = False
        with vss_no_login_mode():
            if try_token_without_login():
                ok, msg = validate_or_renew_token(allow_reauth=False)
                if ok:
                    token_ok = True
                else:
                    log.info("auto-refresh: stored VSS token unusable (%s)", msg)
                    if not force:
                        return {"refreshed": False, "reason": "token-unusable", "message": msg}
            elif not force:
                log.info("auto-refresh: no stored VSS token — keeping cached realtime status")
                return {"refreshed": False, "reason": "no-token"}

        if not token_ok:
            log.info("auto-refresh: cron re-login via VSS credentials")
            try:
                ensure_token(
                    force=True,
                    skip_file=True,
                    login_max_wait_seconds=120,
                    allow_10082_retry=True,
                )
            except Exception as login_exc:  # noqa: BLE001
                log.warning("auto-refresh: VSS re-login failed: %s", login_exc)
                return {"refreshed": False, "reason": "login-failed", "message": str(login_exc)}
            ok, msg = validate_or_renew_token(allow_reauth=False)
            if not ok:
                return {"refreshed": False, "reason": "token-unusable", "message": msg}

        with vss_no_login_mode():
            if cache_get("dhl_devices") is None:
                try:
                    from data import hydrate_missing_snapshots_from_neon

                    hydrate_missing_snapshots_from_neon(
                        exclude_keys={"realtime_status", "alarms_24h", "mix_health"}
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-refresh: device hydrate failed: %s", exc)

            operation_log.log_event(
                "vss_data",
                "fetch_realtime",
                "running",
                f"Auto-refresh: realtime status ({interval // 60} min, stored token)",
            )
            rows = len(refresh_realtime_status())
            operation_log.log_event(
                "vss_data",
                "fetch_realtime",
                "ok",
                f"Auto-refresh: realtime status complete ({rows} devices)",
            )
            log.info("auto-refresh: realtime status updated (%s devices)", rows)
            try:
                import neon_meta_store

                neon_meta_store.record_last_auto_refresh(rows=rows, source="auto")
            except Exception as meta_exc:  # noqa: BLE001
                log.debug("auto-refresh: could not store last time: %s", meta_exc)
            return {"refreshed": True, "rows": rows, "age_seconds": 0}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        log.warning("auto-refresh: realtime status failed: %s", msg)
        operation_log.log_event("vss_data", "fetch_realtime", "error", f"Auto-refresh failed: {msg}")
        return {"refreshed": False, "reason": "error", "message": msg}
    finally:
        _auto_realtime_lock.release()


_local_cron_started = False


def _start_local_realtime_cron() -> None:
    """In-process 30-minute timer for local Flask. Vercel uses vercel.json crons."""
    global _local_cron_started
    if os.environ.get("VERCEL", "").strip():
        return
    interval = realtime_auto_refresh_seconds()
    if interval <= 0 or _local_cron_started:
        return
    _local_cron_started = True

    def _loop() -> None:
        log.info("local realtime cron: every %s minutes", max(1, interval // 60))
        while True:
            time.sleep(interval)
            try:
                result = auto_refresh_realtime(force=True)
                log.info(
                    "local realtime cron: refreshed=%s reason=%s",
                    result.get("refreshed"),
                    result.get("reason"),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("local realtime cron failed: %s", exc)

    threading.Thread(target=_loop, name="dhl-realtime-cron", daemon=True).start()


def start_background_workers() -> None:
    """Startup: hydrate display cache from Neon only — no VSS/MiX API calls."""
    try:
        from data import cache_needs_hydration, hydrate_cache_from_neon

        if cache_needs_hydration():
            hydrate_cache_from_neon()
            log.info("startup: dashboard cache hydrated from Neon")
    except Exception as e:  # noqa: BLE001
        log.warning("startup Neon hydrate skipped: %s", e)
    _start_local_realtime_cron()
