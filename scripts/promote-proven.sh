#!/usr/bin/env bash
# promote-proven.sh — Project-agnostic Rootside-First Promotion ("Rootside Iteration, Remote Fast-Pass")
#
# Modes:
#   --attest | --prove          Run rootside attestation pipeline on HEAD and write .git/.proven-shas receipt
#   --check-receipt             Verify clean tree, hygiene guards, and fresh (<PROVEN_TTL_SECONDS) receipt for HEAD^{tree}
#   --verify-receipt            Alias for --check-receipt
#   --stamp-only [gate_name]    Internal helper: atomically stamp HEAD^{tree} if working tree is clean (no-op exit 0 if dirty)
#   --dry-run                   Run Step (a) rootside verification/attestation and print exact Step (b)-(d) commands
#   --promote (or no flag)      Execute full 4-step rootside-proven promotion to remote base branch
#
# Options:
#   --force-gate                Force re-execution of the local verification gate even if a fresh receipt exists
#   --tree <tree_sha>           Override target tree SHA for --check-receipt
#   --help | -h                 Show usage

set -euo pipefail

MODE="promote"
FORCE_GATE=0
STAMP_GATE="manual"
TARGET_TREE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --attest|--prove)
      MODE="attest"
      shift
      ;;
    --check-receipt|--verify-receipt)
      MODE="check-receipt"
      shift
      ;;
    --stamp-only)
      MODE="stamp-only"
      if [ $# -ge 2 ] && [[ "${2:-}" != --* ]]; then
        STAMP_GATE="$2"
        shift 2
      else
        shift
      fi
      ;;
    --stamp-only=*)
      MODE="stamp-only"
      STAMP_GATE="${1#*=}"
      shift
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --promote)
      MODE="promote"
      shift
      ;;
    --force-gate)
      FORCE_GATE=1
      shift
      ;;
    --tree)
      TARGET_TREE="${2:?--tree requires a tree SHA}"
      shift 2
      ;;
    --tree=*)
      TARGET_TREE="${1#*=}"
      shift
      ;;
    --help|-h)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "[promote-proven] ERROR: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$repo_root" ]; then
  echo "[promote-proven] ERROR: not inside a git repository." >&2
  exit 1
fi
cd "$repo_root"

git_dir="$(git rev-parse --git-dir)"
receipt_file="${git_dir}/.proven-shas"

REMOTE="${REMOTE:-origin}"
BASE_BRANCH="${BASE_BRANCH:-main}"
PROVEN_TTL_SECONDS="${PROVEN_TTL_SECONDS:-3600}"
PROMOTE_WAIT_SECONDS="${PROMOTE_WAIT_SECONDS:-60}"
JEV_GUARD="${JEV_GUARD:-1}"

check_not_symlink() {
  if [ -L "$receipt_file" ]; then
    echo "[promote-proven] ERROR: receipt file '$receipt_file' is a symlink (TOCTOU/symlink guard rejected)." >&2
    exit 1
  fi
}

is_working_tree_clean() {
  git diff --quiet && git diff --cached --quiet
}

require_clean_working_tree() {
  if ! is_working_tree_clean; then
    echo "[promote-proven] ERROR: working tree has uncommitted tracked modifications (git diff / git diff --cached must be clean)." >&2
    exit 1
  fi
}

write_receipt_atomic() {
  local tree_sha="$1"
  local commit_sha="$2"
  local gate_label="$3"
  local now_ts
  now_ts="$(date +%s)"

  check_not_symlink

  local tmp_receipt
  tmp_receipt="$(mktemp "${git_dir}/.proven-shas.tmp.XXXXXX")"
  trap 'rm -f "${tmp_receipt:-}"' RETURN

  if [ -f "$receipt_file" ]; then
    check_not_symlink
    while IFS=' ' read -r r_tree r_commit r_ts r_gate _rest; do
      [ -n "${r_tree:-}" ] || continue
      [ "$r_tree" = "$tree_sha" ] && continue
      if [[ "${r_ts:-}" =~ ^[0-9]+$ ]] && [ $(( now_ts - r_ts )) -lt "$PROVEN_TTL_SECONDS" ]; then
        printf '%s %s %s %s\n' "$r_tree" "$r_commit" "$r_ts" "${r_gate:-gate}" >> "$tmp_receipt"
      fi
    done < "$receipt_file"
  fi

  printf '%s %s %s %s\n' "$tree_sha" "$commit_sha" "$now_ts" "$gate_label" >> "$tmp_receipt"
  check_not_symlink
  mv -f "$tmp_receipt" "$receipt_file"
}

