"""MiX Telematics API client (positions + asset metadata).

Credentials: ``MIX_ACCOUNTS_JSON`` env var (preferred), ``accounts.json`` file
(see ``accounts.json.example``), or inline ``MIX_*`` env vars. Enable with ``MIX_ENABLED=1``.

South Africa (``mix_za``) is the default server key. Set ``MIX_GROUP_IDS`` for
explicit groups, ``MIX_PARENT_ORG_ID`` + ``MIX_SITE_PREFIX`` to pull sites under a
specific organisation (e.g. EABL/Diageo), ``MIX_PARENT_ORG_CONTAINS`` + ``MIX_SITE_PREFIX``
to match by org name, ``MIX_GROUP_NAME_CONTAINS`` to match organisation names, or
``MIX_FETCH_ALL_GROUPS=1`` to pull every accessible group.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import operation_log

log = logging.getLogger(__name__)
_REPO_DIR = Path(__file__).resolve().parent
_session = requests.Session()

_lock = threading.Lock()
_bearer_token: str | None = None
_token_expires_at: float = 0.0
_org_groups_cache: list[dict[str, Any]] | None = None
_org_groups_cache_at: float = 0.0
_rate_lock = threading.Lock()
_rate_times: deque[float] = deque(maxlen=25)

_MIX_COLUMNS = [
    "GroupId",
    "GroupName",
    "AssetId",
    "AssetName",
    "Registration",
    "Make",
    "DriverId",
    "Latitude",
    "Longitude",
    "SpeedKmh",
    "Rpm",
    "Heading",
    "AltitudeM",
    "Address",
    "GpsSource",
    "Satellites",
    "EventTime",
    "AgeHours",
    "LastUpdated",
]

_DEFAULT_SERVER_KEY = "mix_za"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_truthy(name: str, default: str = "0") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def _server_key() -> str:
    return _env("MIX_SERVER_KEY", _DEFAULT_SERVER_KEY)


def _resolve_accounts_path() -> Path:
    raw = _env("MIX_ACCOUNTS_JSON_PATH")
    if raw:
        p = Path(os.path.expandvars(raw))
        if not p.is_absolute():
            p = (_REPO_DIR / p).resolve()
        return p
    return _REPO_DIR / "accounts.json"


def _inline_creds_complete() -> bool:
    keys = (
        "MIX_API_URL",
        "MIX_IDENTITY_URL",
        "MIX_CLIENT_ID",
        "MIX_CLIENT_SECRET",
        "MIX_USERNAME",
        "MIX_PASSWORD",
    )
    return all(_env(k) for k in keys)


def _load_accounts_blob() -> dict[str, Any]:
    """Load MiX credentials from MIX_ACCOUNTS_JSON env, file path, or inline MIX_* vars."""
    raw_json = _env("MIX_ACCOUNTS_JSON")
    if raw_json:
        try:
            blob = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MIX_ACCOUNTS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise ValueError("MIX_ACCOUNTS_JSON must be a JSON object keyed by server name")
        return blob

    path = _resolve_accounts_path()
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            blob = json.load(fh)
        if not isinstance(blob, dict):
            raise ValueError(f"{path} must contain a JSON object keyed by server name")
        return blob

    raise FileNotFoundError(
        f"MiX credentials not found. Set MIX_ACCOUNTS_JSON in .env, "
        f"copy accounts.json.example to {path.name}, or set MIX_* env vars."
    )


def mix_enabled() -> bool:
    if _env_truthy("MIX_DISABLED"):
        return False
    if _env_truthy("MIX_ENABLED"):
        return True
    if _env("MIX_ACCOUNTS_JSON"):
        return True
    path = _resolve_accounts_path()
    return path.is_file() or _inline_creds_complete()


def mix_config_summary() -> str:
    if not mix_enabled():
        return "disabled"
    if _inline_creds_complete():
        return f"inline env ({_env('MIX_API_URL')})"
    if _env("MIX_ACCOUNTS_JSON"):
        return f"MIX_ACCOUNTS_JSON [{_server_key()}]"
    path = _resolve_accounts_path()
    return f"{path.name} [{_server_key()}]"


def _apply_credential_overrides(creds: dict[str, str]) -> dict[str, str]:
    """Allow rotating username/password via env without re-pushing full MIX_ACCOUNTS_JSON."""
    merged = dict(creds)
    for cred_key, env_key in (
        ("IdentityUsername", "MIX_USERNAME"),
        ("IdentityPassword", "MIX_PASSWORD"),
        ("IdentityClientId", "MIX_CLIENT_ID"),
        ("IdentityClientSecret", "MIX_CLIENT_SECRET"),
        ("ApiUrl", "MIX_API_URL"),
        ("IdentityUrl", "MIX_IDENTITY_URL"),
        ("IdentityScope", "MIX_SCOPE"),
    ):
        val = _env(env_key)
        if val:
            merged[cred_key] = val.strip()
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in merged.items()}


def _load_server_creds() -> dict[str, str]:
    if _inline_creds_complete():
        return _apply_credential_overrides({
            "ApiUrl": _env("MIX_API_URL"),
            "IdentityUrl": _env("MIX_IDENTITY_URL"),
            "IdentityClientId": _env("MIX_CLIENT_ID"),
            "IdentityClientSecret": _env("MIX_CLIENT_SECRET"),
            "IdentityUsername": _env("MIX_USERNAME"),
            "IdentityPassword": _env("MIX_PASSWORD"),
            "IdentityScope": _env("MIX_SCOPE", "offline_access+MiX.Integrate"),
        })
    server_key = _server_key()
    all_creds = _load_accounts_blob()
    if server_key not in all_creds:
        raise KeyError(
            f"Key '{server_key}' not in MiX accounts. Available: {list(all_creds.keys())}"
        )
    creds = all_creds[server_key]
    if not isinstance(creds, dict):
        raise ValueError(f"MiX accounts[{server_key!r}] must be a JSON object")
    return _apply_credential_overrides({str(k): str(v).strip() for k, v in creds.items()})


def api_base_url() -> str:
    return _load_server_creds()["ApiUrl"].rstrip("/")


def ensure_bearer_token() -> str:
    """Return a cached MiX bearer token, refreshing when near expiry."""
    global _bearer_token, _token_expires_at
    with _lock:
        if _bearer_token and time.time() < _token_expires_at - 60:
            return _bearer_token

    creds = _load_server_creds()
    identity_base = creds["IdentityUrl"].rstrip("/")
    token_url = f"{identity_base}/core/connect/token"
    try:
        disc = _session.get(
            f"{identity_base}/core/.well-known/openid-configuration",
            timeout=15,
        )
        if disc.status_code == 200:
            ep = disc.json().get("token_endpoint")
            if ep:
                token_url = str(ep)
    except Exception:  # noqa: BLE001
        pass

    scope = (creds.get("IdentityScope") or "offline_access+MiX.Integrate").strip()
    if not scope:
        scope = "offline_access+MiX.Integrate"
    elif "MiX.Integrate" not in scope.replace("+", " "):
        scope = f"{scope}+MiX.Integrate"

    from requests.utils import quote

    basic = base64.b64encode(
        f"{creds['IdentityClientId']}:{creds['IdentityClientSecret']}".encode()
    ).decode()
    body = (
        "grant_type=password"
        f"&username={quote(creds['IdentityUsername'])}"
        f"&password={quote(creds['IdentityPassword'])}"
        f"&scope={scope}"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
        "User-Agent": "Mozilla/5.0 (compatible; ControlTech-MiX-Automation/1.0)",
    }
    log.info("MiX: requesting bearer token from %s (user=%s)", token_url, creds["IdentityUsername"])
    operation_log.log_event(
        "mix_data", "oauth", "running", f"MiX OAuth starting ({_server_key()})",
        detail={"server": _server_key(), "user": creds["IdentityUsername"]},
    )
    resp = _session.post(token_url, data=body, headers=headers, timeout=30)
    if resp.status_code != 200:
        detail = _mix_token_error_detail(resp)
        msg = (
            f"MiX token request failed ({resp.status_code}): {detail}. "
            f"Check IdentityUsername/IdentityPassword in MIX_ACCOUNTS_JSON for {_server_key()}."
        )
        operation_log.log_event("mix_data", "oauth", "error", msg)
        raise RuntimeError(msg)
    data = resp.json()
    token = str(data["access_token"])
    expires_in = int(data.get("expires_in", 3600))
    with _lock:
        _bearer_token = token
        _token_expires_at = time.time() + expires_in
    log.info("MiX: token acquired (valid ~%s min)", expires_in // 60)
    operation_log.log_event(
        "mix_data",
        "oauth",
        "ok",
        f"MiX OAuth OK ({_server_key()}, valid ~{expires_in // 60} min)",
        detail={"server": _server_key(), "expires_in": expires_in},
    )
    return token


def _mix_token_error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            parts = [str(body.get(k)) for k in ("error", "error_description", "message") if body.get(k)]
            if parts:
                return " — ".join(parts)
    except Exception:
        pass
    text = (resp.text or "").strip()
    if text.startswith("<!DOCTYPE") or text.startswith("<html"):
        return "Unauthorized (invalid MiX username, password, or client credentials)"
    return text[:200]


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_organisation_groups(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """List organisation groups visible to the authenticated MiX user."""
    global _org_groups_cache, _org_groups_cache_at
    if (
        not force_refresh
        and _org_groups_cache is not None
        and time.time() - _org_groups_cache_at < 3600
    ):
        return list(_org_groups_cache)

    token = ensure_bearer_token()
    api = api_base_url()
    url = f"{api}/api/organisationgroups"
    resp = _mix_http_get_resilient(url, headers=_api_headers(token), timeout=30)
    if resp.status_code != 200:
        if _org_groups_cache is not None:
            log.warning(
                "MiX organisation groups failed (%s) — using in-memory cache (%s groups)",
                resp.status_code,
                len(_org_groups_cache),
            )
            return list(_org_groups_cache)
        cached = _load_organisation_groups_cache()
        if cached:
            log.warning(
                "MiX organisation groups failed (%s) — using Neon cache (%s groups)",
                resp.status_code,
                len(cached),
            )
            with _lock:
                _org_groups_cache = cached
                _org_groups_cache_at = time.time()
            return list(cached)
        log.warning(
            "MiX organisation groups failed (%s) — continuing without flat group list",
            resp.status_code,
        )
        return []
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"MiX organisation groups unexpected response: {type(data)}")
    with _lock:
        _org_groups_cache = data
        _org_groups_cache_at = time.time()
    unparsed = sum(1 for g in data if _group_target(g) is None)
    if unparsed:
        log.warning("MiX: %s/%s organisation groups had unparsed GroupId", unparsed, len(data))
    if data:
        sample = data[0]
        log.info(
            "MiX: sample group raw_id=%r parsed=%s name=%r type=%r",
            sample.get("GroupId", sample.get("groupId")),
            _group_field_int(sample, "GroupId", "groupId"),
            sample.get("Name", sample.get("name")),
            sample.get("Type"),
        )
    log.info("MiX: %s organisation groups loaded", len(data))
    _save_organisation_groups_cache(data)
    return data


def clear_org_groups_cache() -> None:
    """Drop cached /api/organisationgroups (used on manual refresh)."""
    global _org_groups_cache, _org_groups_cache_at
    with _lock:
        _org_groups_cache = None
        _org_groups_cache_at = 0.0


def resolve_group_targets() -> list[dict[str, Any]]:
    """Return ``[{GroupId, Name}, ...]`` based on env configuration."""
    site_prefix = _env("MIX_SITE_PREFIX", "DHL")

    parent_org_id = _env("MIX_PARENT_ORG_ID")
    if parent_org_id:
        org_id = _normalize_mix_id(parent_org_id)
        if org_id is None:
            raise ValueError(f"MIX_PARENT_ORG_ID is not a valid MiX id: {parent_org_id!r}")
        groups = fetch_organisation_groups()
        matched = _sites_under_org_id(groups, org_id, site_prefix)
        if not matched:
            for org_contains in filter(
                None,
                (
                    _env("MIX_PARENT_ORG_CONTAINS"),
                    "EABL",
                    "Diageo",
                ),
            ):
                matched = _sites_under_org_with_prefix(groups, org_contains, site_prefix)
                if matched:
                    log.info(
                        "MiX: %s site(s) under %r with prefix %r (name fallback)",
                        len(matched),
                        org_contains,
                        site_prefix,
                    )
                    break
        if not matched:
            matched = _flat_dhl_named_groups(groups, site_prefix)
            if matched:
                log.warning(
                    "MiX: org id %s matched 0 DHL sites; using %s DHL-named visible group(s)",
                    parent_org_id,
                    len(matched),
                )
        if matched:
            _save_site_targets_cache(matched, org_id=org_id, site_prefix=site_prefix)
            log.info(
                "MiX: %s site(s) under org id %s with DHL prefix %r — %s",
                len(matched),
                parent_org_id,
                site_prefix,
                ", ".join(str(g.get("Name", g["GroupId"])) for g in matched[:5])
                + (" …" if len(matched) > 5 else ""),
            )
            return matched
        cached = _load_site_targets_cache(org_id, site_prefix)
        if not cached:
            cached = _site_targets_from_health_snapshot(site_prefix)
        if cached:
            log.warning(
                "MiX: live site API unavailable — using %s cached DHL site(s) for org %s",
                len(cached),
                parent_org_id,
            )
            return cached
        dhl_flat = sum(
            1 for g in groups if _site_name_has_prefix(str(g.get("Name", "")), site_prefix)
        )
        log.error(
            "MiX: site resolution failed org=%s flat_groups=%s dhl_named_flat=%s",
            parent_org_id,
            len(groups),
            dhl_flat,
        )
        raise RuntimeError(
            f"No MiX sites under org id {parent_org_id!r} with prefix {site_prefix!r}. "
            "Check MIX_PARENT_ORG_ID / MIX_SITE_PREFIX or set MIX_GROUP_IDS."
        )

    explicit = _env("MIX_GROUP_IDS")
    if explicit:
        names = {g["GroupId"]: g.get("Name", "") for g in fetch_organisation_groups()}
        out: list[dict[str, Any]] = []
        for part in explicit.split(","):
            part = part.strip()
            if not part:
                continue
            gid = _normalize_mix_id(part)
            if gid is None:
                continue
            out.append({"GroupId": gid, "Name": names.get(gid, str(gid))})
        if not out:
            raise ValueError("MIX_GROUP_IDS is empty")
        log.info("MiX: using MIX_GROUP_IDS (%s group(s))", len(out))
        return out

    groups = fetch_organisation_groups()

    parent_org = _env("MIX_PARENT_ORG_CONTAINS")
    if parent_org:
        matched = _sites_under_org_with_prefix(groups, parent_org, site_prefix)
        if matched:
            log.info(
                "MiX: %s site(s) under %r with prefix %r",
                len(matched),
                parent_org,
                site_prefix,
            )
            return matched
        raise RuntimeError(
            f"No MiX sites under {parent_org!r} with prefix {site_prefix!r}. "
            "Check MIX_PARENT_ORG_CONTAINS / MIX_SITE_PREFIX or set MIX_GROUP_IDS."
        )

    needle = _env("MIX_GROUP_NAME_CONTAINS")
    if needle:
        groups = [g for g in groups if needle.lower() in str(g.get("Name", "")).lower()]
        if not groups:
            raise RuntimeError(f"No MiX groups match MIX_GROUP_NAME_CONTAINS={needle!r}")

    if _env_truthy("MIX_FETCH_ALL_GROUPS") or not needle:
        if not _env_truthy("MIX_FETCH_ALL_GROUPS") and not needle:
            raise RuntimeError(
                "Set MIX_GROUP_IDS, MIX_PARENT_ORG_ID, MIX_PARENT_ORG_CONTAINS (e.g. Diageo), "
                "MIX_GROUP_NAME_CONTAINS, or MIX_FETCH_ALL_GROUPS=1 in .env"
            )
        return [{"GroupId": g["GroupId"], "Name": g.get("Name", "")} for g in groups]

    return [{"GroupId": g["GroupId"], "Name": g.get("Name", "")} for g in groups]


def _normalize_mix_id(value: Any) -> int | None:
    """Parse MiX snowflake ids from API/Excel forms (``1058…``, ``A1058…``, ``A-7766…``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Avoid precision loss — only accept floats that are exact integers.
        if value != value or abs(value) > 2**63:
            return None
        as_int = int(value)
        if float(as_int) == value:
            return as_int
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    upper = text.upper()
    if upper.startswith("A-"):
        tail = text[2:].strip()
        if not tail:
            return None
        try:
            return int(f"-{tail}") if not tail.startswith("-") else int(tail)
        except ValueError:
            return None
    if upper.startswith("A") and len(text) > 1:
        tail = text[1:].strip()
        if not tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None
    return None


def _mix_id_api_variants(value: Any) -> list[str]:
    """Return id strings to try in MiX REST paths (plain, ``A…``, ``A-…``)."""
    gid = _normalize_mix_id(value)
    if gid is None:
        raw = str(value).strip()
        return [raw] if raw else []
    variants = [str(gid)]
    if gid >= 0:
        variants.append(f"A{gid}")
    else:
        variants.append(f"A{gid}")
        variants.append(f"A-{abs(gid)}")
    return list(dict.fromkeys(variants))


def _group_field_int(group: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = group.get(key)
        if value is None or str(value).strip() == "":
            continue
        parsed = _normalize_mix_id(value)
        if parsed is not None:
            return parsed
    return None


def _group_parent_id(group: dict[str, Any]) -> int | None:
    return _group_field_int(
        group,
        "ParentGroupId",
        "parentGroupId",
        "ParentId",
        "parentId",
        "ParentOrganisationGroupId",
        "parentOrganisationGroupId",
    )


def _group_organisation_id(group: dict[str, Any]) -> int | None:
    return _group_field_int(
        group,
        "OrganisationGroupId",
        "organisationGroupId",
        "TopLevelOrganisationGroupId",
        "topLevelOrganisationGroupId",
        "RootOrganisationGroupId",
        "rootOrganisationGroupId",
    )


def _group_target(group: dict[str, Any]) -> dict[str, Any] | None:
    gid = _group_field_int(group, "GroupId", "groupId")
    if gid is None:
        return None
    name = str(
        group.get("Name")
        or group.get("name")
        or group.get("GroupName")
        or group.get("groupName")
        or ""
    ).strip()
    return {"GroupId": gid, "Name": name or str(gid)}


def _children_by_parent(by_id: dict[int, dict[str, Any]]) -> dict[int, list[int]]:
    from collections import defaultdict

    children: dict[int, list[int]] = defaultdict(list)
    for gid, group in by_id.items():
        parent = _group_parent_id(group)
        if parent is not None:
            children[parent].append(gid)
    return children


def _descendant_ids(org_id: int, children_by_parent: dict[int, list[int]]) -> set[int]:
    out: set[int] = set()
    stack = list(children_by_parent.get(org_id, []))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children_by_parent.get(cur, []))
    return out


def _belongs_to_org(
    gid: int,
    org_id: int,
    *,
    by_id: dict[int, dict[str, Any]],
    descendants: set[int],
) -> bool:
    if gid == org_id:
        return False
    if gid in descendants or _is_descendant_of(gid, org_id, by_id):
        return True
    group = by_id.get(gid, {})
    return _group_organisation_id(group) == org_id


def _flatten_subgroup_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "Groups",
        "groups",
        "SubGroups",
        "subGroups",
        "OrganisationGroups",
        "organisationGroups",
        "Children",
        "children",
    ):
        val = data.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    return [data]


