"""List all VSS users (requires a valid token in .vss_token.txt or VSS_TOKEN env).

Run from repo root:
  .\\.venv\\Scripts\\python.exe scripts\\vss_list_users.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as fh:
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


_USER_ENDPOINTS = [
    "/vss/user/findAll.action",
    "/vss/user/findByPage.action",
    "/vss/user/findPage.action",
    "/vss/user/list.action",
    "/vss/user/query.action",
    "/vss/user/getAllUser.action",
    "/vss/user/getUserList.action",
    "/vss/user/findUserList.action",
    "/vss/user/findAllUser.action",
    "/vss/sysuser/findAll.action",
    "/vss/roleuser/findAll.action",
    "/vss/roleuser/findByPage.action",
]


def _unwrap_rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "rows", "data", "users", "userList", "records"):
        val = data.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    return []


def _user_label(row: dict) -> str:
    for key in (
        "username",
        "userName",
        "loginName",
        "account",
        "name",
        "realName",
        "nickName",
        "email",
        "phone",
        "mobile",
        "userId",
        "id",
    ):
        val = row.get(key)
        if val not in (None, ""):
            return f"{key}={val}"
    return json.dumps(row, ensure_ascii=False)[:120]


def main() -> None:
    _load_env_file(REPO / ".env")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    import vss_client as v

    token_pair = v.try_token_without_login()
    if not token_pair:
        print("No token in .vss_token.txt or VSS_TOKEN — logging in (may hit 10082 if rate-limited)...")
        token, pid = v.ensure_token(login_max_wait_seconds=180)
    else:
        token, pid = token_pair

    last_err = ""
    for path in _USER_ENDPOINTS:
        for payload in (
            {"token": token, "pid": pid, "pageNum": -1, "pageCount": -1},
            {"token": token, "pid": pid, "pageNum": 1, "pageCount": 5000},
            {"token": token, "pid": pid},
        ):
            try:
                j = v.vss_post_raw(path, payload, timeout=45, max_attempts=2)
            except Exception as exc:
                last_err = f"{path}: {exc}"
                break
            status = j.get("status")
            if status == 10023:
                print("Session expired (10023). Paste a fresh token into .vss_token.txt and retry.")
                raise SystemExit(1)
            if status != 10000:
                last_err = f"{path}: status={status} msg={j.get('msg')}"
                break
            rows = _unwrap_rows(j.get("data"))
            if not rows and isinstance(j.get("data"), dict):
                rows = _unwrap_rows(j)
            if not rows:
                last_err = f"{path}: empty data"
                continue
            print(f"Endpoint: {path}")
            print(f"Users: {len(rows)}")
            print("-" * 60)
            for i, row in enumerate(rows, 1):
                print(f"{i:4}. {_user_label(row)}")
            out = REPO / "vss_users.json"
            out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print("-" * 60)
            print(f"Saved full records to {out}")
            return

    print("Could not list users from known endpoints.")
    if last_err:
        print("Last error:", last_err)
    print("Run scripts/vss_discover_user_api.py to scan the VSS UI for more endpoints.")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
