#!/usr/bin/env python3
"""
import-wiki.py — Idempotent one-shot import of ~/wiki/**/*.md into Sovereign Memory.

Uses a SHA-256 file manifest for true content-hash deduplication.
Manifest lives at .import-manifest.json in the plugin directory.

Env overrides:
  WIKI_DIR         default ~/wiki
  SOCKET_PATH      default /tmp/sovereign.sock
  DRY_RUN          if set, skip writes
  FORCE_REIMPORT   if set, ignore manifest and re-import everything
  MANIFEST_PATH    override manifest location
  DEDUP_THRESHOLD  default 0.9 (used for safety-net check, not primary dedup)

Exit codes: 0 success, 1 errors, 2 bad wiki dir, 3 daemon unreachable
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
WIKI_DIR = Path(os.environ.get("WIKI_DIR", os.path.expanduser("~/wiki")))
SOCKET_PATH = os.environ.get("SOCKET_PATH", "/tmp/sovereign.sock")
MANIFEST_PATH = Path(
    os.environ.get("MANIFEST_PATH", str(PLUGIN_DIR / ".import-manifest.json"))
)
DRY_RUN = bool(os.environ.get("DRY_RUN"))
FORCE_REIMPORT = bool(os.environ.get("FORCE_REIMPORT"))
CONTENT_MAX = 4000


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)


def _call(method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> dict:
    conn = UnixSocketHTTPConnection(SOCKET_PATH)
    conn.timeout = timeout
    try:
        headers = {"Content-Type": "application/json"}
        payload = json.dumps(body).encode("utf-8") if body else b""
        if body:
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload if body else None, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", errors="replace")}
    finally:
        conn.close()


def already_indexed(excerpt: str, threshold: float = 0.95) -> bool:
    """Semantic recall dedup. Returns True if any result exceeds threshold."""
    try:
        resp = _call("GET", f"/recall?q={urllib.parse.quote(excerpt)}&limit=3")
    except Exception:
        return False

    results = resp.get("results") if isinstance(resp, dict) else None
    if not results:
        return False

    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            score = item.get("score", 0)
            if isinstance(score, (int, float)):
                if score > threshold or (
                    score < 0 and max(0, min(1, (score + 20) / 20)) > threshold
                ):
                    return True
        return False

    if not isinstance(results, str) or not results:
        return False

    import re
    for m in re.finditer(r"\(score=(-?\d+(?:\.\d+)?)\)", results):
        try:
            raw_score = float(m.group(1))
            if 0 <= raw_score <= 1 and raw_score > threshold:
                return True
            if raw_score < 0 and max(0, min(1, (raw_score + 20) / 20)) > threshold:
                return True
        except ValueError:
            continue
    return False


def load_manifest(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"version": 1, "entries": {}}
    return {"version": 1, "entries": {}}


def save_manifest(path: Path, data: dict) -> None:
    """Atomic write: dump to tmp file, then os.replace."""
    data["version"] = 1
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dir_), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(path))
    except:
        os.unlink(tmp)
        raise


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    if not WIKI_DIR.is_dir():
        print(f"ERROR: wiki dir not found: {WIKI_DIR}", file=sys.stderr)
        return 2

    # Smoke-test daemon
    try:
        health = _call("GET", "/health")
        if health.get("status") != "ok":
            print(f"ERROR: sovrd unhealthy: {health}", file=sys.stderr)
            return 3
    except Exception as e:
        print(f"ERROR: cannot reach sovrd at {SOCKET_PATH}: {e}", file=sys.stderr)
        return 3

    # Load manifest
    manifest = load_manifest(MANIFEST_PATH)
    entries: dict = manifest.get("entries", {})

    if FORCE_REIMPORT:
        print("[force-reimport] ignoring manifest entirely")
        entries = {}
    elif DRY_RUN:
        print("[dry-run] will not modify manifest or POST")

    files = sorted(WIKI_DIR.rglob("*.md"))
    total = len(files)
    if total == 0:
        print("No .md files found under", WIKI_DIR)
        return 0

    imported_new = 0
    imported_updated = 0
    skipped_unchanged = 0
    skipped_semantic = 0
    errors = 0

    for i, fp in enumerate(files, 1):
        rel = str(fp.relative_to(WIKI_DIR))
        try:
            content = fp.read_text(encoding="utf-8").strip()
        except Exception as e:
            errors += 1
            print(f"[{i}/{total}] error reading: {rel}  ({e})")
            continue

        if not content:
            skipped_unchanged += 1
            print(f"[{i}/{total}] skipped (empty): {rel}")
            continue

        digest = sha256_of(content)
        entry = entries.get(rel)

        # Manifest-based decision
        if entry and not FORCE_REIMPORT:
            if entry.get("sha256") == digest:
                skipped_unchanged += 1
                print(f"[{i}/{total}] skipped-unchanged: {rel}")
                continue
            else:
                # Hash differs → update
                new_count = entry.get("import_count", 0) + 1
                import_tag = f"[wiki-import:{rel} v={new_count}]"
        else:
            # Not in manifest → import fresh, but run safety-net semantic check
            import_tag = f"[wiki-import:{rel} v=1]"
            if not FORCE_REIMPORT:
                excerpt = content[:80].replace("\n", " ").strip()
                if already_indexed(excerpt, threshold=0.95):
                    print(f"[{i}/{total}] ⚠ pre-manifest dupe suspected (semantic): {rel}")
                    skipped_semantic += 1
                    # Still import — flag for human review

        if DRY_RUN:
            status = "would-update" if (entry and entry.get("sha256") != digest) else "would-import"
            print(f"[{i}/{total}] {status} (dry-run): {rel}")
            if entry and entry.get("sha256") != digest:
                imported_updated += 1
            else:
                imported_new += 1
            continue

        # POST /learn
        tagged = f"{import_tag}\n{content[:CONTENT_MAX]}"
        try:
            resp = _call("POST", "/learn", body={"content": tagged, "category": "wiki-import"})
            if isinstance(resp, dict) and resp.get("status") == "learned":
                # Update manifest after successful learn
                if rel not in entries:
                    imported_new += 1
                    print(f"[{i}/{total}] imported-new: {rel}")
                else:
                    imported_updated += 1
                    print(f"[{i}/{total}] imported-updated: {rel} v={new_count}")

                entries[rel] = {
                    "sha256": digest,
                    "size": len(content.encode("utf-8")),
                    "mtime": os.path.getmtime(str(fp)),
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                    "import_count": new_count if (entry and entry.get("sha256") != digest) else 1,
                }
                save_manifest(MANIFEST_PATH, {"version": 1, "entries": dict(sorted(entries.items()))})
            else:
                errors += 1
                print(f"[{i}/{total}] error on /learn: {rel}  ({resp})")
        except Exception as e:
            errors += 1
            print(f"[{i}/{total}] exception on /learn: {rel}  ({e})")

    print()
    print("=" * 60)
    print(
        f"Summary: scanned={total} "
        f"imported-new={imported_new} "
        f"imported-updated={imported_updated} "
        f"skipped-unchanged={skipped_unchanged} "
        f"skipped-semantic={skipped_semantic} "
        f"errors={errors}"
    )
    print("=" * 60)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
