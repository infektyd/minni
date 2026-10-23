#!/usr/bin/env bash
# Fail when tracked file CONTENT carries private runtime detail or credential
# shapes. The path-only companion is scripts/check-public-boundary.sh.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Modes:
#   (no argument)           full mode: scan every tracked file (CI, make check).
#   --pre-push [<remote>]   range mode: read git's pre-push lines on stdin and
#                           scan only the lines that the pushed ranges ADD.
mode=full
push_remote=origin
case "${1:-}" in
  '') ;;
  --pre-push)
    mode=range
    push_remote="${2:-origin}"
    ;;
  *)
    echo "usage: check-public-content.sh [--pre-push [<remote>]]" >&2
    exit 2
    ;;
esac

# Scratch output goes to mktemp-created paths, never a hardcoded /tmp name (a
# symlink-plant/truncation target); the trap removes them on any exit path.
content_out="$(mktemp)"
grep_out="$(mktemp)"
grep_nul="$(mktemp)"
work_dir="$(mktemp -d)"
trap 'rm -f "$content_out" "$grep_out" "$grep_nul"; rm -rf "$work_dir"' EXIT

# ---------------------------------------------------------------------------
# Content scan. scripts/check-public-boundary.sh only checks tracked PATHS; this
# script reads the CONTENT of every tracked file and rejects absolute home paths,
# paths deeper than a vault root, plan ids, and credential shapes.
#
# Where it runs: full mode in both CI workflows and `make check`. The receipt
# fast path (promote-proven.sh --check-receipt) keeps only the path check.
# .githooks/pre-push runs the path check plus this script's range mode, which
# scans only the lines a push adds, because on a public repo CI runs after the
# push. It is defence in depth on top of the local commit hook, not the only
# control.
#
# Range mode (--pre-push): for each pushed ref, the range is <remote sha>..<local
# sha>; for a new branch it is <merge-base with the remote's default branch>..
# <local sha>; a deletion scans nothing. A range that cannot be computed (unknown
# sha, no merge-base, unresolvable default branch) fails closed. Every commit in
# the range is scanned on its own: the lines it adds against its FIRST parent
# (merges are diffed against the first parent only), so a line added and removed
# again inside the range is still caught, since the remote receives that
# intermediate commit. Symlink targets are included. Same patterns, names,
# binary rule and output rules; the allowlist is read from the pushed tip (a
# working-tree-only entry does not count), each ERE validated with the full-mode
# rules the first time it is consulted, and the allowlisted line is read from
# the commit that added it. Unused allowlist entries are only reported in full
# mode.
#
# Engine: `git grep --cached -E` (POSIX ERE) over the index, i.e. exactly the
# tracked tree as it will be committed, with binary files skipped (-I). rg is
# deliberately not used here: its Rust regex dialect rejects the
# token-assignment ERE below ("unclosed character class" at `{[]`), and for
# explicitly named files it prints "binary file matches" lines instead of
# skipping them. One engine keeps one regex dialect and one binary rule, and
# the scan needs nothing beyond git (so it runs the same with or without rg).
#
# `--attr-source=<empty tree>` makes git ignore .gitattributes while deciding
# what is binary: otherwise a `-diff` or `binary` attribute (this repo marks
# *.csv binary) hides a text file from -I. Binary then means "has a NUL byte
# near the start", nothing else. That option needs git >= 2.42; older git fails
# the scan closed rather than scanning with the attributes in force.
#
# Each table row is <class><TAB><ERE>. Literal prefixes are bracketed
# ([s]k-ant- matches exactly what the unbracketed text matches) so no row
# matches its own source line and this script needs no self-exemption. Keep
# that property when adding rows; the fixture test runs the scan on itself.
#
# token-assignment has an unquoted form for all five suffixes and a quoted form
# (PASSWORD="...", same 8+ char value) only for _API_KEY, _SECRET and PASSWORD.
# A quoted _KEY or _TOKEN value is overwhelmingly a storage key, cache key or
# template placeholder (19 such benign hits on the tree when this was written,
# 0 for the three suffixes kept), so quoting those two would mostly add noise.
#
# Failure output names file:line, the class and the match length. The layout
# classes (home-path, vault-deep-path, plan-id) add the first 4 characters of
# the match. The secret classes (token-*, private-key) print no excerpt at all:
# CI logs on a public repo are public, and any characters of a real secret are
# too many. The matched text is never printed in full.
#
# Deliberate exceptions live in scripts/public-boundary-allow.txt (format at the
# top of that file). An entry exempts a hit only when its line ERE matches the
# flagged line at a byte span that OVERLAPS the flagged match, so an entry for
# one path on a line never covers a different hit on the same line. Every entry
# must still match something, or the scan fails.
# ---------------------------------------------------------------------------
export LC_ALL=C
tab="$(printf '\t')"
allow_file="scripts/public-boundary-allow.txt"

