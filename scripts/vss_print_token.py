"""Print a fresh VSS token via apiLogin (uses project .env). Run from repo root:

  .\\.venv\\Scripts\\python.exe scripts\\vss_print_token.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


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
            os.environ.setdefault(key, val)


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    _load_env_file(repo / ".env")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from vss_client import login_with_backoff

    token, pid = login_with_backoff(max_wait_seconds=180)
    print("token:", token)
    print("pid:", pid)


if __name__ == "__main__":
    main()
