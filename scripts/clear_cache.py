"""Clear dashboard caches before a restart (run from project root).

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\clear_cache.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_env(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ[key.strip()] = val.strip()


def main() -> None:
    _load_env(os.path.join(ROOT, ".env"))
    from data import clear_all_dashboard_caches

    clear_all_dashboard_caches(include_disk_catalog=True)
    print("Cleared in-memory caches, MiX tacho cache, and .mix_asset_catalog.csv")
    print("Restart the dashboard: .\\.venv\\Scripts\\python.exe app.py")


if __name__ == "__main__":
    main()
