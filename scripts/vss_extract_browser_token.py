"""Try to read a VSS token from an already-logged-in browser session (no DevTools).

Works when Chrome/Edge has an active VSS login and stores cookies for the host.
Run from repo root:

  .\\.venv\\Scripts\\python.exe scripts\\vss_extract_browser_token.py

If this finds nothing, log into VSS in Edge (not Chrome), wait 2 minutes, run again.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

VSS_HOST = os.environ.get("VSS_EXTRACT_HOST", "40.76.130.233")
VSS_PORT = os.environ.get("VSS_EXTRACT_PORT", "9966")
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".vss_token.txt"


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _cookie_matches(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return VSS_HOST.lower() in host or host.endswith(f":{VSS_PORT}")


def _try_browser_cookie3() -> list[dict]:
    try:
        import browser_cookie3
    except ImportError:
        return []

    out: list[dict] = []
    loaders = [
        ("chrome", browser_cookie3.chrome),
        ("edge", browser_cookie3.edge),
        ("firefox", browser_cookie3.firefox),
    ]
    for name, fn in loaders:
        try:
            jar = fn(domain_name=VSS_HOST)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] unavailable: {e}")
            continue
        for c in jar:
            if _cookie_matches(f"http://{c.domain}"):
                out.append({"browser": name, "name": c.name, "value": c.value, "domain": c.domain})
    return out


def _try_chrome_local_storage() -> list[dict]:
    """Best-effort scan of Chrome/Edge Local Storage LevelDB files for token-like strings."""
    try:
        import plyvel
    except ImportError:
        return []

    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []

    roots = [
        Path(local) / "Google/Chrome/User Data",
        Path(local) / "Microsoft/Edge/User Data",
    ]
    hits: list[dict] = []
    token_re = re.compile(rb'"token"\s*:\s*"([0-9a-f]{16,64})"', re.I)
    pid_re = re.compile(rb'"pid"\s*:\s*"([^"]{8,})"', re.I)

    for root in roots:
        if not root.is_dir():
            continue
        for ls_dir in root.glob("*/Local Storage/leveldb"):
            for ldb in ls_dir.glob("*.ldb"):
                try:
                    raw = ldb.read_bytes()
                except OSError:
                    continue
                if VSS_HOST.encode() not in raw and b"9966" not in raw:
                    continue
                tok_m = token_re.search(raw)
                pid_m = pid_re.search(raw)
                if tok_m:
                    hits.append(
                        {
                            "source": str(ldb),
                            "token": tok_m.group(1).decode("ascii", "ignore"),
                            "pid": pid_m.group(1).decode("ascii", "ignore") if pid_m else "",
                        }
                    )
    return hits


def main() -> int:
    _load_env()
    print(f"Looking for VSS session on {VSS_HOST}:{VSS_PORT} ...")
    print("Tip: log into VSS in the browser first, then run this script.\n")

    cookies = _try_browser_cookie3()
    if cookies:
        print("Cookies found:")
        for c in cookies:
            val = c["value"]
            preview = val[:20] + "..." if len(val) > 20 else val
            print(f"  [{c['browser']}] {c['domain']} / {c['name']} = {preview}")
        for c in cookies:
            name = c["name"].lower()
            if name in ("token", "vss_token", "access_token") or (len(c["value"]) == 32 and re.fullmatch(r"[0-9a-f]+", c["value"], re.I)):
                token = c["value"]
                pid = next((x["value"] for x in cookies if x["name"].lower() == "pid"), "")
                print("\nLikely token cookie:")
                print("token:", token)
                if pid:
                    print("pid:", pid)
                TOKEN_FILE.write_text(f"{token} {pid}\n".strip() + "\n", encoding="utf-8")
                print(f"\nSaved to {TOKEN_FILE}")
                return 0
    else:
        print("No cookies via browser_cookie3 (install with: pip install browser-cookie3)")

    storage_hits = _try_chrome_local_storage()
    if storage_hits:
        best = storage_hits[0]
        print("\nLocal storage hit:")
        print("token:", best["token"])
        if best.get("pid"):
            print("pid:", best["pid"])
        TOKEN_FILE.write_text(f"{best['token']} {best.get('pid', '')}\n".strip() + "\n", encoding="utf-8")
        print(f"\nSaved to {TOKEN_FILE}")
        return 0

    print(
        "\nNothing found yet. Options:\n"
        "  1) Log into VSS in Microsoft Edge (often allows F12 where Chrome blocks it)\n"
        "  2) pip install browser-cookie3 && run this script again\n"
        "  3) Stop app.py/notebooks for 60 min, then run scripts/vss_print_token.py ONCE\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
