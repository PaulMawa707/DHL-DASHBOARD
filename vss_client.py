"""VSS API client (lifted from positions.ipynb cell 0).

Centralizes:
- Login + token caching (handles 10082 "login too frequently")
- POST helpers with retry/backoff (10129 "too frequent" handling)
- Endpoints used by the dashboard: fleets, devices, realtime status, alarm list, lang dict.

Credentials are read from environment variables; see .env.example / README.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


BASE_URL: str = _env("VSS_BASE_URL", "http://40.76.130.233:9966")
USERNAME: str = _env("VSS_USERNAME", "mawa@controltech-ea.com")
PASSWORD_PLAINTEXT: str = _env("VSS_PASSWORD", "Kenya+123")

_TOKEN_FILE = Path(__file__).resolve().parent / ".vss_token.txt"

_vss_log = logging.getLogger("vss_client")

_POOL_MAXSIZE: int = int(_env("VSS_POOL_MAXSIZE", "50") or "50")

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})
_session.mount("http://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))
_session.mount("https://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))

_lock = threading.Lock()
_VSS_TOKEN: str | None = None
_VSS_PID: str | None = None
_VSS_TOKEN_AT: datetime | None = None
# Set inside ``ensure_token`` for debugging: memory / env / file / login.
_LAST_TOKEN_SOURCE: str | None = None
# Mtime of ``.vss_token.txt`` when the in-memory token was last aligned with that file
# (used to pick up hand-edited tokens without restarting the process).
_FILE_TOKEN_MTIME: float | None = None


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _reset_session() -> None:
    global _session
    try:
        _session.close()
    except Exception:
        pass
    _session = requests.Session()
    _session.headers.update({"Content-Type": "application/json"})
    _session.mount("http://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))
    _session.mount("https://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))


def vss_post_raw(path: str, payload: dict, timeout: int = 25, max_attempts: int = 5) -> dict:
    url = f"{BASE_URL}{path}"
    delay_s = 1.0
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            r = _session.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt == max_attempts:
                raise
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.8, 10.0)
            _reset_session()

    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")


def vss_post(path: str, payload: dict, timeout: int = 25, max_wait_seconds: int = 300) -> dict:
    started = time.time()
    delay_s = 1.5

    while True:
        j = vss_post_raw(path, payload, timeout=timeout)
        status = j.get("status")
        if status == 10000:
            return j
        if status == 10129:
            if time.time() - started + delay_s > max_wait_seconds:
                raise RuntimeError(f"VSS rate-limited (10129) after {int(time.time() - started)}s full={j}")
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.7, 30.0)
            continue
        raise RuntimeError(f"VSS error status={status} msg={j.get('msg')} full={j}")


def _load_token_from_file() -> tuple[str, str] | None:
    if not _TOKEN_FILE.is_file():
        return None
    try:
        lines = [ln.strip() for ln in _TOKEN_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        parts = lines[0].split()
        tok = parts[0].strip()
        pid = parts[1].strip() if len(parts) > 1 else ""
        # Common hand-edit: token on line 1, pid alone on line 2 (save format is one line).
        if not pid and len(lines) > 1:
            pid = lines[1].strip()
        return (tok, pid) if tok else None
    except Exception:
        return None


def _save_token_to_file(token: str, pid: str) -> None:
    try:
        _TOKEN_FILE.write_text(f"{token} {pid}\n", encoding="utf-8")
    except Exception:
        pass


def login_with_backoff(max_wait_seconds: int = 60) -> tuple[str, str]:
    """Log in via apiLogin, backing off on 10082 without hammering the endpoint.

    **Important:** every `apiLogin` attempt counts toward VSS rate limits. Short
    retry loops (5s, 8s, …) keep you stuck in 10082. On 10082 we wait **at
    least** `VSS_10082_SLEEP_SEC` (default 120s) before trying again.
    """
    started = time.time()
    cool_10082 = float(_env("VSS_10082_SLEEP_SEC", "120") or "120")
    cool_10082 = max(60.0, cool_10082)
    while True:
        j = vss_post_raw(
            "/vss/user/apiLogin.action",
            {"username": USERNAME, "password": md5_hex(PASSWORD_PLAINTEXT)},
            timeout=25,
            max_attempts=3,
        )
        if j.get("status") == 10000 and isinstance(j.get("data"), dict):
            data = j["data"]
            tok = str(data.get("token") or "")
            pid = str(data.get("pid") or "")
            return tok, pid
        if j.get("status") == 10082:
            # When a hand-pasted token file exists, apiLogin will keep failing for ~10 min — fail fast.
            if _load_token_from_file() and not _env_truthy("VSS_10082_RETRY_LOGIN"):
                raise RuntimeError(
                    "VSS login rate-limited (10082). Paste a fresh browser token into "
                    ".vss_token.txt and click Refresh — do not call apiLogin until the lockout clears. "
                    f"full={j}"
                )
            elapsed = time.time() - started
            if elapsed >= max_wait_seconds:
                raise RuntimeError(
                    f"VSS login rate-limited (10082) after {int(elapsed)}s. "
                    f"Stop all dashboards/notebooks using this account for ~10 min, "
                    f"put a token in .vss_token.txt or VSS_TOKEN, then retry. full={j}"
                )
            # Long quiet period so we don't extend VSS lockout with rapid logins.
            wait = min(cool_10082, max(0.0, max_wait_seconds - elapsed - 1.0))
            if wait < 5.0:
                raise RuntimeError(
                    f"VSS login rate-limited (10082); not enough time left in budget ({max_wait_seconds}s). "
                    f"Increase VSS_LOGIN_MAX_WAIT or wait without calling apiLogin. full={j}"
                )
            time.sleep(wait)
            continue
        raise RuntimeError(f"VSS login failed status={j.get('status')} msg={j.get('msg')} full={j}")


def _env_truthy(name: str, default: str = "0") -> bool:
    v = _env(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _vss_credentials_in_env() -> bool:
    """True when ``VSS_USERNAME`` and ``VSS_PASSWORD`` are both set in the process environment.

    Used to recover from 10023 via ``apiLogin`` after a stale ``.vss_token.txt`` would
    otherwise block login (file is normally preferred over ``apiLogin``).
    """
    u = os.environ.get("VSS_USERNAME", "")
    p = os.environ.get("VSS_PASSWORD", "")
    return bool(str(u).strip() and str(p).strip())


_thread_ctx = threading.local()


def set_vss_no_login(enabled: bool) -> None:
    """When True, ``ensure_token`` never calls apiLogin (used during manual/auto refresh)."""
    _thread_ctx.no_login = enabled


def _vss_no_login() -> bool:
    return bool(getattr(_thread_ctx, "no_login", False))


@contextmanager
def vss_no_login_mode():
    """Reuse the in-memory VSS session during refresh — no apiLogin / file reload."""
    set_vss_no_login(True)
    try:
        yield
    finally:
        set_vss_no_login(False)


def try_token_without_login() -> tuple[str, str] | None:
    """Return a token from memory, env, or file without calling apiLogin."""
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME

    with _lock:
        if _VSS_TOKEN:
            _LAST_TOKEN_SOURCE = "memory"
            return _VSS_TOKEN, _VSS_PID or ""

        env_tok = _env("VSS_TOKEN")
        env_pid = _env("VSS_PID")
        if env_tok:
            _LAST_TOKEN_SOURCE = "env"
            _FILE_TOKEN_MTIME = None
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = env_tok, env_pid, datetime.now()
            return _VSS_TOKEN, _VSS_PID or ""

        cached = _load_token_from_file()
        if cached:
            _LAST_TOKEN_SOURCE = "file"
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = cached[0], cached[1], datetime.now()
            try:
                _FILE_TOKEN_MTIME = _TOKEN_FILE.stat().st_mtime
            except OSError:
                _FILE_TOKEN_MTIME = None
            return _VSS_TOKEN, _VSS_PID or ""
    return None


def ensure_token(
    *,
    force: bool = False,
    skip_file: bool = False,
    login_max_wait_seconds: int | None = None,
) -> tuple[str, str]:
    """Return the *same* token across all refresh cycles.

    VSS keeps a session alive for 30 minutes of inactivity, and the dashboard
    auto-refreshes every 5 minutes — so once we have a token we keep using it
    until the server itself says it's invalid (status 10023). At that point
    `_retry_on_session_expired` will pass `force=True` to re-check env/file and
    only then log in once.

    Lookup order (when no in-memory token exists yet, or ``force=True``):

      1) ``VSS_TOKEN`` env var
      2) ``.vss_token.txt`` file (skipped when ``skip_file=True``)
      3) login with backoff (handles 10082)
    """
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME

    with _lock:
        # PID may be empty on some responses; token alone must still count as a session.
        if not force and _VSS_TOKEN:
            if _FILE_TOKEN_MTIME is not None and _TOKEN_FILE.is_file():
                try:
                    if _TOKEN_FILE.stat().st_mtime > _FILE_TOKEN_MTIME:
                        _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = None, None, None
                        _FILE_TOKEN_MTIME = None
                except OSError:
                    pass
            if _VSS_TOKEN:
                _LAST_TOKEN_SOURCE = "memory"
                return _VSS_TOKEN, _VSS_PID or ""

        if force:
            # Drop the stale in-memory token so a newly pasted env/file token can take over
            # without forcing another apiLogin attempt.
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = None, None, None
            _FILE_TOKEN_MTIME = None

        env_tok = _env("VSS_TOKEN")
        env_pid = _env("VSS_PID")
        if env_tok:
            _LAST_TOKEN_SOURCE = "env"
            _FILE_TOKEN_MTIME = None
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = env_tok, env_pid, datetime.now()
            return _VSS_TOKEN, _VSS_PID or ""

        if not skip_file:
            cached = _load_token_from_file()
            if cached:
                _LAST_TOKEN_SOURCE = "file"
                _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = cached[0], cached[1], datetime.now()
                try:
                    _FILE_TOKEN_MTIME = _TOKEN_FILE.stat().st_mtime
                except OSError:
                    _FILE_TOKEN_MTIME = None
                return _VSS_TOKEN, _VSS_PID or ""

        if _vss_no_login():
            raise RuntimeError(
                "VSS token not available for refresh (no apiLogin in refresh mode). "
                "The dashboard reuses the in-memory session for 30 minutes — restart the app "
                "or paste a new token into .vss_token.txt if the session has expired."
            )

        if login_max_wait_seconds is not None:
            max_wait = max(30, int(login_max_wait_seconds))
        else:
            max_wait = int(float(_env("VSS_LOGIN_MAX_WAIT", "600") or "600"))
            max_wait = max(120, max_wait)
        token, pid = login_with_backoff(max_wait_seconds=max_wait)
        _LAST_TOKEN_SOURCE = "login"
        _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = token, pid, datetime.now()
        _save_token_to_file(token, pid)
        try:
            _FILE_TOKEN_MTIME = _TOKEN_FILE.stat().st_mtime
        except OSError:
            _FILE_TOKEN_MTIME = None
        return token, pid


def last_vss_token_source() -> str | None:
    """Where ``ensure_token()`` last took the token from: ``memory``, ``env``, ``file``, or ``login``."""
    return _LAST_TOKEN_SOURCE


def get_current_token() -> tuple[str, str] | None:
    """Return the in-memory token without ever logging in (for inspection)."""
    with _lock:
        if _VSS_TOKEN:
            return _VSS_TOKEN, _VSS_PID or ""
    return None


def _token_for_keepalive(*, allow_reauth: bool) -> tuple[str, str] | None:
    """Prefer in-memory token; never log in when refresh/no-reauth mode is active."""
    tok = get_current_token()
    if tok:
        return tok
    if _vss_no_login() or not allow_reauth:
        return try_token_without_login()
    return ensure_token()


def keepalive_ping(*, allow_reauth: bool = True) -> bool:
    """Touch a tiny endpoint to reset the VSS 30-min inactivity timer.

    Returns True if the token is still alive (or got refreshed), False if VSS
    is currently rejecting logins (10082) and the dashboard should keep using
    its cached data for now.

    When ``allow_reauth`` is False (refresh / background keepalive), the same
    in-memory token is pinged without reloading ``.vss_token.txt`` or calling
    apiLogin on 10023.
    """
    tok = _token_for_keepalive(allow_reauth=allow_reauth)
    if not tok:
        return False
    token, _ = tok
    payload = {"token": token, "terminal": 2, "lang": "en"}
    j = vss_post_raw("/vss/lang/findLangDict.action", payload, timeout=15, max_attempts=2)
    status = j.get("status") if isinstance(j, dict) else None
    if status == 10000:
        return True
    if status == 10023:
        if _vss_no_login() or not allow_reauth:
            return False
        try:
            token2, _ = ensure_token(force=True)
            j2 = vss_post_raw(
                "/vss/lang/findLangDict.action",
                {"token": token2, "terminal": 2, "lang": "en"},
                timeout=15,
                max_attempts=2,
            )
            st2 = j2.get("status") if isinstance(j2, dict) else None
            if st2 == 10000:
                return True
            if st2 == 10023:
                return False
            # Unusual response after reload — treat as alive to avoid false negatives.
            return True
        except RuntimeError:
            return False
    # Other status codes mean the token still authenticated us, just data was empty
    return True


def validate_or_renew_token(*, allow_reauth: bool = True) -> tuple[bool, str]:
    """Make sure the in-memory token is actually valid before heavy work.

    Returns (ok, message). When ok is False, the caller should fall back to
    cached data and try again on the next refresh tick.
    """
    try:
        ok = keepalive_ping(allow_reauth=allow_reauth)
        return (
            ok,
            "ok"
            if ok
            else "VSS session invalid/expired or login throttled (10082); using cached data",
        )
    except RuntimeError as e:
        return (False, f"token check failed: {e}")


def _retry_on_session_expired(call_fn):
    """Run ``call_fn(token)``; on 10023 reload from env/file once, then stop (no long apiLogin loops)."""
    token, _ = ensure_token()
    try:
        return call_fn(token)
    except RuntimeError as e:
        msg = str(e)
        if "10023" not in msg and "session has expired" not in msg.lower():
            raise
        token, _ = ensure_token(force=True)
        try:
            return call_fn(token)
        except RuntimeError as e2:
            msg2 = str(e2)
            if "10023" not in msg2 and "session has expired" not in msg2.lower():
                raise
            if _vss_no_login():
                raise RuntimeError(
                    "VSS session expired (10023). Refresh reuses the in-memory token — "
                    "paste a new token into .vss_token.txt and restart, or wait for the "
                    "next startup login."
                ) from e2
            if _load_token_from_file():
                raise RuntimeError(
                    "VSS session expired (10023). Paste a new token from the VSS web UI into "
                    ".vss_token.txt (token and pid on one line or two lines), save, and click "
                    "Refresh data — apiLogin is not used while .vss_token.txt exists (avoids 10082 lockout)."
                ) from e2
            if not _vss_credentials_in_env():
                raise
            token, _ = ensure_token(force=True, skip_file=True, login_max_wait_seconds=90)
            return call_fn(token)


def fleet_id_csv(fleet_ids: list[str] | str | None) -> str:
    if not fleet_ids:
        return ""
    if isinstance(fleet_ids, str):
        return fleet_ids
    return ",".join([str(x).strip() for x in fleet_ids if str(x).strip()])


def list_devices_page(token: str, page_num: int, page_count: int = 200, *, keyword: str = "", fleetid: str = "") -> dict:
    payload: dict[str, Any] = {
        "token": token,
        "pageNum": page_num,
        "pageCount": page_count,
        "keyword": keyword,
    }
    if fleetid:
        payload["fleetid"] = fleetid

    j = vss_post_raw("/vss/vehicle/findAll.action", payload)
    status = j.get("status")
    if status == 10000:
        return j.get("data") or {}
    if status == 10025:
        return {"dataList": [], "totalCount": 0}
    raise RuntimeError(f"findAll failed status={status} msg={j.get('msg')} full={j}")


def list_all_fleets(token: str) -> list[dict]:
    """Get every fleet via /vss/fleet/findAll.action (pageNum=-1, pageCount=-1).

    Tries JSON first, then form-urlencoded (matches the bundled web client).
    Returns [] on 10025 ("no data") and on transport failures.
    """
    payloads = [
        {"token": token, "pageNum": -1, "pageCount": -1},
        {"token": token, "pageNum": "-1", "pageCount": "-1"},
        {"token": token},
    ]

    for payload in payloads:
        try:
            j = vss_post_raw("/vss/fleet/findAll.action", payload, timeout=60, max_attempts=3)
        except Exception:
            continue
        st = j.get("status") if isinstance(j, dict) else None
        if st == 10025:
            return []
        if st != 10000:
            continue
        data = j.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            dl = data.get("dataList") or data.get("list") or []
            if isinstance(dl, list):
                return dl

    try:
        url = f"{BASE_URL}/vss/fleet/findAll.action"
        r = _session.post(
            url,
            data={"token": token, "pageNum": "-1", "pageCount": "-1"},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=60,
        )
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and j.get("status") == 10000:
            data = j.get("data")
            if isinstance(data, dict):
                dl = data.get("dataList") or []
                if isinstance(dl, list):
                    return dl
        if isinstance(j, dict) and j.get("status") == 10025:
            return []
    except Exception:
        pass

    return []


def _fleet_fields(f: dict) -> tuple[str, str]:
    fid = (
        f.get("fleetid")
        or f.get("fleetId")
        or f.get("id")
        or f.get("guid")
        or f.get("fleetGuid")
        or ""
    )
    name = (
        f.get("fleetname")
        or f.get("fleetName")
        or f.get("name")
        or ""
    )
    return str(fid), str(name)


def _fleet_parent_id(f: dict) -> str:
    v = (
        f.get("pid")
        or f.get("parentId")
        or f.get("parentid")
        or f.get("parentFleetId")
        or f.get("parentfleetid")
        or ""
    )
    return str(v).strip()


def _fleets_children_index(all_fleets: list[dict]) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """parent_fleet_id -> [(child_id, child_name), ...], and id -> name."""
    by_parent: dict[str, list[tuple[str, str]]] = {}
    id_to_name: dict[str, str] = {}
    for f in all_fleets:
        if not isinstance(f, dict):
            continue
        fid, name = _fleet_fields(f)
        if not fid:
            continue
        id_to_name[fid] = name or fid
        pid = _fleet_parent_id(f)
        by_parent.setdefault(pid, []).append((fid, name or fid))
    return by_parent, id_to_name


def expand_fleet_tree_from_root(token: str, root_id: str) -> dict[str, str]:
    """All fleet IDs under ``root_id`` (including the root), using ``pid`` links from ``findAll``."""
    root_id = (root_id or "").strip()
    if not root_id:
        return {}
    all_f = list_all_fleets(token)
    by_parent, id_to_name = _fleets_children_index(all_f)
    if root_id not in id_to_name:
        _vss_log.warning(
            "DHL_ROOT_FLEET_ID=%s… not found in /fleet/findAll — check the GUID or user scope",
            root_id[:12],
        )
        # Do not crawl a synthetic single-id tree: /vehicle/findAll for unknown fleet ids is empty
        # and would incorrectly fall through to global keyword paging in older logic.
        return {}
    out: dict[str, str] = {}
    queue = [root_id]
    seen: set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        out[cur] = id_to_name.get(cur, cur)
        for child_id, cname in by_parent.get(cur, []):
            if child_id not in seen:
                queue.append(child_id)
    return out


def explicit_dhl_fleets_from_env(token: str) -> dict[str, str]:
    """Optional env: ``DHL_ROOT_FLEET_ID`` (umbrella + descendants) or ``DHL_FLEET_IDS`` (comma-separated)."""
    root = _env("DHL_ROOT_FLEET_ID", "").strip()
    if root:
        m = expand_fleet_tree_from_root(token, root)
        if m:
            _vss_log.info(
                "Using DHL_ROOT_FLEET_ID: %s fleet(s) to crawl (umbrella tree)",
                len(m),
            )
        else:
            _vss_log.error(
                "DHL_ROOT_FLEET_ID is set but resolved 0 fleets — wrong GUID, or fleet/findAll does not "
                "return that id / pid tree for this user."
            )
        return m
    raw = _env("DHL_FLEET_IDS", "").strip()
    if not raw:
        return {}
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    if not ids:
        return {}
    id_to_name: dict[str, str] = {}
    for f in list_all_fleets(token):
        if not isinstance(f, dict):
            continue
        fid, name = _fleet_fields(f)
        if fid in ids:
            id_to_name[fid] = name or fid
    out = {i: id_to_name.get(i, i) for i in ids}
    _vss_log.info("Using DHL_FLEET_IDS: %s fleet(s) to crawl", len(out))
    return out


def discover_dhl_fleets(*, contains: str = "DHL") -> list[tuple[str, str]]:
    """Return [(fleet_id, fleet_name)] for fleets whose name contains the keyword."""
    q = (contains or "").strip().upper()

    def _call(token: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for f in list_all_fleets(token):
            if not isinstance(f, dict):
                continue
            fid, name = _fleet_fields(f)
            if not fid:
                continue
            if q and q not in name.upper():
                continue
            out.append((fid, name))
        return out

    return _retry_on_session_expired(_call)


def discover_dhl_devices(
    *,
    page_size: int = 200,
    max_pages: int = 60,
    contains: str = "DHL",
    skip_fleet_discovery: bool = False,
    fleet_fetch_workers: int = 6,
) -> list[dict]:
    """All devices that belong to DHL fleets.

    Resolution order (first match wins):
      0) **Env** ``DHL_ROOT_FLEET_ID`` — expand the umbrella fleet and all descendants (``pid`` tree)
         from ``/vss/fleet/findAll``, then page ``/vehicle/findAll`` per fleet.
      1) **Env** ``DHL_FLEET_IDS`` — comma-separated fleet GUIDs (same per-fleet paging).
      2) List fleets whose **name** contains ``contains`` (default ``DHL``), then page per fleet.
      3) **Fallback:** keyword ``findAll`` scan on device names / fleet names — **skipped** when fleets
         came from ``DHL_ROOT_FLEET_ID`` / ``DHL_FLEET_IDS`` unless ``DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1``.

    ``skip_fleet_discovery=True`` (e.g. ``DHL_FAST_DEVICE_KEYWORD_ONLY``) uses the global keyword scan;
    it runs after env fleet resolution and bypasses the env-only error stop when you explicitly opt into keyword mode.
    """
    q = (contains or "").strip().upper()
    fleet_id_to_name: dict[str, str] = {}

    def _call(token: str) -> list[dict]:
        nonlocal fleet_id_to_name
        # True when .env asks for fleet-scoped discovery (even if VSS resolves 0 fleets).
        env_fleet_sources_requested = bool(
            _env("DHL_ROOT_FLEET_ID", "").strip() or _env("DHL_FLEET_IDS", "").strip()
        )
        configured = explicit_dhl_fleets_from_env(token)
        if configured:
            fleet_id_to_name = configured
        elif skip_fleet_discovery:
            _vss_log.info("discover_dhl_devices: keyword-only (skip fleet list + per-fleet crawl)")
            fleet_id_to_name = {}
        elif env_fleet_sources_requested:
            fleet_id_to_name = {}
            _vss_log.error(
                "DHL_ROOT_FLEET_ID / DHL_FLEET_IDS is set but VSS returned no fleet ids to crawl "
                "(root missing from /fleet/findAll, or DHL_FLEET_IDS did not match any fleet). "
                "Skipping global keyword device scan; fix the GUID(s) or set "
                "DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1 to opt in to keyword paging.",
            )
        else:
            fleet_pairs = discover_dhl_fleets(contains=contains)
            fleet_id_to_name = {fid: name for fid, name in fleet_pairs if fid}
            if not fleet_id_to_name:
                _vss_log.warning(
                    "No fleets matched name %r in fleet/findAll — falling back to keyword device scan. "
                    "Set DHL_ROOT_FLEET_ID or DHL_FLEET_IDS in .env to crawl by fleet id instead.",
                    contains,
                )

        def _fetch_one(fid: str) -> list[dict]:
            out: list[dict] = []
            for page in range(1, max_pages + 1):
                try:
                    d = list_devices_page(token, page, page_size, fleetid=fid) or {}
                except Exception:
                    return out
                page_rows = d.get("dataList") or []
                if not page_rows:
                    break
                for r in page_rows:
                    if not isinstance(r, dict):
                        continue
                    if not r.get("fleetid"):
                        r["fleetid"] = fid
                    if not r.get("fleetName"):
                        r["fleetName"] = fleet_id_to_name.get(fid, "")
                    out.append(r)
            return out

        rows: list[dict] = []
        if fleet_id_to_name:
            w = max(1, min(fleet_fetch_workers, 16))
            with ThreadPoolExecutor(max_workers=w) as ex:
                for batch in ex.map(_fetch_one, list(fleet_id_to_name.keys())):
                    rows.extend(batch)

        if rows:
            by_id: dict[str, dict] = {}
            for r in rows:
                did = str(r.get("deviceno") or "")
                if did and did != "None":
                    by_id[did] = r
            return list(by_id.values())

        allow_kw = _env("DHL_ALLOW_KEYWORD_DEVICE_FALLBACK", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        # Empty ``fleet_id_to_name`` must still suppress keyword scan when .env asked for fleet ids
        # (unless keyword-only mode explicitly requested via ``skip_fleet_discovery``).
        if env_fleet_sources_requested and not allow_kw and not skip_fleet_discovery:
            if fleet_id_to_name:
                _vss_log.warning(
                    "discover_dhl_devices: 0 devices after paging /vehicle/findAll for %s fleet(s) from "
                    "DHL_ROOT_FLEET_ID / DHL_FLEET_IDS — skipping keyword scan. "
                    "Fix token/VSS access or set DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1 to force the old keyword crawl.",
                    len(fleet_id_to_name),
                )
            return []

        # Fallback: keyword scan on devices, match against deviceName too.
        seen: dict[str, dict] = {}
        for page in range(1, max_pages + 1):
            if page == 1 or page % 2 == 0 or page == max_pages:
                _vss_log.info(
                    "discover_dhl_devices: keyword scan page %s/%s (%s devices so far)",
                    page,
                    max_pages,
                    len(seen),
                )
            d = list_devices_page(token, page, page_size, keyword=contains) or {}
            page_rows = d.get("dataList") or []
            if not page_rows:
                break
            for r in page_rows:
                if not isinstance(r, dict):
                    continue
                fname = str(r.get("fleetName") or "")
                dname = str(r.get("devicename") or r.get("deviceName") or "")
                if q and (q in fname.upper() or q in dname.upper()):
                    fid = str(r.get("fleetid") or "")
                    if not r.get("fleetName") and fid in fleet_id_to_name:
                        r["fleetName"] = fleet_id_to_name[fid]
                    did = str(r.get("deviceno") or "")
                    if did and did != "None":
                        seen[did] = r
        return list(seen.values())

    return _retry_on_session_expired(_call)


def current_gps_and_status(token: str, device_ids: list[str] | str) -> list[dict]:
    if isinstance(device_ids, list):
        device_ids = ",".join(device_ids)
    j = vss_post(
        "/vss/vehicle/getDeviceStatus.action",
        {"token": token, "deviceID": device_ids},
    )
    data = j.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        lst = data.get("dataList") or data.get("list") or data.get("rows")
        if isinstance(lst, list):
            return [r for r in lst if isinstance(r, dict)]
        if isinstance(data, dict) and (
            "deviceno" in data or "deviceguid" in data or "deviceID" in data or "deviceid" in data
        ):
            return [data]
    return []


def realtime_status_for_devices(
    device_ids: list[str],
    *,
    batch: int = 20,
    sleep_s: float = 0.0,
    max_workers: int = 6,
) -> list[dict]:
    """Pull realtime status for many devices, parallel batches, returning a flat list."""
    chunks = [device_ids[i : i + batch] for i in range(0, len(device_ids), batch)]

    def _call(token: str) -> list[dict]:
        def _fetch(chunk: list[str]) -> list[dict]:
            try:
                return current_gps_and_status(token, chunk)
            except RuntimeError as e:
                msg = str(e)
                if "10023" in msg or "session has expired" in msg.lower():
                    raise
                _vss_log.warning("getDeviceStatus failed (batch size %s): %s", len(chunk), e)
                return []
            except Exception as e:  # noqa: BLE001
                _vss_log.warning("getDeviceStatus failed (batch size %s): %s", len(chunk), e)
                return []

        out: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for rows in ex.map(_fetch, chunks):
                out.extend(rows)
        if sleep_s:
            time.sleep(sleep_s)
        return out

    return _retry_on_session_expired(_call)


def _alarm_history_path() -> str:
    """VSS Web API V2.8 documents ``/vss/alarm/apiFindAllByTime.action`` (§3.9).

    Some deployments still accept the legacy ``findAllByTime.action``. Override with
    ``VSS_ALARM_FIND_PATH`` if needed.
    """
    # Default to legacy URL — most live VSS builds match the dashboard’s prior behaviour.
    # Set ``/vss/alarm/apiFindAllByTime.action`` for strict V2.8 manual alignment.
    p = _env("VSS_ALARM_FIND_PATH", "/vss/alarm/findAllByTime.action").strip()
    if not p:
        return "/vss/alarm/findAllByTime.action"
    return p if p.startswith("/") else f"/{p}"


def _alarms_one_request(token: str, payload: dict, *, path: str | None = None) -> dict:
    """Paged alarm history call with 10129 backoff."""
    url = path or _alarm_history_path()
    started = time.time()
    delay_s = 1.5
    while True:
        j = vss_post_raw(url, payload, timeout=90, max_attempts=4)
        st = j.get("status") if isinstance(j, dict) else None
        if st == 10000:
            return j.get("data") or {}
        if st == 10025:
            return {"dataList": [], "totalCount": 0}
        if st == 10129:
            if time.time() - started > 180:
                raise RuntimeError(f"alarms rate-limited (10129) too long: {j}")
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.7, 30.0)
            continue
        raise RuntimeError(f"alarm history failed ({url}) status={st} msg={j.get('msg') if isinstance(j, dict) else j}")


def _alarms_paged_for_device_batch(
    token: str,
    *,
    device_ids: list[str],
    begin_time: str,
    end_time: str,
    alarm_type_csv: str,
    page_count: int,
    max_pages: int,
) -> list[dict]:
    rows: list[dict] = []
    device_csv = ",".join(str(d) for d in device_ids if d)
    path = _alarm_history_path()
    use_api_shape = "apiFindAllByTime" in path
    for page in range(1, max_pages + 1):
        if use_api_shape:
            # Howen VSS Web API V2.8 §3.9 (application/json or form post)
            payload = {
                "token": token,
                "pageNum": page,
                "pageCount": page_count,
                "deviceID": device_csv,
                "beginTime": begin_time,
                "endTime": end_time,
                "alarmType": alarm_type_csv or "",
            }
        else:
            payload = {
                "token": token,
                "beginTime": begin_time,
                "endTime": end_time,
                "pageNum": page,
                "pageCount": page_count,
                "keyword": "",
                "alarmType": alarm_type_csv,
                "fleetIdList": "",
                "deviceGuid": "",
                "deviceID": device_csv,
            }
        data = _alarms_one_request(token, payload, path=path)
        page_rows = data.get("dataList") or []
        if not page_rows:
            return rows
        rows.extend(page_rows)
        total = data.get("totalCount")
        if isinstance(total, int) and len(rows) >= total:
            return rows
    return rows


def alarms_find_all_by_time_for_devices(
    *,
    begin_dt: datetime,
    end_dt: datetime,
    device_ids: list[str],
    alarm_type_csv: str = "",
    page_count: int = 500,
    max_pages: int = 30,
    batch_size: int = 50,
    max_workers: int = 6,
) -> list[dict]:
    """All alarms in [begin_dt, end_dt] for the given DHL device IDs.

    Uses the documented alarm-by-page API (default ``apiFindAllByTime``). Device IDs are
    queried in batches to avoid huge tenant-wide pulls. Set ``VSS_ALARM_FIND_PATH`` to
    ``/vss/alarm/findAllByTime.action`` only if your server requires the legacy URL.
    """
    begin_time = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    clean_ids = [str(d).strip() for d in device_ids if str(d).strip()]
    if not clean_ids:
        return []

    batches = [clean_ids[i : i + batch_size] for i in range(0, len(clean_ids), batch_size)]

    def _call(token: str) -> list[dict]:
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(
                    _alarms_paged_for_device_batch,
                    token,
                    device_ids=batch,
                    begin_time=begin_time,
                    end_time=end_time,
                    alarm_type_csv=alarm_type_csv,
                    page_count=page_count,
                    max_pages=max_pages,
                )
                for batch in batches
            ]
            for fut in as_completed(futures):
                try:
                    results.extend(fut.result())
                except RuntimeError as e:
                    msg = str(e)
                    if "10023" in msg or "session has expired" in msg.lower():
                        raise
                    _vss_log.warning("alarm history batch failed: %s", e)
                except Exception as e:  # noqa: BLE001
                    _vss_log.warning("alarm history batch failed: %s", e)
        return results

    return _retry_on_session_expired(_call)


def alarms_find_all_by_time(
    *,
    begin_dt: datetime,
    end_dt: datetime,
    fleet_ids: list[str],
    alarm_type_csv: str = "",
    page_count: int = 500,
    max_pages: int = 200,
) -> list[dict]:
    """Compatibility wrapper kept for legacy callers — fleetIdList may be ignored."""
    begin_time = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    fleet_csv = fleet_id_csv(fleet_ids)
    # V2.8 ``apiFindAllByTime`` is device-scoped; fleet filtering stays on the legacy URL.
    path = "/vss/alarm/findAllByTime.action"

    def _call(token: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            payload = {
                "token": token,
                "beginTime": begin_time,
                "endTime": end_time,
                "pageNum": page,
                "pageCount": page_count,
                "keyword": "",
                "alarmType": alarm_type_csv,
                "fleetIdList": fleet_csv,
                "deviceGuid": "",
                "deviceID": "",
            }
            data = _alarms_one_request(token, payload, path=path)
            page_rows = data.get("dataList") or []
            if not page_rows:
                return rows
            rows.extend(page_rows)
            total = data.get("totalCount")
            if isinstance(total, int) and len(rows) >= total:
                return rows
        return rows

    return _retry_on_session_expired(_call)


def get_lang_dict(lang: str = "en") -> dict:
    j = vss_post_raw(
        "/vss/lang/findLangDict.action",
        {"terminal": 2, "lang": lang},
        timeout=60,
        max_attempts=3,
    )
    if isinstance(j, dict) and isinstance(j.get("data"), dict):
        return j["data"]
    return j if isinstance(j, dict) else {}