def _flatten_group_summary_tree(data: Any) -> list[dict[str, Any]]:
    """Flatten MiX ``GroupSummary`` JSON (nested ``SubGroups``) into node dicts."""
    out: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        out.append(node)
        subs = node.get("SubGroups") or node.get("subGroups") or []
        if isinstance(subs, list):
            for child in subs:
                _walk(child)

    _walk(data)
    return out


_MIX_SITE_TARGETS_KEY = "mix_dhl_site_targets"
_MIX_ORG_GROUPS_KEY = "mix_organisation_groups"


def _save_organisation_groups_cache(groups: list[dict[str, Any]]) -> None:
    try:
        import neon_meta_store

        neon_meta_store.set_meta(_MIX_ORG_GROUPS_KEY, {"groups": groups})
    except Exception as exc:  # noqa: BLE001
        log.debug("MiX: organisation groups cache save skipped: %s", exc)


def _load_organisation_groups_cache() -> list[dict[str, Any]] | None:
    try:
        import neon_meta_store

        meta = neon_meta_store.get_meta(_MIX_ORG_GROUPS_KEY)
    except Exception as exc:  # noqa: BLE001
        log.debug("MiX: organisation groups cache load skipped: %s", exc)
        return None
    if not meta:
        return None
    groups = meta.get("groups")
    if isinstance(groups, list) and groups:
        return [g for g in groups if isinstance(g, dict)]
    return None


