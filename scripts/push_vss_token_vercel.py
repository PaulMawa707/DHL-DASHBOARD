"""Refresh the VSS token and push VSS_TOKEN / VSS_PID to Vercel production.

Designed for:
- Manual runs: ``python scripts/push_vss_token_vercel.py``
- GitHub Actions: scheduled every 12h; skips if Vercel token was updated < 22h ago
- Force refresh: ``python scripts/push_vss_token_vercel.py --force``

Requires VSS credentials in the environment (or ``.env`` in the repo root).
For CI set ``VERCEL_TOKEN`` (https://vercel.com/account/tokens). Locally the Vercel CLI login also works.
Optional overrides: ``VERCEL_PROJECT_ID``, ``VERCEL_ORG_ID``, ``VERCEL_SCOPE``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_TARGETS = ("production",)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def _vercel_config() -> tuple[str, str | None]:
    """Resolve the linked mix-vss project.

    ``.vercel/project.json`` wins over ``VERCEL_PROJECT_ID`` so a stale GitHub
    secret cannot send tokens to a different Vercel project.
    """
    file_project = ""
    file_org: str | None = None
    project_file = ROOT / ".vercel" / "project.json"
    if project_file.is_file():
        data = json.loads(project_file.read_text(encoding="utf-8"))
        file_project = str(data.get("projectId") or "").strip()
        file_org = str(data.get("orgId") or "").strip() or None

    env_project = os.environ.get("VERCEL_PROJECT_ID", "").strip()
    env_org = os.environ.get("VERCEL_ORG_ID", "").strip() or None
    if file_project:
        if env_project and env_project != file_project:
            print(
                f"Ignoring VERCEL_PROJECT_ID={env_project[:16]}…; "
                f"using .vercel/project.json ({file_project[:16]}…)"
            )
        return file_project, file_org or env_org
    if not env_project:
        raise RuntimeError("VERCEL_PROJECT_ID is not set and .vercel/project.json is missing")
    return env_project, env_org


def _vercel_api_token() -> str | None:
    return os.environ.get("VERCEL_TOKEN", "").strip() or None


def _vercel_params(org_id: str | None) -> dict[str, str]:
    return {"teamId": org_id} if org_id else {}


def _vercel_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _vercel_cli_base() -> list[str]:
    import shutil
    import sys

    scope = os.environ.get("VERCEL_SCOPE", "").strip()

    node = shutil.which("node")
    if sys.platform == "win32" and node:
        for root in (
            Path(os.environ.get("APPDATA", "")) / "npm",
            Path(os.environ.get("LOCALAPPDATA", "")) / "npm",
        ):
            vc = root / "node_modules" / "vercel" / "dist" / "vc.js"
            if vc.is_file():
                cmd = [node, str(vc)]
                if scope:
                    cmd.extend(["--scope", scope])
                return cmd
    cmd = ["vercel"]
    if scope:
        cmd.extend(["--scope", scope])
    return cmd


def vercel_env_age_hours(key: str) -> float | None:
    """Hours since ``key`` was last updated on Vercel production (if present)."""
    vercel_token = _vercel_api_token()
    if not vercel_token:
        return None

    project_id, org_id = _vercel_config()
    resp = requests.get(
        f"https://api.vercel.com/v9/projects/{project_id}/env",
        headers=_vercel_headers(vercel_token),
        params=_vercel_params(org_id),
        timeout=30,
    )
    resp.raise_for_status()

    now = time.time()
    best: float | None = None
    for item in resp.json().get("env", []):
        if item.get("key") != key:
            continue
        targets = item.get("target") or []
        if "production" not in targets and targets:
            continue
        updated_ms = item.get("updatedAt") or item.get("createdAt")
        if updated_ms is None:
            continue
        age_h = (now - (float(updated_ms) / 1000.0)) / 3600.0
        if best is None or age_h < best:
            best = age_h
    return best


def upsert_vercel_env_cli(*, key: str, value: str, targets: tuple[str, ...] = DEFAULT_TARGETS) -> None:
    for target in targets:
        rm = subprocess.run(
            [*_vercel_cli_base(), "env", "rm", key, target, "--yes"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if rm.returncode not in (0, 1):
            rm.check_returncode()
        add = subprocess.run(
            [*_vercel_cli_base(), "env", "add", key, target],
            cwd=ROOT,
            input=value,
            capture_output=True,
            text=True,
        )
        add.check_returncode()
        print(f"Updated Vercel env {key} ({target}) via CLI")


def delete_vercel_env(*, key: str, targets: tuple[str, ...] = DEFAULT_TARGETS) -> None:
    """Remove an env var from Vercel for the given deployment targets."""
    vercel_token = _vercel_api_token()
    if not vercel_token:
        for target in targets:
            rm = subprocess.run(
                [*_vercel_cli_base(), "env", "rm", key, target, "--yes"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if rm.returncode not in (0, 1):
                rm.check_returncode()
            print(f"Removed Vercel env {key} ({target}) via CLI")
        return

    project_id, org_id = _vercel_config()
    params = _vercel_params(org_id)
    headers = _vercel_headers(vercel_token)
    resp = requests.get(
        f"https://api.vercel.com/v9/projects/{project_id}/env",
        headers=headers,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    target_set = set(targets)
    for item in resp.json().get("env", []):
        if item.get("key") != key:
            continue
        item_targets = set(item.get("target") or [])
        if item_targets and not (item_targets & target_set):
            continue
        env_id = item.get("id")
        if not env_id:
            continue
        delete = requests.delete(
            f"https://api.vercel.com/v9/projects/{project_id}/env/{env_id}",
            headers=headers,
            params=params,
            timeout=30,
        )
        delete.raise_for_status()
        print(f"Removed Vercel env {key} ({', '.join(sorted(item_targets or target_set))})")


def upsert_vercel_env(*, key: str, value: str, targets: tuple[str, ...] = DEFAULT_TARGETS) -> None:
    vercel_token = _vercel_api_token()
    if not vercel_token:
        upsert_vercel_env_cli(key=key, value=value, targets=targets)
        return

    project_id, org_id = _vercel_config()
    params = _vercel_params(org_id)
    headers = _vercel_headers(vercel_token)

    resp = requests.get(
        f"https://api.vercel.com/v9/projects/{project_id}/env",
        headers=headers,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()

    target_set = set(targets)
    for item in resp.json().get("env", []):
        if item.get("key") != key:
            continue
        item_targets = set(item.get("target") or [])
        if item_targets and not (item_targets & target_set):
            continue
        env_id = item.get("id")
        if not env_id:
            continue
        delete = requests.delete(
            f"https://api.vercel.com/v9/projects/{project_id}/env/{env_id}",
            headers=headers,
            params=params,
            timeout=30,
        )
        delete.raise_for_status()

    create = requests.post(
        f"https://api.vercel.com/v10/projects/{project_id}/env",
        headers=headers,
        params=params,
        json={"key": key, "value": value, "type": "encrypted", "target": list(targets)},
        timeout=30,
    )
    create.raise_for_status()
    print(f"Updated Vercel env {key} ({', '.join(targets)})")


def _refresh_lead_hours() -> float:
    try:
        return max(0.5, float(os.environ.get("VSS_VERCEL_REFRESH_LEAD_HOURS", "1") or "1"))
    except ValueError:
        return 1.0


def _token_ttl_hours() -> float:
    from vss_client import _token_ttl_hours

    return _token_ttl_hours()


def local_token_age_hours() -> float | None:
    from vss_client import _load_token_record

    rec = _load_token_record()
    if not rec or not rec.issued_at:
        return None
    issued = rec.issued_at
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - issued).total_seconds() / 3600.0


def neon_token_age_hours() -> float | None:
    """Age of the token production cron actually uses (Neon), not Vercel env."""
    try:
        from neon_token_store import configured, load_vss_token
    except Exception:
        return None
    if not configured():
        return None
    try:
        row = load_vss_token()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read Neon token age ({exc})")
        return None
    if not row:
        return None
    issued = row.get("issued_at")
    if not isinstance(issued, datetime):
        return None
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - issued).total_seconds() / 3600.0


def should_skip_refresh(*, force: bool) -> bool:
    if force:
        return False

    threshold = _token_ttl_hours() - _refresh_lead_hours()

    neon_age = neon_token_age_hours()
    if neon_age is not None:
        if neon_age < threshold:
            print(
                f"Skip: Neon VSS token was updated {neon_age:.1f}h ago "
                f"(refresh threshold {threshold:.1f}h)"
            )
            return True
        print(f"Neon VSS token is {neon_age:.1f}h old — refreshing")
        return False

    try:
        vercel_age = vercel_env_age_hours("VSS_TOKEN")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read Vercel env age ({exc}); continuing with refresh check")
        vercel_age = None

    if vercel_age is not None and vercel_age < threshold:
        print(
            f"Skip: VSS_TOKEN on Vercel was updated {vercel_age:.1f}h ago "
            f"(refresh threshold {threshold:.1f}h)"
        )
        return True

    local_age = local_token_age_hours()
    if local_age is not None and local_age < threshold:
        from vss_client import _load_token_record, _token_is_live

        rec = _load_token_record()
        if rec and rec.token and _token_is_live(rec.token):
            print(
                f"Skip: local token is {local_age:.1f}h old and still live "
                f"(refresh threshold {threshold:.1f}h)"
            )
            return True

    return False


def acquire_fresh_token(*, force: bool) -> tuple[str, str]:
    from neon_token_store import configured as neon_configured, load_vss_token
    from vss_client import (
        _save_token_to_file,
        _token_is_live,
        ensure_token,
        try_token_without_login,
        validate_or_renew_token,
    )

    neon_ok = neon_configured()

    if not force:
        cached = try_token_without_login()
        if cached:
            ok, msg = validate_or_renew_token(allow_reauth=True)
            if ok:
                token, pid = cached
                print(f"Reusing live token ({msg})")
                return token, pid or ""
            print(f"Stored token invalid ({msg}); logging in")

    token, pid = ensure_token(
        force=True,
        login_max_wait_seconds=120,
        allow_10082_retry=True,
    )
    if not _token_is_live(token):
        raise RuntimeError("VSS login succeeded but the new token failed validation")
    if neon_ok:
        _save_token_to_file(token, pid)
        row = load_vss_token()
        if not row or row.get("token") != token:
            raise RuntimeError("VSS token was not persisted to Neon after apiLogin")
        print(f"VSS token verified in Neon (pid={ (row.get('pid') or '')[:12] }…)")
        print("VSS apiLogin OK — token saved to Neon and ready for Vercel")
    else:
        print("NEON_DB_URL not set locally — pushing token to Vercel only (skipping Neon save)")
    return token, pid or ""


def push_token_pair(token: str, pid: str) -> None:
    upsert_vercel_env(key="VSS_TOKEN", value=token)
    upsert_vercel_env(key="VSS_PID", value=pid)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Always apiLogin and push, even if the current token looks fresh",
    )
    return parser.parse_args()


def main() -> int:
    _load_dotenv(ROOT / ".env")
    args = parse_args()

    if should_skip_refresh(force=args.force):
        return 0

    try:
        token, pid = acquire_fresh_token(force=args.force)
        push_token_pair(token, pid)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Done — VSS_TOKEN and VSS_PID pushed to Vercel production")
    return 0


if __name__ == "__main__":
    sys.exit(main())