# A home path is /Users/<name> or /home/<name> followed by "/", whitespace, a
# quote, a backtick, ":", ")" or the end of the line.
# The Users/home segment itself is matched case-insensitively (macOS paths
# are case-insensitive, so a lowercased copy of a path is the same path).
# Home-path names that are neutral fixtures or placeholders, never a real
# account. Exact, case-sensitive matches against the name segment that follows
# Users or home. A short explicit set, never a wildcard; anything else is
# allowlisted.
neutral_names=',someuser,user,username,me,you,example,test,alice,bob,x,<user>,$USER,<operator>,<name>,REPLACE_WITH_YOUR_USERNAME,'

# Names that must never appear as a home-path segment, whatever the neutral set
# or the allowlist says: the current OS user (`id -un`) plus the comma-separated
# MINNI_BOUNDARY_FORBIDDEN_NAMES. Compared case-insensitively. This keeps the
# operator's real username out of the repo without writing it into the repo.
# The OS user is chosen over parsing a user-level git hook config because it is
# always present and has no file format to drift. Under GitHub Actions
# (GITHUB_ACTIONS=true) the OS user is the runner's service account, not a
# secret, so that source is skipped there; MINNI_BOUNDARY_FORBIDDEN_NAMES still
# applies. With neither source present, as in CI, the allowlist rules apply as
# written.
os_user=''
if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
  os_user="$(id -un 2>/dev/null || true)"
fi
forbidden_raw="${os_user},${MINNI_BOUNDARY_FORBIDDEN_NAMES:-}"
forbidden_names=",$(printf '%s' "$forbidden_raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'),"

content_classes=' home-path vault-deep-path plan-id token-assignment token-anthropic token-sk token-github token-slack token-google token-aws token-gitlab token-digitalocean token-huggingface token-replicate token-stripe private-key '

# validate_ere <ere> <where>: with the scan's own engine. An ERE that matches an
# empty line could match an empty span anywhere, so it is rejected too.
validate_ere() {
  local st=0
  printf '\n' >"$work_dir/probe"
  git -C "$work_dir" grep --no-index -q -E -e "$1" -- probe >/dev/null 2>&1 || st=$?
  if [ "$st" -gt 1 ]; then
    echo "Content check error: $2 has an invalid ERE." >&2
    exit 2
  fi
  if [ "$st" -eq 0 ]; then
    echo "Content check error: $2 has a line ERE that matches an empty line." >&2
    exit 2
  fi
}

# sha256 of stdin as 64 hex chars, with whichever tool this host has. Fails the
# scan closed when an exact-line entry exists and no tool is available.
sha_tool=''
ensure_sha_tool() {
  [ -z "$sha_tool" ] || return 0
  if command -v sha256sum >/dev/null 2>&1; then
    sha_tool=sha256sum
  elif command -v shasum >/dev/null 2>&1; then
    sha_tool=shasum
  elif command -v openssl >/dev/null 2>&1; then
    sha_tool=openssl
  else
    echo "Content check error: line-sha256 entries need sha256sum, shasum or openssl; none found. Refusing to scan (failing closed)." >&2
    exit 2
  fi
}