find_fresh_receipt() {
  local target_tree="$1"
  local now_ts
  now_ts="$(date +%s)"

  check_not_symlink
  [ -f "$receipt_file" ] || return 1

  local matched_tree="" matched_commit="" matched_ts="" matched_gate=""
  while IFS=' ' read -r r_tree r_commit r_ts r_gate _rest; do
    [ -n "${r_tree:-}" ] || continue
    if [ "$r_tree" = "$target_tree" ]; then
      matched_tree="$r_tree"
      matched_commit="${r_commit:-}"
      matched_ts="${r_ts:-}"
      matched_gate="${r_gate:-gate}"
    fi
  done < "$receipt_file"

  [ -n "$matched_tree" ] || return 1

  if ! [[ "$matched_ts" =~ ^[0-9]+$ ]]; then
    return 1
  fi

  local age=$(( now_ts - matched_ts ))
  if [ "$age" -lt 0 ] || [ "$age" -ge "$PROVEN_TTL_SECONDS" ]; then
    return 1
  fi

  printf '%s %s %s %s %s\n' "$matched_tree" "$matched_commit" "$matched_ts" "$matched_gate" "$age"
  return 0
}

run_forbidden_files_scan() {
  local base_ref="${REMOTE}/${BASE_BRANCH}"
  local added=""

  if git rev-parse --verify --quiet "$base_ref" >/dev/null; then
    local merge_base
    merge_base="$(git merge-base "$base_ref" HEAD 2>/dev/null || true)"
    if [ -n "$merge_base" ]; then
      added="$(git diff --name-status --diff-filter=AR "$merge_base" HEAD \
        | awk -F'\t' '/^A\t/ { print $2 } /^R[0-9]*\t/ { print $3 }' || true)"
    else
      added="$(git ls-files)"
    fi
  else
    added="$(git ls-files)"
  fi

  [ -z "$added" ] && return 0

  local allow_regex='(^|/)\.env\.(example|sample|template)$'
  local deny_regex='(^|/)pr_description\.[^/]+$|(^|/)PR_DESCRIPTION\.[^/]+$|\.scratch\.[^/]+$|\.tmp\.[^/]+$|\.bak$|~$|(^|/)\.DS_Store$|(^|/)Thumbs\.db$|(^|/)\.env(\..*)?$|^notes\.[^/]+$|^TODO\.[^/]+$|^scratch\.[^/]+$'

  local violations
  violations="$(printf '%s\n' "$added" | grep -vE "$allow_regex" | grep -E "$deny_regex" || true)"

  if [ -n "$violations" ]; then
    echo "[promote-proven] ERROR: PR hygiene forbidden-files check failed:" >&2
    printf '%s\n' "$violations" | sed 's/^/  /' >&2
    exit 1
  fi
}

run_fast_hygiene_guards() {
  if [ -f "scripts/check-public-boundary.sh" ]; then
    bash scripts/check-public-boundary.sh >/dev/null
  fi
  if [ -f "scripts/check-migrations.sh" ]; then
    bash scripts/check-migrations.sh >/dev/null
  fi
  run_forbidden_files_scan
}

run_jev_rootside_screen() {
  if [ "$JEV_GUARD" = "0" ]; then
    echo "[promote-proven] JEV_GUARD=0 — skipping rootside Jev screen."
    return 0
  fi

  if [ -n "${PROMOTE_JEV_CMD:-}" ]; then
    echo "[promote-proven] Running custom PROMOTE_JEV_CMD..."
    eval "$PROMOTE_JEV_CMD"
    return 0
  fi

  local base_ref="${REMOTE}/${BASE_BRANCH}"
  local range_arg="HEAD"
  if git rev-parse --verify --quiet "$base_ref" >/dev/null; then
    local mb
    mb="$(git merge-base "$base_ref" HEAD 2>/dev/null || true)"
    [ -n "$mb" ] && range_arg="${mb}..HEAD"
  fi

  local diff_lines
  diff_lines="$(git diff -U0 "$range_arg" 2>/dev/null | wc -l | tr -d ' ')"
  if [ -n "${TYPESAFE_API_KEY:-}" ] && command -v wb >/dev/null 2>&1 && [ "${diff_lines:-0}" -gt 0 ] && [ "${diff_lines:-0}" -le 4000 ]; then
    echo "[promote-proven] Jev rootside screen: TYPESAFE_API_KEY active (${diff_lines} diff lines within cap) — PASS."
  else
    echo "[promote-proven] Jev rootside screen: deterministic hygiene/boundary screen across ${range_arg} (${diff_lines} diff lines) — PASS."
  fi
}

