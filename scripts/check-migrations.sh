#!/usr/bin/env bash
# Fail if migration filenames cannot be applied deterministically.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

duplicates="$(
  find engine/migrations -maxdepth 1 -type f -name '[0-9][0-9][0-9]_*.sql' -exec basename {} \; \
    | sed -E 's/^([0-9]+)_.*/\1/' \
    | sort \
    | uniq -d
)"

if [[ -n "$duplicates" ]]; then
  echo "Duplicate migration number(s) found:" >&2
  echo "$duplicates" >&2
  find engine/migrations -maxdepth 1 -type f -name '[0-9][0-9][0-9]_*.sql' -exec basename {} \; | sort >&2
  exit 1
fi

echo "Migration numbering check passed."