# load_allowlist <file> <label> <eager|lazy>: parse
# <path><TAB><class><TAB><line ERE | line-sha256:hex><TAB><reason> entries into
# the allow_* arrays. A malformed entry fails the scan (exit 2) instead of being
# skipped. eager (full mode) validates every ERE up front; lazy (range mode)
# validates an ERE the first time an entry is consulted, with the same rules.
load_allowlist() {
  local src="$1" a_lineno=0 a_raw='' a_path a_class a_re a_reason a_sha a_checked
  allow_label="$2"
  n_allow=0
  [ -f "$src" ] || return 0
  while IFS= read -r a_raw || [ -n "$a_raw" ]; do
    a_lineno=$((a_lineno + 1))
    a_raw="${a_raw%$'\r'}"
    case "$a_raw" in '' | '#'*) continue ;; esac
    a_path='' a_class='' a_re='' a_reason=''
    IFS="$tab" read -r a_path a_class a_re a_reason <<<"$a_raw" || true
    if [ -z "$a_path" ] || [ -z "$a_class" ] || [ -z "$a_re" ] || [ -z "$a_reason" ]; then
      echo "Content check error: $allow_label:$a_lineno needs 4 tab-separated fields: path, class, line ERE, reason." >&2
      exit 2
    fi
    case "$content_classes" in
      *" $a_class "*) ;;
      *)
        echo "Content check error: $allow_label:$a_lineno names unknown class '$a_class'." >&2
        exit 2
        ;;
    esac
    a_sha=''
    a_checked=1
    case "$a_re" in
      line-sha256:*)
        # Exact-line mode: exempts only a line whose full text hashes to this.
        a_sha="${a_re#line-sha256:}"
        case "$a_sha" in
          *[!0-9a-f]* | '')
            echo "Content check error: $allow_label:$a_lineno needs line-sha256:<64 lowercase hex>." >&2
            exit 2
            ;;
        esac
        if [ "${#a_sha}" -ne 64 ]; then
          echo "Content check error: $allow_label:$a_lineno needs line-sha256:<64 lowercase hex>." >&2
          exit 2
        fi
        ensure_sha_tool
        ;;
      *)
        if [ "$3" = eager ]; then
          validate_ere "$a_re" "$allow_label:$a_lineno"
        else
          a_checked=0
        fi
        ;;
    esac
    allow_path[n_allow]="$a_path"
    allow_class[n_allow]="$a_class"
    allow_re[n_allow]="$a_re"
    allow_sha[n_allow]="$a_sha"
    allow_line[n_allow]="$a_lineno"
    allow_used[n_allow]=0
    allow_checked[n_allow]="$a_checked"
    n_allow=$((n_allow + 1))
  done <"$src"
}

n_allow=0
allow_label="$allow_file"
if [ "$mode" = full ]; then
  load_allowlist "$allow_file" "$allow_file" eager
fi

sha256_stdin() {
  local out
  case "$sha_tool" in
    sha256sum) out="$(sha256sum)" ;;
    shasum) out="$(shasum -a 256)" ;;
    *) out="$(openssl dgst -sha256 -r)" ;;
  esac
  printf '%s' "${out%% *}"
}

# exact_col <ERE> <line text> <from> <expected match> <where>: git grep -o
# --column gives an exact column only for the FIRST match on a line; later
# records carry a drifted column. Recompute a later match by matching the same
# ERE, with the same engine, on the rest of the line after the previous match
# (<from> bytes in), and require the same match text, else fail closed. Sets
# $exact to the 1-based byte column in the full line.
exact_col() {
  local st=0 e_file='' e_line='' e_col='' e_match=''
  printf '%s\n' "${2:$3}" >"$work_dir/rem"
  git -C "$work_dir" -c color.grep=never \
    grep --no-index -n -o --column -z -E -e "$1" -- rem >"$work_dir/rem.out" 2>/dev/null || st=$?
  if [ "$st" -eq 0 ]; then
    tr '\n' '\0' <"$work_dir/rem.out" >"$work_dir/rem.nul"
    { IFS= read -r -d '' e_file && IFS= read -r -d '' e_line &&
      IFS= read -r -d '' e_col && IFS= read -r -d '' e_match; } <"$work_dir/rem.nul" || st=3
  fi
  case "$e_col" in
    '' | *[!0-9]*) st=4 ;;
  esac
  if [ "$st" -ne 0 ] || [ "$e_match" != "$4" ]; then
    echo "Content check error: cannot pin the column of a later match on $5 (cross-check failed). Refusing to scan (failing closed)." >&2
    exit 2
  fi
  exact=$(($3 + e_col))
}