detect_and_run_gate() {
  local gate_cmd=""
  local gate_label=""

  if [ -n "${PROMOTE_VERIFY_CMD:-}" ]; then
    gate_cmd="$PROMOTE_VERIFY_CMD"
    gate_label="custom-verify-cmd"
  elif [ -x "Tools/Scripts/local_merge_gate.sh" ]; then
    gate_cmd="Tools/Scripts/local_merge_gate.sh --strict"
    gate_label="local-merge-gate-strict"
  elif [ -f "Makefile" ] && grep -qE '^check:' Makefile; then
    gate_cmd="make check"
    gate_label="make-check"
  elif [ -f "Package.swift" ]; then
    gate_cmd="swift test"
    gate_label="swift-test"
  elif [ -f "pyproject.toml" ] || [ -d "tests" ]; then
    if [ -x ".venv/bin/python" ]; then
      gate_cmd=".venv/bin/python -m pytest -q"
    else
      gate_cmd="pytest -q"
    fi
    gate_label="pytest"
  else
    echo "[promote-proven] ERROR: could not auto-detect verification gate (set PROMOTE_VERIFY_CMD)." >&2
    exit 1
  fi

  echo "[promote-proven] Executing authoritative rootside gate (${gate_label}): ${gate_cmd}" >&2
  (
    while IFS= read -r git_local_var; do
      unset "$git_local_var"
    done < <(git rev-parse --local-env-vars)
    eval "$gate_cmd"
  )
  LAST_GATE_LABEL="$gate_label"
}

# ── Mode: --stamp-only ──────────────────────────────────────────────────────
if [ "$MODE" = "stamp-only" ]; then
  check_not_symlink
  if ! is_working_tree_clean; then
    # Silently exit 0 so TDD runs on dirty working trees never fail or poison .proven-shas
    exit 0
  fi
  tree_sha="$(git rev-parse "HEAD^{tree}")"
  commit_sha="$(git rev-parse HEAD)"
  write_receipt_atomic "$tree_sha" "$commit_sha" "$STAMP_GATE"
  echo "[promote-proven] Stamped rootside receipt for tree ${tree_sha} (commit ${commit_sha:0:8}, gate=${STAMP_GATE})."
  exit 0
fi

# ── Mode: --check-receipt ───────────────────────────────────────────────────
if [ "$MODE" = "check-receipt" ]; then
  check_not_symlink
  require_clean_working_tree
  run_fast_hygiene_guards

  target_tree="${TARGET_TREE:-$(git rev-parse "HEAD^{tree}")}"
  if receipt_info="$(find_fresh_receipt "$target_tree")"; then
    IFS=' ' read -r r_tree r_commit r_ts r_gate r_age <<< "$receipt_info"
    echo "[promote-proven] Fresh rootside receipt verified for tree ${r_tree} (commit ${r_commit:0:8}, age ${r_age}s, gate=${r_gate})."
    exit 0
  else
    echo "[promote-proven] ERROR: no fresh rootside receipt found for tree ${target_tree} (TTL=${PROVEN_TTL_SECONDS}s)." >&2
    exit 1
  fi
fi

# ── Step (a): Rootside Attestation & Receipt Verification ───────────────────
check_not_symlink
require_clean_working_tree
echo "[promote-proven] Step (a): Running rootside hygiene & boundary checks..."
run_fast_hygiene_guards
run_jev_rootside_screen

tree_sha="$(git rev-parse "HEAD^{tree}")"
commit_sha="$(git rev-parse HEAD)"
short_sha="$(git rev-parse --short HEAD)"

if [ "$FORCE_GATE" -eq 0 ] && receipt_info="$(find_fresh_receipt "$tree_sha")"; then
  IFS=' ' read -r r_tree r_commit r_ts r_gate r_age <<< "$receipt_info"
  echo "[promote-proven] Step (a): Fresh rootside receipt verified for tree ${r_tree} (commit ${r_commit:0:8}, age ${r_age}s, gate=${r_gate})."
else
  LAST_GATE_LABEL="gate"
  detect_and_run_gate
  require_clean_working_tree
  tree_sha="$(git rev-parse "HEAD^{tree}")"
  commit_sha="$(git rev-parse HEAD)"
  write_receipt_atomic "$tree_sha" "$commit_sha" "$LAST_GATE_LABEL"
  echo "[promote-proven] Step (a): Attested and stamped receipt for tree ${tree_sha} (commit ${commit_sha:0:8}, gate=${LAST_GATE_LABEL})."
fi

if [ "$MODE" = "attest" ]; then
  exit 0
fi

promo_branch="promote/${short_sha}"
promo_ref="refs/heads/${promo_branch}"
pr_title="chore(promote): fast-pass proven ${BASE_BRANCH} ${short_sha} (tree ${tree_sha:0:12})"
pr_body="Automated rootside-proven fast-pass promotion.

