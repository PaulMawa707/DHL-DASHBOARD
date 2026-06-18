"""Extract likely VSS token/pid from browser storage WITHOUT DevTools.

This scans Chrome/Edge profile storage files for JSON-ish fragments containing
`"token":"<hex>"` and optional `"pid":"..."`.

Usage (PowerShell):
  Set-Location "C:\\Users\\Paul\\Downloads\\dhl_dashboard"
  .\\.venv\\Scripts\\python.exe scripts\\vss_scan_storage_token.py

If it finds a token, it prints candidates and (optionally) writes the first one
to `.vss_token.txt` in the repo root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


VSS_HOST = os.environ.get("VSS_EXTRACT_HOST", "40.76.130.233").strip()
VSS_PORT = os.environ.get("VSS_EXTRACT_PORT", "9966").strip() or "9966"
WRITE_FILE = os.environ.get("VSS_EXTRACT_WRITE_FILE", "1").strip().lower() in ("1", "true", "yes", "on")

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = REPO_ROOT / ".vss_token.txt"


TOKEN_RE = re.compile(rb'"token"\s*:\s*"([0-9a-f]{16,128})"', re.I)
PID_RE = re.compile(rb'"pid"\s*:\s*"([^"]{8,512})"', re.I)

# Some VSS builds use different keys.
ALT_TOKEN_RE = re.compile(rb'(?:vssToken|accessToken|token)\s*"?\s*[:=]\s*"([0-9a-f]{16,128})"', re.I)
ALT_PID_RE = re.compile(rb'(?:pid)\s*"?\s*[:=]\s*"([^"]{8,512})"', re.I)


@dataclass(frozen=True)
class Hit:
    token: str
    pid: str
    source: str


def _localappdata() -> Path | None:
    raw = os.environ.get("LOCALAPPDATA", "")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _profile_leveldb_dirs() -> list[Path]:
    lad = _localappdata()
    if lad is None:
        return []

    roots = [
        lad / "Google" / "Chrome" / "User Data",
        lad / "Microsoft" / "Edge" / "User Data",
    ]

    dirs: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for prof in root.glob("*"):
            if not prof.is_dir():
                continue
            # Local Storage LevelDB
            d = prof / "Local Storage" / "leveldb"
            if d.is_dir():
                dirs.append(d)
    return dirs


def _profile_cookie_dbs() -> list[Path]:
    lad = _localappdata()
    if lad is None:
        return []

    roots = [
        lad / "Google" / "Chrome" / "User Data",
        lad / "Microsoft" / "Edge" / "User Data",
    ]

    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for prof in root.glob("*"):
            if not prof.is_dir():
                continue
            for cand in (prof / "Network" / "Cookies", prof / "Cookies"):
                if cand.is_file():
                    out.append(cand)
    return out


def _scan_bytes(blob: bytes, *, source: str) -> list[Hit]:
    if not blob:
        return []

    # Prefer strict JSON-ish hits first
    hits: list[Hit] = []
    for m in TOKEN_RE.finditer(blob):
        token = m.group(1).decode("ascii", "ignore")
        # Search a small window after the token for pid
        start = max(0, m.start() - 4096)
        end = min(len(blob), m.end() + 4096)
        pid_m = PID_RE.search(blob[start:end]) or ALT_PID_RE.search(blob[start:end])
        pid = pid_m.group(1).decode("utf-8", "ignore") if pid_m else ""
        hits.append(Hit(token=token, pid=pid, source=source))

    if hits:
        return hits

    # Fallback: looser patterns
    m2 = ALT_TOKEN_RE.search(blob)
    if m2:
        token = m2.group(1).decode("ascii", "ignore")
        pid_m = ALT_PID_RE.search(blob)
        pid = pid_m.group(1).decode("utf-8", "ignore") if pid_m else ""
        return [Hit(token=token, pid=pid, source=source)]

    return []


def _relevant(blob: bytes) -> bool:
    # Quick prefilter: mention VSS host or port or typical key strings.
    bhost = VSS_HOST.encode("utf-8", "ignore")
    return (
        bhost in blob
        or VSS_PORT.encode("utf-8", "ignore") in blob
        or b"token" in blob.lower()
        or b"vss" in blob.lower()
    )


def scan() -> list[Hit]:
    out: dict[tuple[str, str], Hit] = {}
    leveldb_dirs = _profile_leveldb_dirs()
    if not leveldb_dirs:
        return []

    # Scan only log/ldb files (ignore LOCK/CURRENT/MANIFEST*).
    for d in leveldb_dirs:
        for fp in list(d.glob("*.log")) + list(d.glob("*.ldb")):
            try:
                blob = fp.read_bytes()
            except OSError:
                continue
            if not _relevant(blob):
                continue
            for hit in _scan_bytes(blob, source=str(fp)):
                if len(hit.token) < 16:
                    continue
                key = (hit.token, hit.pid)
                out.setdefault(key, hit)

    return list(out.values())


def scan_cookie_dbs() -> list[Hit]:
    """Scan Chrome/Edge cookie sqlite DB files for token-like values.

    Note: cookie values are often encrypted on Windows. We still scan raw bytes
    for embedded JSON fragments and host markers — sometimes VSS stores a plain
    token in a cookie value.
    """
    out: dict[tuple[str, str], Hit] = {}
    for fp in _profile_cookie_dbs():
        try:
            blob = fp.read_bytes()
        except OSError:
            continue
        if VSS_HOST.encode("utf-8", "ignore") not in blob and b"9966" not in blob:
            continue
        for hit in _scan_bytes(blob, source=str(fp)):
            out.setdefault((hit.token, hit.pid), hit)
    return list(out.values())


def _looks_like_hex(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{16,128}", s or ""))


def _score(hit: Hit) -> int:
    # Prefer 32-hex tokens (common) + non-empty pid.
    score = 0
    if _looks_like_hex(hit.token):
        score += 5
        if len(hit.token) == 32:
            score += 5
    if hit.pid:
        score += 3
    # Prefer shorter pid-ish values (but allow long).
    if hit.pid and len(hit.pid) < 200:
        score += 1
    # Prefer sources mentioning VSS host
    if VSS_HOST in hit.source:
        score += 2
    return score


def main() -> int:
    print(f"Scanning browser storage for VSS token (host={VSS_HOST}, port={VSS_PORT})")
    hits = scan()
    if not hits:
        hits = scan_cookie_dbs()
    if not hits:
        print("No token-like strings found in Chrome/Edge Local Storage LevelDB.")
        print("Also scanned cookie DBs; still nothing found.")
        print("If you're logged into VSS, try: open VSS, wait 30s, then re-run this script.")
        return 1

    hits = sorted(hits, key=_score, reverse=True)
    print(f"Found {len(hits)} candidate(s). Showing top 10:\n")
    for i, h in enumerate(hits[:10], start=1):
        tok_preview = (h.token[:12] + "..." + h.token[-6:]) if len(h.token) > 22 else h.token
        pid_preview = (h.pid[:12] + "..." + h.pid[-6:]) if h.pid and len(h.pid) > 22 else (h.pid or "(none)")
        print(f"{i:>2}. token={tok_preview}  pid={pid_preview}")
        print(f"    source={h.source}")

    best = hits[0]
    if WRITE_FILE and best.token:
        line = f"{best.token} {best.pid}".strip()
        TOKEN_FILE.write_text(line + "\n", encoding="utf-8")
        print(f"\nWrote best candidate to {TOKEN_FILE}")
        print("Restart the dashboard and click Refresh data.")
    else:
        print("\nSet env VSS_EXTRACT_WRITE_FILE=1 to auto-write .vss_token.txt")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

