"""Discover VSS user-list API paths from the web UI bundles."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
BASE = "http://40.76.130.233:9966"


def main() -> None:
    html = requests.get(f"{BASE}/", timeout=15).text
    scripts = re.findall(r'src=["\']([^"\']+)["\']', html)
    all_hits: set[str] = set()
    to_fetch = list(scripts)
    seen: set[str] = set()
    while to_fetch:
        src = to_fetch.pop()
        if src in seen or not src.endswith(".js"):
            continue
        seen.add(src)
        url = f"{BASE}{src}" if src.startswith("/") else src
        try:
            js = requests.get(url, timeout=30).text
        except requests.RequestException as exc:
            print("skip", src, exc, file=sys.stderr)
            continue
        all_hits.update(re.findall(r"/vss/[a-zA-Z0-9_/]+\.action", js))
        to_fetch.extend(re.findall(r'src=["\']([^"\']+\.js)["\']', js))
        to_fetch.extend(re.findall(r'["\'](/[^"\']+\.js)["\']', js))

    print(f"Total VSS .action endpoints in bundles: {len(all_hits)}")
    find_all = sorted(h for h in all_hits if "find" in h.lower() or "list" in h.lower() or "query" in h.lower())
    print("\nfind/list/query endpoints:")
    for hit in find_all:
        print(hit)

    user_hits = sorted(
        h for h in all_hits if any(k in h.lower() for k in ("user", "role", "account", "operator", "admin"))
    )
    print("\nUser-related VSS endpoints:")
    for hit in user_hits:
        print(hit)


if __name__ == "__main__":
    main()
