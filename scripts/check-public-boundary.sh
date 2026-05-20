#!/usr/bin/env bash
# Fail when public git contains private runtime state or generated local output.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if git ls-files | rg '(^|/)node_modules/|\.log$|\.db$|\.sqlite$|\.sqlite3$|\.faiss$|\.npz$|\.fmadapter$|(^|/)\.DS_Store$|^\.claude/|^codex-vault/|^claudecode-vault/|^logs/|^inbox/|^outbox/|^raw/|^wiki/|^session-extracts/' >/tmp/sovereign-public-boundary.txt; then
  echo "Public-boundary check failed. These tracked files look private, generated, or runtime-only:" >&2
  cat /tmp/sovereign-public-boundary.txt >&2
  exit 1
fi

if git ls-files | rg '(^|/)session-export-[0-9]+\.zip$|\.fmadapter$|adapter.*\.json$|hook.*dump|sovereign_memory\.db' >/tmp/sovereign-public-sensitive.txt; then
  echo "Sensitive-artifact check failed. These tracked files should not ship in the public repo:" >&2
  cat /tmp/sovereign-public-sensitive.txt >&2
  exit 1
fi

echo "Public-boundary check passed."