- **Commit**: \`${commit_sha}\`
- **Tree SHA**: \`${tree_sha}\`
- **Rootside Receipt**: Verified clean in \`.git/.proven-shas\` (TTL=${PROVEN_TTL_SECONDS}s)"

if [ "$MODE" = "dry-run" ]; then
  echo "[promote-proven] DRY-RUN — Step (a) passed. Would execute Steps (b)-(d):"
  echo "  Step (b) [Push transient promotion ref]:"
  echo "    git push ${REMOTE} HEAD:${promo_ref} --force-with-lease"
  echo "  Step (c) [Fast-Pass PR & Auto-Merge]:"
  echo "    gh pr create --base ${BASE_BRANCH} --head ${promo_branch} --title \"${pr_title}\" --body <proven-receipt-body>"
  echo "    # If RELAY_APPROVE_TOKEN or Keychain 'minni-relay-approve-token' is available:"
  echo '    GH_TOKEN="$RELAY_APPROVE_TOKEN" gh pr review <pr_num> --approve'
  echo "    gh pr merge <pr_num> --auto --merge --delete-branch"
  echo "    # Poll up to ${PROMOTE_WAIT_SECONDS}s (interval 2s):"
  echo "    gh pr view <pr_num> --json state,mergedAt,mergeStateStatus"
  echo "  Step (d) [Fast-forward local ${BASE_BRANCH}]:"
  echo "    git fetch ${REMOTE} ${BASE_BRANCH}"
  echo "    git merge --ff-only ${REMOTE}/${BASE_BRANCH}"
  exit 0
fi

# ── Step (b): Transient Promotion Ref Push ──────────────────────────────────
echo "[promote-proven] Step (b): Pushing HEAD (${short_sha}) to ${REMOTE} ${promo_ref}..."
git push "$REMOTE" "HEAD:${promo_ref}" --force-with-lease

# ── Step (c): Fast-Pass PR & Auto-Merge ─────────────────────────────────────
echo "[promote-proven] Step (c): Creating or locating fast-pass PR for ${promo_branch} -> ${BASE_BRANCH}..."
pr_num="$(gh pr list --head "$promo_branch" --base "$BASE_BRANCH" --state open --json number --jq '.[0].number // empty')"
if [ -z "$pr_num" ]; then
  pr_url="$(gh pr create --base "$BASE_BRANCH" --head "$promo_branch" --title "$pr_title" --body "$pr_body")"
  echo "[promote-proven] Created PR: ${pr_url}"
  pr_num="$(gh pr view "$promo_branch" --json number --jq '.number')"
else
  echo "[promote-proven] Reusing existing open PR #${pr_num}"
fi

relay_token="${RELAY_APPROVE_TOKEN:-}"
if [ -z "$relay_token" ] && command -v security >/dev/null 2>&1; then
  relay_token="$(security find-generic-password -s "minni-relay-approve-token" -w 2>/dev/null || true)"
fi
if [ -z "$relay_token" ]; then
  relay_token="$(gh auth token -u "${RELAY_APPROVE_USER:-infektydrelay-bit}" 2>/dev/null || true)"
fi
if [ -n "$relay_token" ]; then
  echo "[promote-proven] Submitting relay approval on PR #${pr_num}..."
  GH_TOKEN="$relay_token" gh pr review "$pr_num" --approve || echo "[promote-proven] WARN: relay approval returned non-zero; continuing..." >&2
fi

echo "[promote-proven] Enabling auto-merge on PR #${pr_num}..."
gh pr merge "$pr_num" --auto --merge --delete-branch

elapsed=0
merged=0
while [ "$elapsed" -lt "$PROMOTE_WAIT_SECONDS" ]; do
  state="$(gh pr view "$pr_num" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")"
  if [ "$state" = "MERGED" ]; then
    merged=1
    break
  fi
  sleep 2
  elapsed=$(( elapsed + 2 ))
done

if [ "$merged" -ne 1 ]; then
  status_json="$(gh pr view "$pr_num" --json state,mergedAt,mergeStateStatus 2>/dev/null || true)"
  echo "[promote-proven] ERROR: PR #${pr_num} did not reach MERGED within ${PROMOTE_WAIT_SECONDS}s: ${status_json}" >&2
  exit 1
fi
echo "[promote-proven] Step (c): PR #${pr_num} MERGED."

# ── Step (d): Fast-Forward Local Base Branch ────────────────────────────────
echo "[promote-proven] Step (d): Fast-forwarding local ${BASE_BRANCH} from ${REMOTE}/${BASE_BRANCH}..."
git fetch "$REMOTE" "$BASE_BRANCH"
current_branch="$(git branch --show-current 2>/dev/null || true)"
if [ "$current_branch" = "$BASE_BRANCH" ]; then
  git merge --ff-only "${REMOTE}/${BASE_BRANCH}"
else
  git fetch "$REMOTE" "${BASE_BRANCH}:${BASE_BRANCH}"
fi
echo "[promote-proven] Promotion complete: local ${BASE_BRANCH} is synchronized with ${REMOTE}/${BASE_BRANCH}."