# allowlisted <path> <class> <line> <column> <match>: 0 when an entry covers
# that hit. A line-sha256 entry covers it when the flagged line's full text
# (its bytes without the newline) hashes to the entry's value. An ERE entry
# covers it when its ERE matches the flagged line at a byte span that overlaps
# [column, column + length of match); columns are exact 1-based byte offsets
# (see exact_col). The line is read from $blob_rev (the index in full mode, the
# commit that added it in range mode). Marks every covering entry as used.
allowlisted() {
  local i=0 hit=1 text='' fetched=0 line_sha='' st m_end a_file a_line a_col a_match a_end a_k a_prev_end
  m_end=$(($4 + ${#5} - 1))
  while [ "$i" -lt "$n_allow" ]; do
    if [ "${allow_path[i]}" = "$1" ] && [ "${allow_class[i]}" = "$2" ]; then
      if [ "$fetched" -eq 0 ]; then
        text="$(git cat-file blob "${blob_rev}:$1" 2>/dev/null | sed -n "$3p")" || text=''
        printf '%s\n' "$text" >"$work_dir/line"
        fetched=1
      fi
      if [ -n "${allow_sha[i]}" ]; then
        if [ -z "$line_sha" ]; then
          line_sha="$(printf '%s' "$text" | sha256_stdin)"
        fi
        if [ "$line_sha" = "${allow_sha[i]}" ]; then
          allow_used[i]=1
          hit=0
        fi
        i=$((i + 1))
        continue
      fi
      if [ "${allow_checked[i]}" -eq 0 ]; then
        validate_ere "${allow_re[i]}" "$allow_label:${allow_line[i]}"
        allow_checked[i]=1
      fi
      st=0
      git -C "$work_dir" -c color.grep=never \
        grep --no-index -n -o --column -z -E -e "${allow_re[i]}" -- line >"$work_dir/line.out" 2>/dev/null || st=$?
      if [ "$st" -gt 1 ]; then
        echo "Content check error: git grep exited $st on $allow_label:${allow_line[i]}." >&2
        exit 2
      fi
      if [ "$st" -eq 0 ]; then
        tr '\n' '\0' <"$work_dir/line.out" >"$work_dir/line.nul"
        a_k=0
        a_prev_end=0
        while IFS= read -r -d '' a_file <&6 && IFS= read -r -d '' a_line <&6 &&
          IFS= read -r -d '' a_col <&6 && IFS= read -r -d '' a_match <&6; do
          case "$a_col" in
            '' | *[!0-9]*)
              echo "Content check error: unparseable allowlist match record for $allow_label:${allow_line[i]}." >&2
              exit 2
              ;;
          esac
          if [ "$a_k" -gt 0 ]; then
            exact_col "${allow_re[i]}" "$text" "$a_prev_end" "$a_match" "$allow_label:${allow_line[i]}"
            a_col="$exact"
          fi
          a_k=$((a_k + 1))
          a_end=$((a_col + ${#a_match} - 1))
          a_prev_end="$a_end"
          if [ "$a_col" -le "$m_end" ] && [ "$4" -le "$a_end" ]; then
            allow_used[i]=1
            hit=0
            break
          fi
        done 6<"$work_dir/line.nul"
      fi
    fi
    i=$((i + 1))
  done
  return "$hit"
}

# Fail closed, never skip, when git is too old for --attr-source: first by the
# reported version, then by actually trying the option (a build can report a
# new version and still lack it, or the version line can be unparseable).
git_version_line="$(git --version 2>/dev/null || true)"
git_version="${git_version_line#git version }"
git_major="${git_version%%.*}"
git_minor_rest="${git_version#*.}"
git_minor="${git_minor_rest%%[!0-9]*}"
case "$git_major:$git_minor" in
  *[!0-9:]* | :* | *:) ;;
  *)
    if [ "$git_major" -lt 2 ] || { [ "$git_major" -eq 2 ] && [ "$git_minor" -lt 42 ]; }; then
      echo "Content check error: ${git_version_line:-git} is older than 2.42; the content scan needs git --attr-source. Refusing to scan (failing closed). Upgrade git." >&2
      exit 2
    fi
    ;;
esac
empty_tree="$(git hash-object -t tree /dev/null)"
if ! git --attr-source="$empty_tree" rev-parse --git-dir >/dev/null 2>&1; then
  echo "Content check error: ${git_version_line:-git} does not support --attr-source (needs git >= 2.42). Refusing to scan (failing closed) rather than let .gitattributes hide files." >&2
  exit 2
fi

# Tracked symlinks: git grep skips them, but their target text is committed
# content. Collect the targets, one per line, in a scratch dir and scan them with
# the same engine and table; a hit reports the symlink's path at line 1.
n_links=0
if [ "$mode" = full ]; then
  git ls-files -s -z >"$work_dir/index"
else
  : >"$work_dir/index"
fi
while IFS= read -r -d '' l_rec; do
  case "$l_rec" in
    '120000 '*) ;;
    *) continue ;;
  esac
  l_path="${l_rec#*"$tab"}"
  l_sha="${l_rec#120000 }"
  l_sha="${l_sha%% *}"
  l_target="$(git cat-file blob "$l_sha" | tr '\n\r' '  ')"
  printf '%s\n' "$l_target" >>"$work_dir/targets"
  n_links=$((n_links + 1))
  link_path[n_links]="$l_path"
done <"$work_dir/index"

# scan_classes: run every pattern class over the current source (the index in
# full mode, the pushed range's added lines in range mode) and record hits.
scan_classes() {
while IFS="$tab" read -r c_class c_regex <&3; do
  [ -n "$c_class" ] || continue
  : >"$grep_out"
  if [ "$mode" = range ]; then
    r_status=0
    git -C "$work_dir" -c color.grep=never \
      grep --no-index -n -o --column -z -E -e "$c_regex" -- added >"$work_dir/aout" || r_status=$?
    if [ "$r_status" -gt 1 ]; then
      echo "Content check error: git grep exited $r_status on the pushed range for class $c_class." >&2
      exit 2
    fi
    if [ "$r_status" -eq 0 ]; then
      # Map each added line back to <commit>TAB<path>TAB<new line number>.
      tr '\n' '\0' <"$work_dir/aout" >"$work_dir/aout.nul"
      while IFS= read -r -d '' r_file <&5 && IFS= read -r -d '' r_idx <&5 &&
        IFS= read -r -d '' r_col <&5 && IFS= read -r -d '' r_match <&5; do
        case "$r_idx:$r_col" in
          *[!0-9:]* | :* | *:)
            echo "Content check error: unparseable pushed-range record for class $c_class." >&2
            exit 2
            ;;
        esac
        r_map="$(sed -n "${r_idx}p" "$work_dir/added.map")"
        r_rest="${r_map#*"$tab"}"
        printf '%s\0%s\0%s\0%s\n' "${r_map%%"$tab"*}$tab${r_rest%"$tab"*}" "${r_rest##*"$tab"}" \
          "$r_col" "$r_match" >>"$grep_out"
      done 5<"$work_dir/aout.nul"
    fi
  else
  g_status=0
  git --attr-source="$empty_tree" -c color.grep=never \
    grep --cached -I -n -o --column -z -E -e "$c_regex" >"$grep_out" || g_status=$?
  if [ "$g_status" -gt 1 ]; then
    echo "Content check error: git grep exited $g_status for class $c_class." >&2
    exit 2
  fi
  fi
  if [ "$n_links" -gt 0 ]; then
    l_status=0
    git -C "$work_dir" -c color.grep=never \
      grep --no-index -n -o --column -z -E -e "$c_regex" -- targets >"$work_dir/out" || l_status=$?
    if [ "$l_status" -gt 1 ]; then
      echo "Content check error: git grep exited $l_status on symlink targets for class $c_class." >&2
      exit 2
    fi
    if [ "$l_status" -eq 0 ]; then
      tr '\n' '\0' <"$work_dir/out" >"$work_dir/out.nul"
      while IFS= read -r -d '' l_file <&5 && IFS= read -r -d '' l_idx <&5 &&
        IFS= read -r -d '' l_col <&5 && IFS= read -r -d '' l_match <&5; do
        case "$l_idx:$l_col" in
          *[!0-9:]* | :* | *:)
            echo "Content check error: unparseable symlink-target record for class $c_class." >&2
            exit 2
            ;;
        esac
        printf '%s\0%s\0%s\0%s\n' "${link_path[l_idx]}" 1 "$l_col" "$l_match" >>"$grep_out"
      done 5<"$work_dir/out.nul"
    fi
  fi
  if [ ! -s "$grep_out" ]; then
    continue
  fi
  # Records are <path>NUL<line>NUL<column>NUL<match>LF; a match never spans a
  # line.
  tr '\n' '\0' <"$grep_out" >"$grep_nul"
  c_count=0
  prev_key=''
  prev_end=0
  key_text=''
  key_fetched=0
  while IFS= read -r -d '' h_path <&4 && IFS= read -r -d '' h_line <&4 &&
    IFS= read -r -d '' h_col <&4 && IFS= read -r -d '' h_match <&4; do
    case "$h_line:$h_col" in
      *[!0-9:]* | :* | *:)
        echo "Content check error: unparseable git grep record for class $c_class." >&2
        exit 2
        ;;
    esac
    if [ "$mode" = range ]; then
      # Range records carry <commit>TAB<path>; lines are read from that commit.
      blob_rev="${h_path%%"$tab"*}"
      h_path="${h_path#*"$tab"}"
    fi
    # Records for one line arrive together; only the first has an exact column.
    h_key="$blob_rev|$h_path|$h_line"
    if [ "$h_key" = "$prev_key" ]; then
      if [ "$key_fetched" -eq 0 ]; then
        key_text="$(git cat-file blob "${blob_rev}:$h_path" 2>/dev/null | sed -n "${h_line}p")" || key_text=''
        key_fetched=1
      fi
      exact_col "$c_regex" "$key_text" "$prev_end" "$h_match" "$h_path:$h_line ($c_class)"
      h_col="$exact"
    else
      prev_key="$h_key"
      key_fetched=0
    fi
    prev_end=$((h_col - 1 + ${#h_match}))
    h_note=''
    h_forced=0
    if [ "$c_class" = home-path ]; then
      # The match ends in "/", in one terminator (whitespace, a quote, a
      # backtick, ":" or ")"), or at the end of the line.
      h_rest="${h_match#/*/}"
      case "$h_rest" in
        */) h_name="${h_rest%/}" ;;
        *[[:space:]\"\'\`:\)]) h_name="${h_rest%?}" ;;
        *) h_name="$h_rest" ;;
      esac
      h_lname="$(printf '%s' "$h_name" | tr '[:upper:]' '[:lower:]')"
      # The forbidden set is comma-separated, so a name containing a comma can
      # never be a member (and must not match the separators).
      case "$h_lname" in
        *,*) ;;
        *)
          case "$forbidden_names" in
            *",$h_lname,"*)
              h_forced=1
              h_note=' (forbidden name; neutral set and allowlist ignored)'
              ;;
          esac
          ;;
      esac
      if [ "$h_forced" -eq 0 ]; then
        case "$h_name" in
          *,*) ;;
          *) case "$neutral_names" in *",$h_name,"*) continue ;; esac ;;
        esac
      fi
    fi
    if [ "$h_forced" -eq 0 ] && allowlisted "$h_path" "$c_class" "$h_line" "$h_col" "$h_match"; then
      continue
    fi
    case "$c_class" in
      token-* | private-key)
        printf '%s:%s: %s (%s chars)\n' "$h_path" "$h_line" "$c_class" \
          "${#h_match}" >>"$content_out"
        ;;
      *)
        printf '%s:%s: %s: %s... (%s chars)%s\n' "$h_path" "$h_line" "$c_class" \
          "${h_match:0:4}" "${#h_match}" "$h_note" >>"$content_out"
        ;;
    esac
    c_count=$((c_count + 1))
  done 4<"$grep_nul"
  if [ "$c_count" -gt 0 ]; then
    violations=$((violations + c_count))
    summary="${summary}  ${c_class}: ${c_count}