def _save_site_targets_cache(
    targets: list[dict[str, Any]],
    *,
    org_id: int,
    site_prefix: str,
) -> None:
    try:
        import neon_meta_store

        neon_meta_store.set_meta(
            _MIX_SITE_TARGETS_KEY,
            {
                "org_id": org_id,
                "site_prefix": site_prefix,
                "targets": targets,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("MiX: site target cache save skipped: %s", exc)


def _load_site_targets_cache(org_id: int, site_prefix: str) -> list[dict[str, Any]] | None:
    try:
        import neon_meta_store

        meta = neon_meta_store.get_meta(_MIX_SITE_TARGETS_KEY)
    except Exception as exc:  # noqa: BLE001
        log.debug("MiX: site target cache load skipped: %s", exc)
        return None
    if not meta:
        return None
    if meta.get("org_id") != org_id or meta.get("site_prefix") != site_prefix:
        return None
    targets = meta.get("targets")
    if isinstance(targets, list) and targets:
        return [t for t in targets if isinstance(t, dict) and t.get("GroupId") is not None]
    return None


def _site_targets_from_health_snapshot(site_prefix: str) -> list[dict[str, Any]]:
    """Rebuild DHL site groups from the last saved mix_health snapshot."""
    df = None
    try:
        from data import cache_peek

        df = cache_peek("mix_health")
    except Exception:
        df = None
    if df is None:
        try:
            from neon_snapshot_store import load_frame

            loaded = load_frame("mix_health")
            if loaded:
                df = loaded[0]
        except Exception:
            df = None
    if df is None or getattr(df, "empty", True) or "GroupId" not in df.columns:
        return []
    name_col = "GroupName" if "GroupName" in df.columns else None
    out: dict[int, dict[str, Any]] = {}
    for _, row in df.drop_duplicates(subset=["GroupId"]).iterrows():
        gid = _normalize_mix_id(row.get("GroupId"))
        if gid is None:
            continue
        name = str(row.get(name_col, gid)) if name_col else str(gid)
        if not _site_name_has_prefix(name, site_prefix):
            continue
        out[gid] = {"GroupId": gid, "Name": name}
    return list(out.values())


def _mix_http_get_resilient(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 90,
) -> requests.Response:
    """GET with short retries on transient MiX server errors (500/502/503)."""
    last: requests.Response | None = None
    for attempt in range(3):
        resp = _mix_http("get", url, headers=headers, timeout=timeout)
        if resp.status_code not in (500, 502, 503, 504):
            return resp
        last = resp
        wait = 2.0 * (attempt + 1)
        log.warning(
            "MiX: transient HTTP %s on %s; retry in %.0fs (%s/3)",
            resp.status_code,
            url.split("/api/", 1)[-1][:60],
            wait,
            attempt + 1,
        )
        time.sleep(wait)
    return last if last is not None else resp


def fetch_subgroups_for_org(org_id: int) -> list[dict[str, Any]]:
    """Fetch organisation hierarchy via official MiX subgroups endpoint."""
    token = ensure_bearer_token()
    api = api_base_url().rstrip("/")
    headers = _api_headers(token)
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()

    # Official route: GET api/organisationgroups/subgroups/{groupId}
    for id_str in _mix_id_api_variants(org_id):
        url = f"{api}/api/organisationgroups/subgroups/{id_str}"
        resp = _mix_http_get_resilient(url, headers=headers, timeout=90)
        if resp.status_code != 200:
            log.warning(
                "MiX: subgroups %s -> HTTP %s (%s)",
                id_str,
                resp.status_code,
                (resp.text or "")[:120],
            )
            continue
        nodes = _flatten_group_summary_tree(resp.json())
        for node in nodes:
            target = _group_target(node)
            if not target:
                continue
            gid = int(target["GroupId"])
            if gid in seen:
                continue
            seen.add(gid)
            merged.append(node)
        if merged:
            log.info(
                "MiX: subgroups tree for org %s via id %s: %s node(s)",
                org_id,
                id_str,
                len(merged),
            )
            break

    if not merged:
        log.warning(
            "MiX: subgroups API returned 0 nodes for org %s (tried %s)",
            org_id,
            _mix_id_api_variants(org_id),
        )
    return merged


def fetch_org_site_ids(org_id: int) -> dict[int, int | None]:
    """Return site ``GroupId`` -> legacy id via MiX ``siteswithlegacyid`` endpoint."""
    token = ensure_bearer_token()
    api = api_base_url().rstrip("/")
    headers = _api_headers(token)
    for id_str in _mix_id_api_variants(org_id):
        url = f"{api}/api/organisationgroups/siteswithlegacyid/{id_str}"
        resp = _mix_http_get_resilient(url, headers=headers, timeout=90)
        if resp.status_code != 200:
            log.warning(
                "MiX: siteswithlegacyid %s -> HTTP %s (%s)",
                id_str,
                resp.status_code,
                (resp.text or "")[:120],
            )
            continue
        data = resp.json()
        if not isinstance(data, dict) or not data:
            continue
        out: dict[int, int | None] = {}
        for raw_key, legacy in data.items():
            gid = _normalize_mix_id(raw_key)
            if gid is None:
                continue
            out[gid] = legacy
        if out:
            log.info(
                "MiX: siteswithlegacyid for org %s via id %s: %s site(s)",
                org_id,
                id_str,
                len(out),
            )
            return out
    return {}


def fetch_group_detail(group_id: int) -> dict[str, Any] | None:
    """Fetch a single group record (name/type) by id."""
    token = ensure_bearer_token()
    api = api_base_url().rstrip("/")
    for id_str in _mix_id_api_variants(group_id):
        url = f"{api}/api/organisationgroups/group/{id_str}"
        resp = _mix_http("get", url, headers=_api_headers(token), timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    return None


def _group_name_lookup(
    gid: int,
    *,
    by_id: dict[int, dict[str, Any]],
    tree_nodes: list[dict[str, Any]],
) -> str | None:
    target = _group_target(by_id.get(gid, {}))
    if target and target.get("Name"):
        return str(target["Name"])
    for node in tree_nodes:
        target = _group_target(node)
        if target and int(target["GroupId"]) == gid and target.get("Name"):
            return str(target["Name"])
    return None


def _sites_from_legacy_id_api(
    org_id: int,
    site_prefix: str,
    groups: list[dict[str, Any]],
    tree_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    site_map = fetch_org_site_ids(org_id)
    if not site_map:
        return []
    by_id = _groups_index(groups)
    out: list[dict[str, Any]] = []
    for gid in sorted(site_map.keys()):
        if gid == org_id:
            continue
        name = _group_name_lookup(gid, by_id=by_id, tree_nodes=tree_nodes)
        if not name:
            detail = fetch_group_detail(gid)
            target = _group_target(detail) if detail else None
            name = str(target["Name"]) if target and target.get("Name") else None
        if not name or not _site_name_has_prefix(name, site_prefix):
            continue
        out.append({"GroupId": gid, "Name": name})
    if out:
        log.info(
            "MiX: siteswithlegacyid matched %s DHL site(s) under org %s",
            len(out),
            org_id,
        )
    return _dedupe_group_targets(out)


def _dedupe_group_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for target in targets:
        try:
            gid = int(target["GroupId"])
        except (KeyError, TypeError, ValueError):
            continue
        out[gid] = {"GroupId": gid, "Name": str(target.get("Name", gid))}
    merged = list(out.values())
    merged.sort(key=lambda g: str(g.get("Name", "")).lower())
    return merged


def _flat_dhl_named_groups(
    groups: list[dict[str, Any]],
    site_prefix: str,
) -> list[dict[str, Any]]:
    """All visible organisation groups whose name includes the DHL prefix segment."""
    out: list[dict[str, Any]] = []
    for group in groups:
        target = _group_target(group)
        if not target:
            continue
        if _site_name_has_prefix(str(target["Name"]), site_prefix):
            out.append(target)
    return _dedupe_group_targets(out)


def _sites_under_org_id(
    groups: list[dict[str, Any]],
    org_id: int,
    site_prefix: str,
) -> list[dict[str, Any]]:
    """Sites under organisation ``org_id`` whose name includes the DHL prefix."""
    by_id = _groups_index(groups)
    children = _children_by_parent(by_id)
    descendants = _descendant_ids(org_id, children)

    def _collect(
        source_groups: list[dict[str, Any]],
        *,
        skip_org_check: bool = False,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        for group in source_groups:
            target = _group_target(group)
            if not target:
                continue
            gid_int = int(target["GroupId"])
            name = str(target["Name"])
            if gid_int == org_id:
                continue
            if not _site_name_has_prefix(name, site_prefix):
                continue
            if not skip_org_check and not _belongs_to_org(
                gid_int, org_id, by_id=by_id, descendants=descendants
            ):
                continue
            if gid_int in seen:
                continue
            seen.add(gid_int)
            out.append(target)
        out.sort(key=lambda g: str(g.get("Name", "")).lower())
        return out

    candidates: list[dict[str, Any]] = []

    # 1) MiX subgroup API — flat /organisationgroups often omits nested DHL sites.
    try:
        api_groups = fetch_subgroups_for_org(org_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("MiX: subgroup API failed for org %s: %s", org_id, exc)
        api_groups = []
    if api_groups:
        api_matched = _collect(api_groups, skip_org_check=True)
        if api_matched:
            log.info(
                "MiX: subgroup API returned %s DHL site(s) under org %s",
                len(api_matched),
                org_id,
            )
            candidates.extend(api_matched)

    # 1b) Flat site-id map when subgroup tree is empty or incomplete.
    if not candidates:
        legacy_matched = _sites_from_legacy_id_api(org_id, site_prefix, groups, api_groups)
        if legacy_matched:
            candidates.extend(legacy_matched)

    # 2) Hierarchy match on the flat organisationgroups list.
    candidates.extend(_collect(groups))

    # 3) Flat DHL prefix widen when parent metadata is incomplete.
    dhl_named = [
        target
        for group in groups
        if (target := _group_target(group))
        and int(target["GroupId"]) != org_id
        and _site_name_has_prefix(str(target["Name"]), site_prefix)
    ]
    if dhl_named:
        org_scoped = [
            target
            for target in dhl_named
            if _group_organisation_id(by_id.get(int(target["GroupId"]), {})) in (None, org_id)
            or _belongs_to_org(int(target["GroupId"]), org_id, by_id=by_id, descendants=descendants)
        ]
        if len(org_scoped) <= len(_collect(groups)) and len(dhl_named) > len(org_scoped):
            org_scoped = dhl_named
        if org_scoped:
            if len(org_scoped) > len(_collect(groups)):
                log.warning(
                    "MiX: parent links incomplete — using %s flat DHL site group(s) for org %s",
                    len(org_scoped),
                    org_id,
                )
            candidates.extend(org_scoped)

    merged = _dedupe_group_targets(candidates)
    if merged:
        return merged

    log.warning(
        "MiX: site resolution empty for org %s (flat=%s, dhl_named=%s, api_nodes=%s)",
        org_id,
        len(groups),
        len(dhl_named),
        len(api_groups),
    )
    return []


def _site_name_has_prefix(name: str, prefix: str) -> bool:
    """Match ``DHL Site``, ``EABL - DHL Site``, or any name containing the DHL token."""
    text = str(name or "").strip()
    if not text:
        return False
    needle = prefix.strip()
    if not needle:
        return True
    upper = text.upper()
    token = needle.upper()
    if upper.startswith(token):
        return True
    padded = f" {upper} "
    if f" {token} " in padded or padded.rstrip().endswith(f" {token}"):
        return True
    for sep in (" - ", "- ", "-", " – ", " — "):
        if f"{sep}{token}" in upper:
            return True
    return token in upper


def _is_descendant_of(gid: int, ancestor_id: int, by_id: dict[int, dict[str, Any]]) -> bool:
    if gid == ancestor_id:
        return False
    seen: set[int] = set()
    cur: int | None = gid
    while cur is not None and cur not in seen:
        parent = _group_parent_id(by_id.get(cur, {}))
        if parent == ancestor_id:
            return True
        seen.add(cur)
        if parent is None:
            break
        cur = parent
    return False


def _groups_index(groups: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for group in groups:
        gid = _group_field_int(group, "GroupId", "groupId")
        if gid is None:
            continue
        by_id[gid] = group
    return by_id


def _sites_under_org_with_prefix(
    groups: list[dict[str, Any]],
    org_contains: str,
    site_prefix: str,
) -> list[dict[str, Any]]:
    by_id = _groups_index(groups)
    org_needle = org_contains.strip().lower()
    org_roots = {
        gid
        for gid, group in by_id.items()
        if org_needle in str(group.get("Name", "")).lower()
    }

    def _under_org(gid: int) -> bool:
        if gid in org_roots:
            return True
        seen: set[int] = set()
        cur: int | None = gid
        while cur is not None and cur not in seen:
            if cur in org_roots:
                return True
            seen.add(cur)
            group = by_id.get(cur)
            if not group:
                break
            if org_needle in str(group.get("Name", "")).lower():
                return True
            cur = _group_parent_id(group)
        return False

    out: list[dict[str, Any]] = []
    for group in groups:
        gid = group.get("GroupId")
        if gid is None:
            continue
        name = str(group.get("Name", "")).strip()
        if not _site_name_has_prefix(name, site_prefix):
            continue
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        if not _under_org(gid_int):
            continue
        out.append({"GroupId": gid_int, "Name": name})

    out.sort(key=lambda g: str(g.get("Name", "")).lower())
    return out


def group_ids() -> list[int]:
    return [int(g["GroupId"]) for g in resolve_group_targets()]


def resolve_organisation_id() -> int:
    """MiX library events require an OrganisationGroup id, not a site/group id from MIX_GROUP_IDS."""
    explicit = (_env("MIX_ORGANISATION_ID") or "").strip()
    if explicit:
        oid = _normalize_mix_id(explicit)
        if oid is None:
            raise ValueError(f"MIX_ORGANISATION_ID is not a valid MiX id: {explicit!r}")
        return oid
    groups = fetch_organisation_groups()
    configured = set(group_ids())
    for g in groups:
        gid = _group_field_int(g, "GroupId", "groupId")
        if gid is not None and gid in configured and g.get("Type") == "OrganisationGroup":
            return gid
    for g in groups:
        gid = _group_field_int(g, "GroupId", "groupId")
        if gid is not None and g.get("Type") == "OrganisationGroup":
            return gid
    for g in groups:
        gid = _group_field_int(g, "GroupId", "groupId")
        if gid is None:
            continue
        if g.get("Type") in ("OrganisationSubGroup", "SiteGroup", "DefaultSite"):
            continue
        if g.get("Type") in ("MultiLevelOrg", "RsoGroup", "DealerGroup"):
            return gid
    return group_ids()[0]


def _safe_get(record: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if k in record and record[k] is not None:
            return str(record[k])
    return default


def _throttle_mix_api() -> None:
    """MiX ZA allows ~20 API calls/min — stay under that when scanning many groups."""
    try:
        max_per_min = int(_env("MIX_MAX_CALLS_PER_MINUTE", "15") or "15")
    except ValueError:
        max_per_min = 15
    max_per_min = max(5, min(max_per_min, 19))
    with _rate_lock:
        now = time.time()
        while _rate_times and now - _rate_times[0] > 60.0:
            _rate_times.popleft()
        if len(_rate_times) >= max_per_min:
            wait = 60.0 - (now - _rate_times[0]) + 0.25
            if wait > 0:
                time.sleep(wait)
        _rate_times.append(time.time())


def _mix_http(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 90,
    **kwargs: Any,
) -> requests.Response:
    """MiX API call with client-side throttle and 429 backoff."""
    last: requests.Response | None = None
    for attempt in range(4):
        _throttle_mix_api()
        resp = getattr(_session, method.lower())(url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code != 429:
            return resp
        last = resp
        with _rate_lock:
            oldest = _rate_times[0] if _rate_times else time.time()
        wait = max(5.0, 61.0 - (time.time() - oldest))
        log.warning(
            "MiX rate limited (429) on %s; retry in %.0fs (attempt %s/4)",
            url.split("/api/", 1)[-1][:60],
            wait,
            attempt + 1,
        )
        time.sleep(wait)
    if last is None:
        raise RuntimeError("MiX request failed before any response")
    return last


def _post_positions_for_groups(
    group_ids_batch: list[int],
    *,
    quantity: int,
    cached_since: str | None,
    ensure_reverse_geocoded: bool,
    token: str,
    api_url: str,
) -> list[dict[str, Any]]:
    url = f"{api_url}/api/positions/groups/latest/{quantity}"
    params: dict[str, str] = {}
    if cached_since:
        params["cachedSince"] = cached_since
    params["ensureReverseGeocoded"] = str(ensure_reverse_geocoded).lower()
    resp = _mix_http(
        "post",
        url,
        headers=_api_headers(token),
        params=params,
        json=group_ids_batch,
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MiX positions failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"MiX positions unexpected response: {type(data)}")
    return data


def fetch_latest_positions(
    *,
    quantity: int | None = None,
    cached_since: str | None = None,
    ensure_reverse_geocoded: bool | None = None,
) -> list[dict[str, Any]]:
    creds = _load_server_creds()
    api_url = creds["ApiUrl"].rstrip("/")
    token = ensure_bearer_token()
    qty = quantity if quantity is not None else int(_env("MIX_QUANTITY", "1") or "1")
    if ensure_reverse_geocoded is None:
        ensure_reverse_geocoded = _env_truthy("MIX_ENSURE_REVERSE_GEOCODED", "1")
    if cached_since is None:
        cached_since = _env("MIX_CACHED_SINCE") or None

    targets = resolve_group_targets()
    log.info("MiX: fetching positions for %s group(s)", len(targets))

    # One group per request is most reliable across MiX ZA tenants.
    def _one_group(target: dict[str, Any]) -> list[dict[str, Any]]:
        gid = int(target["GroupId"])
        try:
            rows = _post_positions_for_groups(
                [gid],
                quantity=qty,
                cached_since=cached_since,
                ensure_reverse_geocoded=ensure_reverse_geocoded,
                token=token,
                api_url=api_url,
            )
        except Exception as e:
            log.warning("MiX: skip group %s (%s): %s", gid, target.get("Name"), e)
            return []
        for row in rows:
            row.setdefault("GroupId", gid)
            row.setdefault("GroupName", target.get("Name", ""))
        return rows

    workers = max(1, min(int(_env("MIX_GROUP_WORKERS", "2") or "2"), 4))
    if len(targets) == 1 or workers == 1:
        out: list[dict[str, Any]] = []
        for t in targets:
            out.extend(_one_group(t))
        return out

    merged: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one_group, t): t for t in targets}
        for fut in as_completed(futs):
            merged.extend(fut.result())
    log.info("MiX: %s position record(s) across %s groups", len(merged), len(targets))
    return merged


def _health_position_batch_size() -> int:
    try:
        batch_size = int(_env("MIX_HEALTH_POS_BATCH", "20") or "20")
    except ValueError:
        batch_size = 20
    return max(1, min(batch_size, 50))


def _health_position_batches(group_ids: list[int]) -> list[list[int]]:
    batch_size = _health_position_batch_size()
    return [group_ids[i : i + batch_size] for i in range(0, len(group_ids), batch_size)]


def fetch_comm_check_positions(
    targets: list[dict[str, Any]] | None = None,
    *,
    api_url: str | None = None,
    token: str | None = None,
    quantity: int = 1,
) -> list[dict[str, Any]]:
    """Latest position timestamps for non-downloading checks (batched, no reverse geocode)."""
    creds = _load_server_creds()
    api = (api_url or creds["ApiUrl"]).rstrip("/")
    tok = token or ensure_bearer_token()
    qty = max(1, quantity)
    group_name_by_id: dict[int, str] = {}

    if targets is None:
        targets = resolve_group_targets()
    gids = [int(t["GroupId"]) for t in targets if t.get("GroupId") is not None]
    for target in targets:
        try:
            gid = int(target["GroupId"])
        except (TypeError, ValueError):
            continue
        group_name_by_id[gid] = str(target.get("Name") or "")

    if not gids:
        return []

    log.info(
        "MiX health: fetching comm-check positions for %s group(s) in batches of %s",
        len(gids),
        _health_position_batch_size(),
    )

    def _fetch_batch(batch: list[int]) -> list[dict[str, Any]]:
        try:
            rows = _post_positions_for_groups(
                batch,
                quantity=qty,
                cached_since=None,
                ensure_reverse_geocoded=False,
                token=tok,
                api_url=api,
            )
        except Exception as exc:
            log.warning("MiX health: position batch %s failed (%s) — retrying per group", batch[:3], exc)
            rows = []
            for gid in batch:
                try:
                    rows.extend(
                        _post_positions_for_groups(
                            [gid],
                            quantity=qty,
                            cached_since=None,
                            ensure_reverse_geocoded=False,
                            token=tok,
                            api_url=api,
                        )
                    )
                except Exception as inner:
                    log.warning("MiX health: skip group %s positions: %s", gid, inner)
        for row in rows:
            try:
                gid = int(row.get("GroupId") or row.get("SiteId") or 0)
            except (TypeError, ValueError):
                gid = 0
            if gid:
                row.setdefault("GroupId", gid)
                row.setdefault("GroupName", group_name_by_id.get(gid, ""))
        return rows

    batches = _health_position_batches(gids)
    try:
        workers = int(_env("MIX_HEALTH_POS_WORKERS", "6") or "6")
    except ValueError:
        workers = 6
    workers = max(1, min(workers, 12))

    if len(batches) == 1 or workers == 1:
        merged: list[dict[str, Any]] = []
        for batch in batches:
            merged.extend(_fetch_batch(batch))
    else:
        merged = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_fetch_batch, batch) for batch in batches]
            for fut in as_completed(futs):
                merged.extend(fut.result())

    log.info("MiX health: %s comm-check position record(s) from %s group(s)", len(merged), len(gids))
    return merged


def fetch_assets_for_group(api_url: str, token: str, group_id: int) -> dict[str, dict[str, Any]]:
    """Probe known MiX asset endpoints; return AssetId -> record map."""
    base = api_url.rstrip("/")
    headers = _api_headers(token)
    candidates = [
        ("GET", f"{base}/api/assets/groups/{group_id}"),
        ("GET", f"{base}/api/assets/group/{group_id}"),
        ("POST", f"{base}/api/assets/groups"),
        ("GET", f"{base}/api/assets?groupId={group_id}"),
        ("GET", f"{base}/api/v1/assets/groups/{group_id}"),
        ("GET", f"{base}/api/groups/{group_id}/assets"),
    ]
    for method, url in candidates:
        try:
            if method == "GET":
                resp = _session.get(url, headers=headers, timeout=30)
            else:
                resp = _session.post(url, headers=headers, json=[group_id], timeout=30)
        except requests.RequestException as e:
            log.debug("MiX assets %s %s: %s", method, url, e)
            continue
        if resp.status_code != 200:
            continue
        assets = resp.json()
        if not isinstance(assets, list) or not assets:
            continue
        log.info("MiX: assets via %s %s (%s rows)", method, url, len(assets))
        out: dict[str, dict[str, Any]] = {}
        for a in assets:
            aid = a.get("AssetId", a.get("assetId"))
            if aid is not None:
                out[str(aid)] = a
        return out
    log.debug("MiX: no assets endpoint for group %s", group_id)
    return {}


def _tacho_speed_line() -> str:
    return _env("MIX_TACHO_SPEED_LINE", "F1") or "F1"


def _tacho_rpm_line() -> str:
    return _env("MIX_TACHO_RPM_LINE", "F2") or "F2"


def _tacho_minutes() -> int:
    try:
        return max(1, min(int(_env("MIX_TACHO_MINUTES", "59") or "59"), 59))
    except ValueError:
        return 59


def _tacho_key_for_line(definitions: list[dict[str, Any]], line_name: str) -> int | None:
    for item in definitions:
        if str(item.get("LineName", "")).strip() == line_name:
            key = item.get("Key")
            if key is not None:
                return int(key)
    return None


def _tacho_value_at_key(interval: dict[str, Any], key: int | None) -> float | None:
    if key is None:
        return None
    for item in interval.get("Data") or []:
        if item.get("Key") == key:
            try:
                return float(item.get("Value"))
            except (TypeError, ValueError):
                return None
    return None


def fetch_asset_tacho(
    api_url: str,
    token: str,
    asset_id: int,
    *,
    minutes: int | None = None,
) -> dict[str, Any] | None:
    """Fetch tacho intervals for one asset (MiX allows up to 1 hour per request)."""
    window = minutes if minutes is not None else _tacho_minutes()
    to_dt = datetime.now(timezone.utc)
    fr_dt = to_dt - timedelta(minutes=max(1, min(window, 59)))
    fr = fr_dt.strftime("%Y%m%d%H%M%S")
    to = to_dt.strftime("%Y%m%d%H%M%S")
    url = f"{api_url.rstrip('/')}/api/tachos/asset/{asset_id}/range/from/{fr}/to/{to}"
    _throttle_mix_api()
    try:
        resp = _session.get(url, headers=_api_headers(token), timeout=45)
    except requests.RequestException as e:
        log.debug("MiX tacho asset %s: %s", asset_id, e)
        return None
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        log.debug("MiX tacho asset %s failed: %s", asset_id, resp.status_code)
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None


def analyze_tacho(tacho: dict[str, Any] | None) -> dict[str, Any]:
    """Parse tacho intervals: F1 = speed (km/h), F2 = RPM.

    ``has_rpm_feed`` / ``has_speed_feed`` mean the tacho channel exists and returned
    readings (including 0 when the engine is off). Use those flags for health checks,
    not whether the latest RPM is > 0.
    """
    out: dict[str, Any] = {
        "has_speed_feed": False,
        "has_rpm_feed": False,
        "speed_kmh": None,
        "rpm": None,
        "max_speed_kmh": None,
        "speed_jump_kmh": None,
        "max_rpm": None,
        "rpm_std": None,
        "interval_count": 0,
        "interval_time": "",
    }
    if not tacho:
        return out

    definitions = tacho.get("ParameterDefinitions") or []
    speed_key = _tacho_key_for_line(definitions, _tacho_speed_line())
    rpm_key = _tacho_key_for_line(definitions, _tacho_rpm_line())
    intervals = tacho.get("Intervals") or []
    out["interval_count"] = len(intervals)
    if not intervals:
        return out

    speeds: list[float] = []
    rpms: list[float] = []
    for interval in intervals:
        if speed_key is not None:
            speed = _tacho_value_at_key(interval, speed_key)
            if speed is not None and speed >= 0:
                speeds.append(speed)
        if rpm_key is not None:
            rpm = _tacho_value_at_key(interval, rpm_key)
            if rpm is not None and rpm >= 0:
                rpms.append(rpm)

    latest = intervals[-1]
    out["interval_time"] = str(latest.get("IntervalDateTime") or "")

    if speeds:
        out["has_speed_feed"] = True
        out["speed_kmh"] = speeds[-1]
        out["max_speed_kmh"] = max(speeds)
        if len(speeds) > 1:
            jump = 0.0
            for a, b in zip(speeds, speeds[1:]):
                jump = max(jump, abs(a - b))
            out["speed_jump_kmh"] = jump

    if rpms:
        out["has_rpm_feed"] = True
        out["rpm"] = rpms[-1]
        out["max_rpm"] = max(rpms)
        active = [r for r in rpms if r > 0]
        if len(active) > 1:
            out["rpm_std"] = float(pd.Series(active).std(ddof=0))
        elif len(active) == 1:
            out["rpm_std"] = 0.0

    return out


def parse_tacho_snapshot(tacho: dict[str, Any] | None) -> dict[str, Any]:
    """Latest tacho speed (F1) and RPM (F2) from the most recent interval."""
    a = analyze_tacho(tacho)
    return {
        "speed_kmh": a["speed_kmh"],
        "rpm": a["rpm"],
        "interval_time": a["interval_time"],
        "interval_count": a["interval_count"],
        "has_speed_feed": a["has_speed_feed"],
        "has_rpm_feed": a["has_rpm_feed"],
    }


def tacho_interval_stats(
    tacho: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Return (latest_speed, max_speed, speed_jump, max_rpm, rpm_std) from tacho intervals."""
    a = analyze_tacho(tacho)
    return (
        a["speed_kmh"],
        a["max_speed_kmh"],
        a["speed_jump_kmh"],
        a["max_rpm"],
        a["rpm_std"],
    )


_tacho_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_tacho_cache_lock = threading.Lock()


def clear_tacho_cache() -> None:
    """Drop in-process tacho responses (call on dashboard refresh)."""
    with _tacho_cache_lock:
        _tacho_cache.clear()


def _tacho_cache_ttl_sec() -> int:
    try:
        return max(60, int(_env("MIX_TACHO_CACHE_SEC", "300") or "300"))
    except ValueError:
        return 300


def fetch_tacho_for_assets(
    api_url: str,
    token: str,
    asset_ids: list[int],
    *,
    minutes: int | None = None,
    force: bool = False,
) -> dict[str, dict[str, Any] | None]:
    """Fetch tacho payloads for many assets; reuse a short-lived in-process cache."""
    if not asset_ids:
        return {}

    ttl = _tacho_cache_ttl_sec()
    now = time.time()
    out: dict[str, dict[str, Any] | None] = {}
    missing: list[int] = []

    with _tacho_cache_lock:
        for aid in asset_ids:
            key = str(aid)
            if not force:
                cached = _tacho_cache.get(key)
                if cached and now - cached[0] < ttl:
                    out[key] = cached[1]
                    continue
            missing.append(aid)

    for aid in missing:
        tacho = fetch_asset_tacho(api_url, token, aid, minutes=minutes)
        key = str(aid)
        out[key] = tacho
        with _tacho_cache_lock:
            _tacho_cache[key] = (time.time(), tacho)

    return out


def enrich_dataframe_with_tacho(
    df: pd.DataFrame,
    api_url: str,
    token: str,
    *,
    asset_ids: list[int] | None = None,
) -> pd.DataFrame:
    """Overlay SpeedKmh and Rpm from tacho data (F1/F2) for each asset."""
    if df is None or df.empty or not _env_truthy("MIX_TACHO_ENRICH_POSITIONS", "1"):
        return df

    out = df.copy()
    if "Rpm" not in out.columns:
        out["Rpm"] = ""

    ids = asset_ids
    if ids is None:
        ids = []
        for raw in out["AssetId"].astype(str):
            try:
                ids.append(int(raw))
            except ValueError:
                continue

    raw_by_asset = fetch_tacho_for_assets(api_url, token, ids)
    snapshots = {key: analyze_tacho(raw) for key, raw in raw_by_asset.items()}

    filled_speed = 0
    filled_rpm = 0
    for idx, row in out.iterrows():
        snap = snapshots.get(str(row.get("AssetId", "")).strip(), {})
        speed = snap.get("speed_kmh")
        rpm = snap.get("rpm")
        if speed is not None:
            out.at[idx, "SpeedKmh"] = str(speed)
            filled_speed += 1
        if snap.get("has_rpm_feed"):
            val = rpm if rpm is not None else 0
            out.at[idx, "Rpm"] = str(int(val) if val == int(val) else val)
            filled_rpm += 1

    log.info(
        "MiX: tacho enrichment — %s assets, %s with speed, %s with RPM",
        len(ids),
        filled_speed,
        filled_rpm,
    )
    return out


def _parse_event_age_hours(ts: str) -> float | None:
    if not ts or not str(ts).strip():
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except ValueError:
            continue
    return None


def positions_to_dataframe(
    raw: list[dict[str, Any]],
    asset_lookup: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    asset_lookup = asset_lookup or {}
    tz = timezone(timedelta(hours=int(_env("MIX_DISPLAY_TZ_OFFSET_HOURS", "3") or "3")))
    run_ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    records: list[dict[str, Any]] = []

    for item in raw:
        asset_id = _safe_get(item, "AssetId", "assetId")
        info = asset_lookup.get(asset_id, {})
        event_time = _safe_get(item, "Timestamp", "EventTime")
        records.append(
            {
                "GroupId": _safe_get(item, "GroupId", "groupId"),
                "GroupName": _safe_get(item, "GroupName", "groupName"),
                "AssetId": asset_id,
                "AssetName": _safe_get(info, "Description", "description"),
                "Registration": _safe_get(info, "RegistrationNumber", "registrationNumber"),
                "Make": _safe_get(info, "Make", "make"),
                "DriverId": _safe_get(item, "DriverId", "driverId"),
                "Latitude": _safe_get(item, "Latitude", "latitude"),
                "Longitude": _safe_get(item, "Longitude", "longitude"),
                "SpeedKmh": _safe_get(item, "SpeedKilometresPerHour", "speedKilometresPerHour"),
                "Rpm": "",
                "Heading": _safe_get(item, "Heading", "heading"),
                "AltitudeM": _safe_get(item, "AltitudeMetres", "altitudeMetres"),
                "Address": _safe_get(item, "FormattedAddress", "formattedAddress"),
                "GpsSource": _safe_get(item, "Source", "source"),
                "Satellites": _safe_get(item, "NumberOfSatellites", "numberOfSatellites"),
                "EventTime": event_time,
                "AgeHours": _parse_event_age_hours(event_time),
                "LastUpdated": run_ts,
            }
        )

    if not records:
        return empty_positions_dataframe()
    df = pd.DataFrame(records)
    for col in _MIX_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[_MIX_COLUMNS]


def empty_positions_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_MIX_COLUMNS)


def _build_asset_lookup(api_url: str, token: str, gids: list[int] | None = None) -> dict[str, dict[str, Any]]:
    """AssetId (str) -> asset record from ``/api/assets/group/{id}``."""
    lookup: dict[str, dict[str, Any]] = {}
    for gid in gids or group_ids():
        for a in _fetch_group_assets_list(api_url, token, gid):
            aid = a.get("AssetId", a.get("assetId"))
            if aid is not None:
                lookup[str(aid)] = a
    return lookup


def enrich_positions_dataframe(df: pd.DataFrame, asset_lookup: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Fill AssetName / Registration / Make from asset metadata when missing."""
    if df is None or df.empty or not asset_lookup:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        info = asset_lookup.get(str(row.get("AssetId", "")).strip(), {})
        if not info:
            continue
        if not str(row.get("AssetName", "")).strip():
            out.at[idx, "AssetName"] = _safe_get(info, "Description", "description")
        if not str(row.get("Registration", "")).strip():
            out.at[idx, "Registration"] = _safe_get(
                info, "RegistrationNumber", "registrationNumber", "Registration", "registration"
            )
        if not str(row.get("Make", "")).strip():
            out.at[idx, "Make"] = _safe_get(info, "Make", "make")
    return out


def load_positions_with_metadata() -> pd.DataFrame:
    """Fetch MiX positions and asset names/registrations (no tacho yet)."""
    creds = _load_server_creds()
    api_url = creds["ApiUrl"]
    token = ensure_bearer_token()
    raw = fetch_latest_positions()
    asset_lookup = _build_asset_lookup(api_url, token)
    if asset_lookup:
        log.info("MiX: loaded metadata for %s asset(s)", len(asset_lookup))
    else:
        log.warning("MiX: no asset metadata returned — names/registrations will be blank")
    df = positions_to_dataframe(raw, asset_lookup)
    df = enrich_positions_dataframe(df, asset_lookup)
    named = int(df["AssetName"].astype(str).str.strip().ne("").sum()) if not df.empty else 0
    log.info("MiX: positions metadata ready — %s rows, %s with asset names", len(df), named)
    return df


def load_positions_dataframe() -> pd.DataFrame:
    """Fetch latest MiX positions (+ asset names/registrations + tacho) as a DataFrame."""
    creds = _load_server_creds()
    api_url = creds["ApiUrl"]
    token = ensure_bearer_token()
    df = load_positions_with_metadata()
    if not df.empty:
        asset_ids = [int(a) for a in df["AssetId"].astype(str) if str(a).strip().isdigit()]
        df = enrich_dataframe_with_tacho(df, api_url, token, asset_ids=asset_ids)
    named = int(df["AssetName"].astype(str).str.strip().ne("").sum()) if not df.empty else 0
    log.info("MiX: positions ready — %s rows, %s with asset names", len(df), named)
    return df


def _fetch_group_assets_list(api_url: str, token: str, group_id: int) -> list[dict[str, Any]]:
    """Return assets for a MiX group (preferred endpoint for ZA)."""
    base = api_url.rstrip("/")
    headers = _api_headers(token)
    for id_str in _mix_id_api_variants(group_id):
        url = f"{base}/api/assets/group/{id_str}"
        resp = _mix_http("get", url, headers=headers, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    return list(fetch_assets_for_group(api_url, token, group_id).values())


def fetch_assets_for_groups_batched(
    api_url: str,
    token: str,
    group_ids: list[int],
    *,
    org_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch assets for many site groups — prefer one org-level GET, else per-site GET."""
    headers = _api_headers(token)
    base = api_url.rstrip("/")
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_sites = set(group_ids) if group_ids else set()

    def _append_assets(assets: list[dict[str, Any]], *, filter_sites: bool) -> None:
        for asset in assets:
            if filter_sites and allowed_sites:
                site = _normalize_mix_id(asset.get("SiteId") or asset.get("siteId"))
                if site is None or site not in allowed_sites:
                    continue
            aid = str(asset.get("AssetId", asset.get("assetId", "")))
            if aid and aid not in seen:
                seen.add(aid)
                merged.append(asset)

    # Fast path: GET /api/assets/group/{orgId} returns all assets under the org tree.
    if org_id is not None:
        for id_str in _mix_id_api_variants(org_id):
            url = f"{base}/api/assets/group/{id_str}"
            resp = _mix_http_get_resilient(url, headers=headers, timeout=120)
            if resp.status_code != 200:
                log.warning("MiX assets/group org %s -> HTTP %s", id_str, resp.status_code)
                continue
            data = resp.json()
            if not isinstance(data, list) or not data:
                continue
            for filter_sites in (bool(allowed_sites), False):
                merged.clear()
                seen.clear()
                _append_assets(data, filter_sites=filter_sites)
                if merged:
                    if not filter_sites and allowed_sites:
                        log.warning(
                            "MiX: org fetch %s returned %s asset(s) without site-id filter (from %s total)",
                            id_str,
                            len(merged),
                            len(data),
                        )
                    else:
                        log.info(
                            "MiX: %s asset(s) for %s DHL site(s) via org fetch %s (from %s total)",
                            len(merged),
                            len(group_ids),
                            id_str,
                            len(data),
                        )
                    return list(merged)
            log.warning(
                "MiX: org asset fetch %s returned %s rows but 0 assets kept",
                id_str,
                len(data),
            )

    if not group_ids:
        return merged
    workers = 1 if len(group_ids) > 10 else max(1, min(int(_env("MIX_GROUP_WORKERS", "2") or "2"), 3))

    def _fetch_one(gid: int) -> list[dict[str, Any]]:
        return _fetch_group_assets_list(api_url, token, gid)

    if workers == 1 or len(group_ids) == 1:
        for gid in group_ids:
            _append_assets(_fetch_one(gid), filter_sites=False)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for assets in ex.map(_fetch_one, group_ids):
                _append_assets(assets, filter_sites=False)

    log.info("MiX: %s asset(s) from %s site group(s) via per-site fetch", len(merged), len(group_ids))
    return merged
