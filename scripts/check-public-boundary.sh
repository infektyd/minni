#!/usr/bin/env bash
# Fail when public git contains private runtime state or generated local output.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# X11 (§security): write scratch output to a mktemp-created file, never a
# hardcoded /tmp path — a hardcoded path is a symlink-plant/truncation target
# (another local user or process can pre-create /tmp/<name> as a symlink to an
# arbitrary file, and this script's `>` redirect would then truncate/overwrite
# whatever it points at). mktemp creates the file atomically and exclusively
# under $TMPDIR (or /tmp), so no attacker-controlled path can be substituted.
# The trap ensures both scratch files are removed on any exit path.
boundary_out="$(mktemp)"
sensitive_out="$(mktemp)"
trap 'rm -f "$boundary_out" "$sensitive_out"' EXIT

if git ls-files | rg '(^|/)node_modules/|\.log$|\.db$|\.sqlite$|\.sqlite3$|\.faiss$|\.npz$|\.fmadapter$|(^|/)\.DS_Store$|^\.claude/|^codex-vault/|^claudecode-vault/|^logs/|^inbox/|^outbox/|^raw/|^wiki/|^session-extracts/' >"$boundary_out"; then
  echo "Public-boundary check failed. These tracked files look private, generated, or runtime-only:" >&2
  cat "$boundary_out" >&2
  exit 1
fi

if git ls-files | rg '(^|/)session-export-[0-9]+\.zip$|\.fmadapter$|adapter.*\.json$|hook.*dump|sovereign_memory\.db' >"$sensitive_out"; then
  echo "Sensitive-artifact check failed. These tracked files should not ship in the public repo:" >&2
  cat "$sensitive_out" >&2
  exit 1
fi

echo "Public-boundary check passed."