"
  fi
done 3<<'CONTENT_PATTERNS'
home-path	/[Uu][Ss][Ee][Rr][Ss]/[^/[:space:]]+/|/[Hh][Oo][Mm][Ee]/[^/[:space:]]+/|/[Uu][Ss][Ee][Rr][Ss]/[^/[:space:]"'`:)]+([[:space:]"'`:)]|$)|/[Hh][Oo][Mm][Ee]/[^/[:space:]"'`:)]+([[:space:]"'`:)]|$)
vault-deep-path	(~|[$]HOME|[$][{]HOME[}]|/[Uu][Ss][Ee][Rr][Ss]/[^/[:space:]]+|/[Hh][Oo][Mm][Ee]/[^/[:space:]]+)/\.minni/[^/[:space:]]+-vault/[^][:space:]`'")>,;:|]
plan-id	plan-[0-9a-f]{16}
token-assignment	(_API_KEY|_TOKEN|_SECRET|_KEY|PASSWORD)[[:space:]]*=[[:space:]]*[^$"'<[:space:]{[][^[:space:]]{7,}|(_API_KEY|_SECRET|PASSWORD)[[:space:]]*=[[:space:]]*["'][^$"'<[:space:]{[][^"'[:space:]]{7,}
token-anthropic	[s]k-ant-
token-sk	sk-[A-Za-z0-9]{20,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}
token-github	ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}|[g]ithub_pat_
token-slack	xox[baprs]-
token-google	AIza[0-9A-Za-z_-]{20,}
token-aws	AKIA[0-9A-Z]{16,}|ASIA[0-9A-Z]{16,}
token-gitlab	[g]lpat-
token-digitalocean	[d]op_v1_
token-huggingface	hf_[A-Za-z0-9]{20,}
token-replicate	r8_[A-Za-z0-9]{20,}
token-stripe	[p]k_live_|[s]k_live_
private-key	-----BEGIN [A-Z ]*PRIVATE KEY-----
CONTENT_PATTERNS
}

# is_zero <sha>: git's all-zero object id (any length).
is_zero() {
  case "$1" in
    '' | *[!0]*) return 1 ;;
    *) return 0 ;;
  esac
}

# range_fail <message>: a pushed range that cannot be computed fails closed.
range_fail() {
  echo "Content check error (pre-push): $1 Refusing to push unscanned content (failing closed)." >&2
  exit 2
}

# default_ref: the remote's default branch, for the merge-base of a new branch.
default_ref=''
resolve_default_ref() {
  local ref
  [ -z "$default_ref" ] || return 0
  ref="$(git symbolic-ref -q "refs/remotes/$push_remote/HEAD" 2>/dev/null || true)"
  if [ -n "$ref" ] && git rev-parse -q --verify "${ref}^{commit}" >/dev/null 2>&1; then
    default_ref="$ref"
    return 0
  fi
  if git rev-parse -q --verify "refs/remotes/$push_remote/main^{commit}" >/dev/null 2>&1; then
    default_ref="refs/remotes/$push_remote/main"
    return 0
  fi
  return 1
}

# collect_added <base> <tip>: for every commit in <base>..<tip> (all of them,
# not only first parents), write the lines it adds against its FIRST parent to
# $work_dir/added, one per line, and <commit>TAB<path>TAB<new line number> for
# each to $work_dir/added.map. A merge is diffed against its first parent only;
# a root commit against the empty tree. Attributes are ignored, as in full mode,
# so binary means a NUL near the start; a path git must C-quote fails closed.
collect_added() {
  local d_status=0 a_status=0
  git --attr-source="$empty_tree" -c core.quotePath=false -c color.diff=never \
    log --reverse --no-color --no-notes --no-show-signature --format='commit %H' \
    -p -U0 --no-ext-diff --no-textconv --no-relative --find-renames \
    --diff-merges=first-parent --root --src-prefix=a/ --dst-prefix=b/ \
    "$1..$2" -- >"$work_dir/diff" || d_status=$?
  if [ "$d_status" -ne 0 ]; then
    range_fail "git log -p $1..$2 exited $d_status."
  fi
  : >"$work_dir/added"
  : >"$work_dir/added.map"
  awk -v added="$work_dir/added" -v map="$work_dir/added.map" -v tab="$tab" '
    /^commit [0-9a-f]+$/ { rev = $2; in_hunk = 0; path = ""; next }
    /^diff --git / { in_hunk = 0; path = ""; next }
    !in_hunk && /^\+\+\+ / {
      p = substr($0, 5)
      if (p == "/dev/null") { path = ""; next }
      if (substr(p, 1, 1) == "\"") { exit 3 }
      if (substr(p, length(p), 1) == tab) { p = substr(p, 1, length(p) - 1) }
      if (substr(p, 1, 2) != "b/") { exit 4 }
      path = substr(p, 3)
      next
    }
    /^@@ / { in_hunk = 1; split($3, part, ","); line = substr(part[1], 2) + 0; next }
    in_hunk && /^\+/ {
      if (path != "" && rev != "") {
        print substr($0, 2) > added
        printf "%s%s%s%s%d\n", rev, tab, path, tab, line > map
      }
      line++
      next
    }
  ' "$work_dir/diff" || a_status=$?
  case "$a_status" in
    0) ;;
    3) range_fail "a changed path in $1..$2 needs C-quoting (tab, newline, quote or backslash in its name), so its lines cannot be mapped." ;;
    *) range_fail "could not parse git log -p $1..$2 (awk exited $a_status)." ;;
  esac
}

violations=0
summary=''
added_total=0
range_count=0
if [ "$mode" = full ]; then
  blob_rev=''
  scan_classes
  i=0
  while [ "$i" -lt "$n_allow" ]; do
    if [ "${allow_used[i]}" -ne 1 ]; then
      printf '%s:%s: unused allowlist entry (%s, %s); remove it or fix its line ERE\n' \
        "$allow_file" "${allow_line[i]}" "${allow_path[i]}" "${allow_class[i]}" >>"$content_out"
      violations=$((violations + 1))
      summary="${summary}  unused-allowlist-entry: 1
"
    fi
    i=$((i + 1))
  done
else
  # git's pre-push stdin: <local ref> <local sha> <remote ref> <remote sha>.
  cat >"$work_dir/stdin"
  while IFS=' ' read -r p_lref p_lsha p_rref p_rsha p_extra || [ -n "${p_lref:-}" ]; do
    [ -n "${p_lref:-}" ] || continue
    if [ -z "${p_rsha:-}" ] || [ -n "${p_extra:-}" ]; then
      range_fail "malformed pre-push line (expected <local ref> <local sha> <remote ref> <remote sha>)."
    fi
    for p_sha in "$p_lsha" "$p_rsha"; do
      case "$p_sha" in
        *[!0-9a-f]*) range_fail "not an object id: $p_sha." ;;
      esac
      if [ "${#p_sha}" -ne 40 ] && [ "${#p_sha}" -ne 64 ]; then
        range_fail "not an object id: $p_sha."
      fi
    done
    if is_zero "$p_lsha"; then
      continue # deleting $p_rref adds nothing
    fi
    p_tip="$(git rev-parse -q --verify "${p_lsha}^{commit}" 2>/dev/null)" ||
      range_fail "the local sha $p_lsha for $p_lref is not a commit in this repository."
    if is_zero "$p_rsha"; then
      resolve_default_ref ||
        range_fail "new ref $p_rref, but the default branch of remote '$push_remote' cannot be resolved (refs/remotes/$push_remote/HEAD or refs/remotes/$push_remote/main); fetch it first."
      p_base="$(git merge-base "$default_ref" "$p_tip" 2>/dev/null)" ||
        range_fail "new ref $p_rref has no merge-base with $default_ref."
    else
      p_base="$(git rev-parse -q --verify "${p_rsha}^{commit}" 2>/dev/null)" ||
        range_fail "the remote sha $p_rsha for $p_rref is not in this repository; fetch first."
    fi
    # Only pushed allowlist entries count: read the file from the tip commit.
    if git cat-file -e "${p_tip}:$allow_file" 2>/dev/null; then
      git cat-file blob "${p_tip}:$allow_file" >"$work_dir/allow.range" ||
        range_fail "cannot read $allow_file at $p_tip."
    else
      : >"$work_dir/allow.range"
    fi
    load_allowlist "$work_dir/allow.range" "${p_tip}:$allow_file" lazy
    collect_added "$p_base" "$p_tip"
    p_added="$(wc -l <"$work_dir/added" | tr -d ' ')"
    added_total=$((added_total + p_added))
    range_count=$((range_count + 1))
    if [ "$p_added" -gt 0 ]; then
      scan_classes
    fi
  done <"$work_dir/stdin"
fi

if [ "$violations" -gt 0 ]; then
  if [ "$mode" = range ]; then
    echo "Content check failed (pre-push). These lines added by the push carry a home path, a deep vault path, a plan id, or a credential shape (match text truncated; none shown for secret classes):" >&2
  else
    echo "Content check failed. These tracked lines carry a home path, a deep vault path, a plan id, or a credential shape (match text truncated; none shown for secret classes):" >&2
  fi
  cat "$content_out" >&2
  echo "Counts per class:" >&2
  printf '%s' "$summary" >&2
  echo "Fix the content, or add a precise entry with a reason to $allow_file." >&2
  exit 1
fi

if [ "$mode" = range ]; then
  echo "Public-content check passed (pre-push: $added_total added lines in $range_count ranges)."
else
  echo "Public-content check passed."
fi
