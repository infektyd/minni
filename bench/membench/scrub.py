"""Scrub gate — secret/PII redaction + cryptographic scrub binding (§5.1).

Slice s2(a). A redaction pass over snapshot text removes/aliases secrets and PII
per a configurable denylist:

- **API-key / token patterns** — ``sk-...`` (incl. ``sk-ant-api03-`` Anthropic
  keys), ``ghp_``/``github_pat_``, ``AKIA...``, Slack ``xox*``, bearer tokens.
- **PEM private-key blocks** — the whole ``-----BEGIN ... PRIVATE KEY-----`` …
  ``-----END ... PRIVATE KEY-----`` armored block (fix 6).
- **JWTs** — ``eyJ...``-headed three-segment ``header.payload.signature`` tokens
  (fix 6).
- **Inline credential assignments** — ``password=`` / ``secret=`` / ``token=`` /
  ``api_key=`` value spans, redacting ONLY the value and keeping the key name so
  prose is not corrupted (fix 6). QUOTED multi-word values (e.g.
  ``password="correct horse battery staple"``) are redacted as one span — the
  closing quote delimits the value so internal spaces are covered (review fix 3).
- **Email addresses** — replaced with a fixed alias.
- **Real-name -> alias map** — by default maps the operator's real name to the
  ``Infektyd`` alias.

The scrub pass also applies the SAME denylist to each file's DOC-ID (its
corpus-relative path, filename-derived per ``corpus._iter_corpus_files``):
a source file literally named after a person (e.g. ``jane-doe-notes.md`` or
``jane.doe@example.com.md``) is RENAMED on disk to a scrubbed doc-id before the
text pass runs, so the real name/email never survives as the ``doc_id`` key in
``manifest.json``, ``scrub_spans.jsonl``, or downstream gold labels (X6, §5.1).
:func:`verify_scrubbed` re-scans doc-ids (not just file bytes) for residual
name/email patterns and rejects a snapshot whose doc-ids still carry PII.

It records a ``scrub_manifest_hash`` = SHA-256 over the canonical sorted list of
redacted spans AND the resulting scrubbed file tree (§5.1). The cross-check rule:
the loader/runner MUST REJECT a corpus claiming ``scrubbed=True`` unless the
``scrub_manifest_hash`` RECOMPUTES and MATCHES over the actual scrubbed bytes. A
bare ``scrubbed=True`` flag over unscrubbed bytes is rejected — the hash is the
real gate, not the boolean.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import _iter_corpus_files, compute_content_hash
from .paths import assert_private_path
from .snapshot import (
    CORPUS_SUBDIR,
    MANIFEST_FILENAME,
    corpus_subdir,
    load_manifest,
)

# ── Redaction patterns (configurable denylist) ───────────────────────────────
# Replacement sentinels are stable strings so scrubbing is deterministic and the
# scrub_manifest_hash is reproducible.
REDACTED_KEY = "[REDACTED_KEY]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
# Distinct sentinels for the new (fix 6) classes so the scrub manifest records
# WHAT was redacted, not just that something was.
REDACTED_PEM = "[REDACTED_PRIVATE_KEY]"
REDACTED_JWT = "[REDACTED_JWT]"
REDACTED_SECRET = "[REDACTED_SECRET]"
DEFAULT_NAME_ALIAS = "Infektyd"

# All replacement sentinels — used by the assignment-pattern idempotence guard so
# a re-scan of already-scrubbed bytes never treats a sentinel as a fresh secret
# (nit a). REDACTED_EMAIL/the name alias are not assignment VALUES so they are not
# needed here, but every sentinel that can legitimately appear as a redacted
# credential value is included.
_ALL_SENTINELS = frozenset(
    {REDACTED_KEY, REDACTED_PEM, REDACTED_JWT, REDACTED_SECRET, REDACTED_EMAIL}
)

# API keys / tokens: sk-... (OpenAI-shape), generic long base62 token after a
# token-ish prefix, and bearer tokens. Ordered longest-first conceptually; the
# engine applies them in sequence.
#
# NOTE on sk-ant-api03- (Anthropic) coverage (fix 6): the sk- pattern below
# matches ``sk-`` then an optional ``proj-`` then >=16 of [A-Za-z0-9_-]. An
# Anthropic key ``sk-ant-api03-<base64url>`` has ``ant-api03-<...>`` as a single
# [A-Za-z0-9_-] run >=16 chars, so it is ALREADY covered; a dedicated test plants
# one and asserts redaction (no separate pattern needed, but verified).
_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # sk-... / sk-proj-... / sk-ant-api03-... style (>=16 trailing key chars)
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    # ghp_/gho_/github_pat_ and similar prefixed tokens
    re.compile(r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Bearer tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),
    # Slack-style xoxb/xoxp tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)

# ── New (fix 6) high-value secret classes ────────────────────────────────────
# PEM private-key BLOCKS — match the whole armored block, header to footer, so
# the key body is removed in one span. DOTALL so the base64 body (with newlines)
# is captured. Non-greedy to stop at the FIRST matching END line (one block per
# match). Anchored on the literal PEM armor so it cannot over-match prose.
_PEM_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    r".*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)

# JWTs — three base64url segments separated by dots: header.payload.signature.
# Each segment is base64url ([A-Za-z0-9_-]). Require realistic minimum lengths so
# an ordinary dotted token like "a.b.c" or a version "1.2.3" cannot match: the
# header is >=10, payload >=10, signature >=10 chars. JWT headers virtually always
# begin "eyJ" (base64 of '{"'), so anchor on it to avoid matching arbitrary
# triple-dotted alphanumerics (e.g. package coordinates) — narrow, not greedy.
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)

# Inline credential ASSIGNMENTS — password=, secret=, token= (and api_key=,
# apikey=) with an optional quote, capturing the VALUE only so the KEY name stays
# (redacting just the secret, never the surrounding prose). Case-insensitive key;
# ``=`` or ``:`` separator with optional spaces. The value must be non-trivial
# (>=6 chars) so an empty or placeholder ``password=`` and a bare ``token: true``
# boolean are left alone (avoids corpus corruption). Separator whitespace is
# restricted to SPACES/TABS (no newline) so a bare ``password=\n`` cannot let the
# value run consume the NEXT line's content.
#
# The value has TWO alternatives (review fix 3):
#   1. QUOTED span — ``"<value>"`` / ``'<value>'``: a multi-word credential like
#      password="correct horse battery staple" MUST be redacted. The old single
#      ``[^\s'"]{6,}`` value stopped at the first space, so a quoted passphrase
#      survived scrubbing — a scrub-gate FALSE NEGATIVE (verify_scrubbed re-scans
#      with the same pattern, so the residual went undetected). The quoted branch
#      permits internal spaces because the closing quote delimits it; it cannot
#      run away across lines because the body excludes the quote chars (and any
#      newline before a closing quote leaves the value spanning at most the line).
#   2. UNQUOTED single-token span — ``[^\s'"]{6,}``: retains the anti-swallow
#      behaviour for bare ``password=hunter2foo`` (stops at the first space/quote).
# Quoted is tried FIRST (alternation order) so a quoted value is matched as one
# span rather than the unquoted branch grabbing only its first token.
#
# Branch-minimum ALIGNMENT (review suggestion 4): the closed-quote branch (1)
# uses ``{5,}`` so a properly-closed 5-char value (``password='abcde'``) is
# matched by it — consuming its CLOSING quote — instead of falling through to the
# unclosed-quote branch (3, ``qval_open >= 5``) which would match ``'abcde`` and
# leave a stray closing ``'`` as literal text. Branch 1 is tried first in the
# alternation, so whenever a closing quote exists the closed branch wins; the
# unclosed branch only fires for a genuinely unterminated quote. The unquoted
# branch (2) keeps its ``{6,}`` minimum so a bare ``token=true``-style trivial
# value is left alone.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b[ \t]*[:=][ \t]*)"
    r"(?:"
    r"(?P<q>['\"])(?P<qval>[^'\"]{5,})(?P=q)"  # 1. quoted (spaces allowed inside)
    r"|"
    r"(?P<qopen>['\"])(?P<qval_open>[^\s'\"]{5,})"  # 3. leading-quote, NO close
    r"|"
    r"(?P<val>[^\s'\"]{6,})"  # 2. unquoted single token
    r")",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# The PRIVATE, gitignored sidecar that holds the plaintext real-name keys needed
# to re-scan for residual name PII. It lives under scrub_spans/_private/ so the
# repo `**/_private/` ignore rule covers it; the plaintext names NEVER enter
# manifest.json (which can land on a public path when allow_public=True). The
# manifest carries only SALTED hashes of these names as a "considered" signal.
NAME_KEYS_DIRNAME = "scrub_spans"
NAME_KEYS_PRIVATE_SUBDIR = "_private"
NAME_KEYS_FILENAME = "name_keys.json"

# Byte cap for the scrub sidecars (scrub_spans.jsonl, name_keys.json). These live
# in operator/attacker-controlled snapshot dirs and are slurped whole into memory
# during the scrub gate; without a stat()-before-read cap a multi-GB file OOMs the
# verifier before the hash gate runs. Mirrors goldset.MAX_GOLD_FILE_BYTES.
MAX_SCRUB_SPANS_FILE_BYTES = 32 * 1024 * 1024


def _name_keys_path(snapshot_dir: Path) -> Path:
    return (
        snapshot_dir
        / NAME_KEYS_DIRNAME
        / NAME_KEYS_PRIVATE_SUBDIR
        / NAME_KEYS_FILENAME
    )


def _hash_name_key(name: str, salt: str) -> str:
    """Salted SHA-256 of a real name (for the manifest's 'considered' signal)."""
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(b"\x00")
    h.update(name.encode("utf-8"))
    return h.hexdigest()


@dataclass(frozen=True)
class ScrubPolicy:
    """Configurable denylist for the scrub pass.

    ``name_aliases`` maps a real name (matched case-insensitively as a whole
    word) to an alias. Defaults to the operator's documented alias mapping;
    callers pass the real name->alias map explicitly for real runs.
    """

    name_aliases: dict[str, str] = field(default_factory=dict)
    redact_keys: bool = True
    redact_emails: bool = True


def default_policy(operator_real_name: str | None = None) -> ScrubPolicy:
    """A default policy that maps the operator's real name to ``Infektyd``."""
    aliases: dict[str, str] = {}
    if operator_real_name:
        aliases[operator_real_name] = DEFAULT_NAME_ALIAS
    return ScrubPolicy(name_aliases=aliases)


@dataclass(frozen=True)
class RedactionSpan:
    """One redaction applied to one file, recorded for the scrub manifest."""

    doc_id: str
    kind: str  # "key" | "email" | "name"
    start: int  # char offset in the ORIGINAL text
    end: int
    replacement: str

    def canonical(self) -> str:
        return f"{self.doc_id}\t{self.kind}\t{self.start}\t{self.end}\t{self.replacement}"


def _name_patterns(policy: ScrubPolicy) -> list[tuple[re.Pattern[str], str]]:
    out: list[tuple[re.Pattern[str], str]] = []
    for real, alias in policy.name_aliases.items():
        # Whole-word, case-insensitive. re.escape so a name with regex chars is
        # treated literally.
        out.append((re.compile(rf"\b{re.escape(real)}\b", re.IGNORECASE), alias))
    return out


def scrub_text(
    doc_id: str, text: str, policy: ScrubPolicy
) -> tuple[str, list[RedactionSpan]]:
    """Scrub one document's text, returning (scrubbed_text, spans).

    Spans are recorded against the ORIGINAL text offsets, in left-to-right order,
    which is deterministic for a given (text, policy).
    """
    spans: list[RedactionSpan] = []

    # Collect all matches against the ORIGINAL text first (so offsets are stable
    # and independent of replacement-length shifts), then rebuild left-to-right.
    matches: list[tuple[int, int, str, str]] = []  # (start, end, kind, replacement)

    if policy.redact_keys:
        for pat in _KEY_PATTERNS:
            for m in pat.finditer(text):
                matches.append((m.start(), m.end(), "key", REDACTED_KEY))
        # PEM private-key blocks — redact the whole armored block (fix 6).
        for m in _PEM_PATTERN.finditer(text):
            matches.append((m.start(), m.end(), "pem", REDACTED_PEM))
        # JWTs — redact the whole three-segment token (fix 6).
        for m in _JWT_PATTERN.finditer(text):
            matches.append((m.start(), m.end(), "jwt", REDACTED_JWT))
        # Inline credential assignments — redact ONLY the value, KEEP the key name
        # and separator so prose like "the password=..." stays readable (fix 6).
        for m in _ASSIGNMENT_PATTERN.finditer(text):
            # The value came from one of THREE branches: the quoted branch
            # (groups q/qval), the leading-quote-no-close branch (groups
            # qopen/qval_open — an accidentally unclosed quote, review fix), or
            # the unquoted branch (group val). Normalise to (open_quote,
            # close_quote, value). Only a properly closed quote re-emits a
            # trailing quote; the unclosed branch leaves the close empty so we do
            # not invent a delimiter the source never had.
            if m.group("qval") is not None:
                open_q = close_q = m.group("q")
                value = m.group("qval")
            elif m.group("qval_open") is not None:
                open_q = m.group("qopen")
                close_q = ""
                value = m.group("qval_open")
            else:
                open_q = close_q = ""
                value = m.group("val")
            # IDEMPOTENCE: skip an already-redacted value so a re-scan of scrubbed
            # bytes (residual_secrets / verify_scrubbed) does NOT flag a sentinel
            # itself as a fresh secret — that would make a genuinely scrubbed tree
            # fail the gate. Check ALL sentinels, not just REDACTED_SECRET (nit a):
            # a PEM/JWT/key value that landed as an assignment value is replaced by
            # its own sentinel (e.g. ``password=[REDACTED_PRIVATE_KEY]``), and a
            # re-scan must not treat that sentinel as a new secret to redact again.
            if value in _ALL_SENTINELS:
                continue
            # Replacement preserves the captured key+separator and the quote;
            # only the secret value is swapped for the sentinel.
            repl = m.group("key") + open_q + REDACTED_SECRET + close_q
            matches.append((m.start(), m.end(), "secret", repl))
    if policy.redact_emails:
        for m in _EMAIL_PATTERN.finditer(text):
            matches.append((m.start(), m.end(), "email", REDACTED_EMAIL))
    for pat, alias in _name_patterns(policy):
        for m in pat.finditer(text):
            matches.append((m.start(), m.end(), "name", alias))

    # Resolve overlaps by preferring the LONGER match (security fix): when two
    # matches overlap, the wider span wins. This matters when a PEM private-key
    # block is the VALUE of an assignment, e.g.
    # ``password=-----BEGIN RSA PRIVATE KEY-----\n<body>\n-----END...``. There the
    # _ASSIGNMENT_PATTERN match starts EARLIER (at ``password=``) but its value
    # regex ``[^\s'"]{6,}`` stops at the first whitespace, so it covers only
    # ``-----BEGIN`` and would leave the key BODY in the clear. The _PEM_PATTERN
    # match is much longer (the whole armored block). A start-first greedy keep
    # would pick the short assignment span and skip the overlapping PEM block;
    # a LONGEST-first greedy keep picks the PEM block instead, so the body is
    # fully redacted. The prior "key subsuming an email inside it wins" behaviour
    # is preserved — the key span is longer than the email it contains.
    #
    # Sort longest-first (ties broken by earliest start, then kind for
    # determinism), greedily accept a match only if it overlaps no already-chosen
    # span, then re-sort the accepted spans into left-to-right order for emit.
    matches.sort(key=lambda t: (-(t[1] - t[0]), t[0], t[2]))
    chosen: list[tuple[int, int, str, str]] = []
    for cand in matches:
        c_start, c_end = cand[0], cand[1]
        if any(c_start < ch[1] and ch[0] < c_end for ch in chosen):
            continue  # overlaps an already-chosen (longer/earlier) span; skip
        chosen.append(cand)
    chosen.sort(key=lambda t: t[0])

    # Rebuild the scrubbed text left-to-right.
    out_parts: list[str] = []
    cursor = 0
    for start, end, kind, repl in chosen:
        out_parts.append(text[cursor:start])
        out_parts.append(repl)
        cursor = end
        spans.append(
            RedactionSpan(
                doc_id=doc_id, kind=kind, start=start, end=end, replacement=repl
            )
        )
    out_parts.append(text[cursor:])
    return "".join(out_parts), spans


def _filename_name_patterns(
    policy: ScrubPolicy,
) -> list[tuple[re.Pattern[str], str]]:
    """Name patterns tolerant of filename-style separators (X6, §5.1).

    A real name in prose is space-separated ("Jane Doe"), but the SAME name in a
    filename is conventionally ``-``/``_``/``.``-separated ("jane-doe",
    "jane_doe", "Jane.Doe"). ``_name_patterns`` (used for file TEXT) only matches
    the literal space form, so it silently misses the filename spelling — this
    builds an equivalent pattern per real name that accepts space, ``-``, ``_``,
    or ``.`` between the name's words, still whole-word and case-insensitive.
    """
    out: list[tuple[re.Pattern[str], str]] = []
    for real, alias in policy.name_aliases.items():
        words = [w for w in re.split(r"\s+", real.strip()) if w]
        if not words:
            continue
        escaped = [re.escape(w) for w in words]
        pattern = r"\b" + r"[-_. ]+".join(escaped) + r"\b"
        out.append((re.compile(pattern, re.IGNORECASE), alias))
    return out


def scrub_doc_id(doc_id: str, policy: ScrubPolicy) -> tuple[str, bool]:
    """Apply the name/email denylist to a corpus-relative doc-id (X6, §5.1).

    ``doc_id`` is filename-derived (``corpus._iter_corpus_files``), so a source
    file literally NAMED after a person or an email address leaks that PII into
    ``manifest.json`` / ``scrub_spans.jsonl`` / gold labels even after the file's
    TEXT is scrubbed — the doc-id itself was never touched. This runs the same
    email + real-name->alias patterns used by :func:`scrub_text` over the doc-id
    string (matched per path SEGMENT, so a directory name can be scrubbed too,
    and the ``.md``-style extension is preserved), returning the scrubbed doc-id
    and whether it changed. Key/secret/PEM/JWT/assignment patterns are NOT
    applied here — those match multi-char runs that would corrupt an otherwise
    innocuous filename; only email + explicit real-name aliases are meaningful
    doc-id PII classes.
    """
    parts = doc_id.split("/")
    changed = False
    out_parts: list[str] = []
    for part in parts:
        stem, dot, ext = part.rpartition(".")
        segment = stem if dot else part
        new_segment = segment
        if policy.redact_emails:
            new_segment = _EMAIL_PATTERN.sub(REDACTED_EMAIL, new_segment)
        for pat, alias in _filename_name_patterns(policy):
            new_segment = pat.sub(alias, new_segment)
        rebuilt = f"{new_segment}.{ext}" if dot else new_segment
        if rebuilt != part:
            changed = True
        out_parts.append(rebuilt)
    return "/".join(out_parts), changed


def rename_scrubbed_doc_ids(
    corpus_dir: str | os.PathLike[str], policy: ScrubPolicy
) -> dict[str, str]:
    """Rename on-disk files whose doc-id carries name/email PII (X6, §5.1).

    Walks the corpus dir (same enumeration as the s1 loader), computes each
    file's scrubbed doc-id via :func:`scrub_doc_id`, and RENAMES the file when
    it changed. Collisions (two distinct original doc-ids scrubbing to the same
    target) are disambiguated by appending a short content-hash suffix to the
    scrubbed stem, so no file is silently overwritten/lost. Returns a
    ``{old_doc_id: new_doc_id}`` map for renamed files only (empty if nothing
    changed) — used by :func:`scrub_snapshot` to keep the manifest/spans in
    sync with the renamed tree.
    """
    corpus_dir = Path(corpus_dir)
    renames: dict[str, str] = {}
    used: set[str] = {doc_id for doc_id, _ in _iter_corpus_files(corpus_dir)}
    for doc_id, abs_path in _iter_corpus_files(corpus_dir):
        new_doc_id, changed = scrub_doc_id(doc_id, policy)
        if not changed:
            continue
        if new_doc_id in used and new_doc_id != doc_id:
            # Disambiguate a collision with a short, deterministic suffix derived
            # from the ORIGINAL doc-id (not the file bytes, which may not exist
            # yet at this offset if a prior rename already moved a colliding
            # sibling) — stable across repeated runs over the same source tree.
            digest = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:8]
            stem, dot, ext = new_doc_id.rpartition(".")
            base = stem if dot else new_doc_id
            candidate = f"{base}-{digest}.{ext}" if dot else f"{base}-{digest}"
            n = 1
            while candidate in used:
                candidate = (
                    f"{base}-{digest}-{n}.{ext}" if dot else f"{base}-{digest}-{n}"
                )
                n += 1
            new_doc_id = candidate
        used.discard(doc_id)
        used.add(new_doc_id)
        new_path = corpus_dir / new_doc_id
        new_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.rename(new_path)
        # Prune now-empty parent directories left behind by the rename so a
        # nested PII directory name does not linger as an empty shell.
        parent = abs_path.parent
        while parent != corpus_dir and not any(parent.iterdir()):
            empty = parent
            parent = parent.parent
            empty.rmdir()
        renames[doc_id] = new_doc_id
    return renames


# Doc-id-shaped name/email residue: used by verify_scrubbed to catch a doc-id
# that still carries PII even when the on-disk file bytes are clean (X6).
def residual_doc_id_pii(
    corpus_dir: str | os.PathLike[str], policy: ScrubPolicy
) -> list[str]:
    """Return doc-ids that still change under :func:`scrub_doc_id` (X6, §5.1).

    A non-empty result means at least one file's PATH (not just its text) still
    carries a real name/email the policy would redact — the independent,
    bytes-level defense for doc-ids, mirroring :func:`residual_secrets` for file
    content.
    """
    hits: list[str] = []
    for doc_id, _ in _iter_corpus_files(Path(corpus_dir)):
        _, changed = scrub_doc_id(doc_id, policy)
        if changed:
            hits.append(doc_id)
    return hits


def compute_scrub_manifest_hash(
    corpus_dir: str | os.PathLike[str], spans: list[RedactionSpan]
) -> str:
    """SHA-256 over the canonical sorted redacted-span list AND scrubbed tree.

    The hash binds BOTH (a) what was redacted (the spans) and (b) the resulting
    scrubbed bytes (the content-hash of the on-disk tree). Recomputing it over a
    DIFFERENT (e.g. unscrubbed) tree yields a different hash, so a bare
    ``scrubbed=True`` over unscrubbed bytes cannot match (§5.1).
    """
    hasher = hashlib.sha256()
    # (a) canonical sorted spans
    for line in sorted(s.canonical() for s in spans):
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")
    hasher.update(b"--tree--\n")
    # (b) the scrubbed file tree's content-hash
    hasher.update(compute_content_hash(corpus_dir).encode("utf-8"))
    return hasher.hexdigest()


def scrub_snapshot(
    snapshot_dir: str | os.PathLike[str],
    policy: ScrubPolicy,
    *,
    allow_public: bool = False,
) -> str:
    """Scrub a frozen snapshot IN PLACE, updating its manifest (§5.1).

    Rewrites each ``corpus/`` file with its scrubbed text, recomputes the
    ``content_hash`` over the now-scrubbed tree, computes the
    ``scrub_manifest_hash``, and rewrites ``manifest.json`` with
    ``scrubbed=True`` + both hashes. Persists SALTED hashes of the policy's
    ``name_aliases`` keys into the manifest (a leak-free "considered" signal) and
    the PLAINTEXT real names ONLY into the private, gitignored
    ``scrub_spans/_private`` sidecar, so :func:`verify_scrubbed` can reconstruct
    the name dimension of the denylist without exposing the operator name on a
    public path (item 3, §5.1). Returns the ``scrub_manifest_hash``.

    NOTE: this mutates the snapshot bytes; it is meant to run against a PRIVATE
    snapshot the freezer already produced. The PRIVATE-PATH WRITE GUARD is
    enforced here too (§5.1, task item 5): a caller passing a snapshot outside
    the private area without ``allow_public=True`` is refused BEFORE any byte is
    rewritten — not all callers route through :func:`freeze_snapshot` first.
    """
    snapshot_dir = Path(snapshot_dir)
    # PRIVATE-PATH WRITE GUARD — scrub_snapshot is an in-place writer.
    assert_private_path(snapshot_dir, allow_public=allow_public)
    corpus_dir = corpus_subdir(snapshot_dir)

    # DOC-ID SCRUB (X6, §5.1): rename any file whose corpus-relative path still
    # carries a real name/email BEFORE the text pass, so (a) the spans recorded
    # below are keyed by the ALREADY-scrubbed doc-id (never the PII-bearing
    # original) and (b) the rebuilt manifest/doc-id set downstream (gold labels
    # via label_cli's StubDrafter, which iterates corpus.doc_ids()) never sees
    # the original filename.
    rename_scrubbed_doc_ids(corpus_dir, policy)

    all_spans: list[RedactionSpan] = []
    for doc_id, abs_path in _iter_corpus_files(corpus_dir):
        text = abs_path.read_text(encoding="utf-8")
        scrubbed, spans = scrub_text(doc_id, text, policy)
        if scrubbed != text:
            abs_path.write_text(scrubbed, encoding="utf-8")
        all_spans.extend(spans)

    content_hash = compute_content_hash(corpus_dir)
    scrub_hash = compute_scrub_manifest_hash(corpus_dir, all_spans)

    # Rewrite the manifest with the post-scrub hashes + scrubbed=True.
    from .snapshot import build_manifest

    manifest = build_manifest(corpus_dir)
    manifest_path = snapshot_dir / MANIFEST_FILENAME
    payload = json.loads(manifest.to_json())
    payload["content_hash"] = content_hash
    payload["scrubbed"] = True
    payload["scrub_manifest_hash"] = scrub_hash
    # SECURITY (item 3, §5.1): NEVER write the plaintext real operator name(s)
    # into manifest.json — when allow_public=True the manifest can land on a
    # public path. The plaintext names needed to re-scan for residual name PII
    # are persisted ONLY in the private, gitignored scrub_spans/_private sidecar.
    # The manifest carries SALTED SHA-256 hashes of the names as a "names were
    # considered" signal that leaks no plaintext. verify_scrubbed reconstructs
    # the real names from the private sidecar (or an explicit policy).
    real_names = sorted(policy.name_aliases.keys())
    if real_names:
        salt = os.urandom(16).hex()
        payload["name_alias_key_hashes"] = sorted(
            _hash_name_key(n, salt) for n in real_names
        )
        # Private sidecar (gitignored) — the ONLY place plaintext names live.
        keys_path = _name_keys_path(snapshot_dir)
        keys_path.parent.mkdir(parents=True, exist_ok=True)
        keys_path.write_text(
            json.dumps({"salt": salt, "names": real_names}, sort_keys=True),
            encoding="utf-8",
        )
    else:
        payload["name_alias_key_hashes"] = []
    # Record whether name redaction was EXPLICITLY opted out (policy carried no
    # name_aliases) so verify_scrubbed can tell "verified, no names to redact"
    # from an unverified gap. A scrub that ran with an empty name map is an
    # explicit opt-out — but it is still only a *claim* the verifier surfaces;
    # it does NOT let name PII pass silently (verify_scrubbed refuses without an
    # explicit policy when opt-out is set).
    payload["name_scrub_opted_out"] = not policy.name_aliases
    manifest_path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Also persist the span list for operator review (provenance), alongside the
    # manifest. This file is NOT part of the content-hash (it sits at snapshot
    # root, outside corpus/).
    (snapshot_dir / "scrub_spans.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "doc_id": s.doc_id,
                    "kind": s.kind,
                    "start": s.start,
                    "end": s.end,
                    "replacement": s.replacement,
                },
                sort_keys=True,
            )
            + "\n"
            for s in sorted(all_spans, key=lambda s: s.canonical())
        ),
        encoding="utf-8",
    )
    return scrub_hash


class ScrubGateError(RuntimeError):
    """Raised when the scrub cross-check fails (§5.1)."""


def residual_secrets(
    corpus_dir: str | os.PathLike[str], policy: ScrubPolicy
) -> list[RedactionSpan]:
    """Re-run the denylist over a (claimed-scrubbed) tree; return any HITS.

    This is the independent bytes-level check the hash alone cannot give: an
    attacker who forges ``scrub_manifest_hash`` over UNSCRUBBED bytes (e.g. with
    an empty span list, which would otherwise recompute to a matching hash) is
    still caught here because the denylist re-scan finds the secrets the forgery
    never removed. A genuinely scrubbed tree yields ZERO residual hits.
    """
    found: list[RedactionSpan] = []
    for doc_id, abs_path in _iter_corpus_files(corpus_dir):
        text = abs_path.read_text(encoding="utf-8")
        _, spans = scrub_text(doc_id, text, policy)
        found.extend(spans)
    return found


def verify_scrubbed(
    snapshot_dir: str | os.PathLike[str],
    policy: ScrubPolicy | None = None,
) -> None:
    """Cross-check a snapshot claims-vs-bytes scrub gate (§5.1). RAISE on fail.

    Enforces, in order:
    1. ``scrubbed`` must be ``True`` and ``scrub_manifest_hash`` non-empty — a
       bare boolean is never sufficient.
    2. ``scrub_manifest_hash`` recomputes (over the persisted spans + on-disk
       tree) and matches; a tampered tree or span list breaks it.
    3. ``content_hash`` of the tree matches the manifest (no post-scrub byte
       drift).
    4. **Independent residual-secret re-scan** (the bytes-level defense): the
       denylist is re-run over the on-disk tree and MUST find ZERO redactable
       spans. This is what defeats a forged hash over unscrubbed bytes — the
       hash check can be gamed with an empty span list, but the re-scan cannot,
       because the secrets are still physically present in the bytes.

    The re-scan policy is reconstructed in this order so the NAME dimension is
    never silently dropped (the name-only-PII forgery, task items 1/5):
      - an explicit ``policy`` argument wins (its real ``name_aliases`` are used);
      - else the policy is rebuilt from the PRIVATE, gitignored
        ``scrub_spans/_private`` name sidecar's plaintext real names (the
        manifest holds only SALTED hashes, item 3), so a corpus whose ONLY PII is
        the operator's name is still re-scanned;
      - else, if the manifest records name hashes but the private sidecar is
        absent (cannot recover the plaintext names), the gate REFUSES — it has no
        real names to re-scan for;
      - otherwise (no policy, no name hashes) the gate REFUSES — whether the
        manifest records an explicit name-scrub opt-out or no opt-out at all. The
        presence of key/email scrub spans does NOT prove the name dimension was
        considered. Real-data runs MUST pass the real :class:`ScrubPolicy`
        explicitly; the no-policy path is only safe for corpora whose private
        name sidecar was recorded.
    """
    snapshot_dir = Path(snapshot_dir)
    manifest = load_manifest(snapshot_dir)
    if not manifest.scrubbed:
        raise ScrubGateError(
            "corpus does not claim scrubbed=True — refusing (real runs require "
            "a scrubbed, hash-bound corpus, §5.1)"
        )
    if not manifest.scrub_manifest_hash:
        raise ScrubGateError(
            "scrubbed=True but scrub_manifest_hash is empty — the boolean alone "
            "is not sufficient (§5.1)"
        )

    corpus_dir = corpus_subdir(snapshot_dir)
    spans = _load_spans(snapshot_dir)
    recomputed = compute_scrub_manifest_hash(corpus_dir, spans)
    if recomputed != manifest.scrub_manifest_hash:
        raise ScrubGateError(
            "scrub_manifest_hash MISMATCH — corpus claims scrubbed but bytes do "
            "not match the recorded scrub (a scrubbed=True flag over unscrubbed "
            "or tampered bytes is rejected, §5.1).\n"
            f"  claimed:    {manifest.scrub_manifest_hash}\n"
            f"  recomputed: {recomputed}"
        )
    # Defense in depth: also confirm the content-hash of the on-disk tree matches
    # the manifest's content_hash (the snapshot was not byte-altered post-scrub).
    actual_content = compute_content_hash(corpus_dir)
    if actual_content != manifest.content_hash:
        raise ScrubGateError(
            "content_hash mismatch on a scrubbed snapshot — bytes changed after "
            f"scrub.\n  manifest: {manifest.content_hash}\n  actual:   {actual_content}"
        )

    # Reconstruct the re-scan policy so the NAME dimension is never dropped.
    private_names = _load_private_name_keys(snapshot_dir)
    if policy is not None:
        rescan_policy = policy
    elif private_names:
        # Rebuild a name-aware policy from the PRIVATE sidecar's plaintext real
        # names (the manifest only holds salted hashes, item 3). The alias value
        # is irrelevant to detection (residual_secrets only needs to MATCH the
        # names, not produce a specific alias), so a sentinel alias suffices.
        rescan_policy = ScrubPolicy(
            name_aliases={k: DEFAULT_NAME_ALIAS for k in private_names}
        )
    elif manifest.name_alias_key_hashes:
        # The manifest records that names WERE considered (salted hashes present)
        # but the private sidecar holding the plaintext is absent — so we cannot
        # re-scan for the actual name bytes. Refuse rather than rubber-stamp; a
        # name-only residual would be invisible without the real names. Pass the
        # real ScrubPolicy explicitly for real-data runs.
        raise ScrubGateError(
            "cannot verify scrub: the manifest records name hashes "
            "(name_alias_key_hashes) but the private scrub_spans/_private name "
            "sidecar holding the plaintext names is missing — the verifier has "
            "no real names to re-scan for. Pass the real ScrubPolicy explicitly "
            "to verify_scrubbed() (§5.1)."
        )
    elif manifest.name_scrub_opted_out:
        # The scrub EXPLICITLY recorded that it opted out of name redaction (the
        # operator ran scrub_snapshot with an empty name_aliases on purpose). We
        # still cannot independently re-scan for name PII without the real names,
        # so without an explicit policy this is an unverifiable claim, not proof.
        # Refuse rather than rubber-stamp: a corpus whose ONLY PII is the
        # operator's name (never scrubbed) must not pass on a bare opt-out flag.
        raise ScrubGateError(
            "cannot verify scrub: the manifest records name redaction was "
            "OPTED OUT (name_scrub_opted_out=True) and no explicit policy was "
            "passed — a name-only PII forgery would be invisible because the "
            "verifier has no real names to re-scan for. Pass the real "
            "ScrubPolicy explicitly to verify_scrubbed() for real-data runs "
            "(§5.1)."
        )
    else:
        # No explicit policy, no private name sidecar, no name_alias_key_hashes,
        # AND no explicit opt-out. The
        # name dimension was never recorded as considered at all — refuse
        # unconditionally (regardless of whether key/email spans exist), because
        # the presence of unrelated spans does NOT prove names were scrubbed.
        # This closes the hole where scrub_spans.jsonl holds key/email spans but
        # ScrubPolicy(name_aliases={}) let real-name bytes through.
        raise ScrubGateError(
            "cannot verify scrub: no policy passed, the manifest persisted no "
            "name_alias_key_hashes, and name redaction was not explicitly opted "
            "out — the name dimension was never proven to be considered. Pass the "
            "real ScrubPolicy explicitly to verify_scrubbed() (§5.1)."
        )
    residual = residual_secrets(corpus_dir, rescan_policy)
    if residual:
        kinds = sorted({s.kind for s in residual})
        raise ScrubGateError(
            "corpus claims scrubbed=True but the denylist re-scan still finds "
            f"{len(residual)} redactable span(s) of kind(s) {kinds} — the bytes "
            "are NOT actually scrubbed (forged flag/hash over unscrubbed bytes "
            "is rejected, §5.1)."
        )

    # DOC-ID residual re-scan (X6, §5.1): the checks above only re-scan file
    # BYTES. A scrubber that redacted every file's TEXT but left a PII-bearing
    # FILENAME untouched (or a tampered/forged snapshot whose files were renamed
    # back) would otherwise pass with the real name/email still present as the
    # doc-id — which flows straight into manifest.json, scrub_spans.jsonl (via
    # doc_id-keyed spans), and gold labels (StubDrafter ids/gold_doc_ids/gold_fact
    # all embed the raw doc_id). Independently re-derive the doc-id set and
    # reject if any doc-id still changes under the denylist.
    doc_id_residual = residual_doc_id_pii(corpus_dir, rescan_policy)
    if doc_id_residual:
        raise ScrubGateError(
            "corpus claims scrubbed=True but the doc-id re-scan still finds "
            f"{len(doc_id_residual)} PII-bearing doc-id(s) (name/email in the "
            "filename/path survives despite scrubbed file text) — the doc-ids "
            "are NOT actually scrubbed (X6, §5.1). Offending doc-id(s) (first 5): "
            f"{doc_id_residual[:5]}"
        )


def _load_private_name_keys(snapshot_dir: Path) -> list[str]:
    """Load the plaintext real-name keys from the PRIVATE sidecar (item 3).

    Returns the real names recorded by :func:`scrub_snapshot` so the verifier can
    re-scan for residual name PII. These live ONLY under scrub_spans/_private/
    (gitignored), never in manifest.json. Returns ``[]`` if the sidecar is
    absent (e.g. a name-less scrub or a forged/incomplete snapshot).
    """
    keys_path = _name_keys_path(snapshot_dir)
    if not keys_path.exists():
        return []
    size = keys_path.stat().st_size
    if size > MAX_SCRUB_SPANS_FILE_BYTES:
        raise ScrubGateError(
            f"name_keys.json is {size} bytes, over the "
            f"{MAX_SCRUB_SPANS_FILE_BYTES}-byte cap (refusing to load)"
        )
    try:
        d = json.loads(keys_path.read_text(encoding="utf-8"))
        names = d["names"]
    except (KeyError, ValueError):
        return []
    # ``names`` is attacker-controllable (the sidecar is edit-controlled). A
    # non-list value (null / str / dict) must yield [] — NEVER fall through to
    # the comprehension, which would (a) raise an UNCAUGHT TypeError on null
    # (escaping the scrub gate as a raw crash) and (b) on a bare string iterate
    # PER CHARACTER, producing single-letter \b name patterns that false-match
    # the whole corpus. The isinstance guard closes both holes at once.
    if not isinstance(names, list):
        return []
    return [str(n) for n in names]


def _load_spans(snapshot_dir: Path) -> list[RedactionSpan]:
    spans_path = snapshot_dir / "scrub_spans.jsonl"
    if not spans_path.exists():
        return []
    size = spans_path.stat().st_size
    if size > MAX_SCRUB_SPANS_FILE_BYTES:
        raise ScrubGateError(
            f"scrub_spans.jsonl is {size} bytes, over the "
            f"{MAX_SCRUB_SPANS_FILE_BYTES}-byte cap (refusing to load)"
        )
    out: list[RedactionSpan] = []
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # scrub_spans.jsonl is edit-controlled; EVERY way of parsing a span line
        # must surface as a scrub-gate failure (so the gate reports a clean
        # refusal that corpus._enforce_scrub_gate catches), not a bare crash
        # before the hash gate runs (item 4). Three failure modes are caught here
        # together: (a) malformed JSON (json.JSONDecodeError) — so json.loads is
        # INSIDE the try; (b) a missing field (KeyError); (c) a valid-but-non-dict
        # line such as a JSON array, where d["doc_id"] raises TypeError. Redact
        # context: report ONLY the field/kind, never the line bytes (which may
        # carry residual PII).
        try:
            d = json.loads(line)
            span = RedactionSpan(
                doc_id=d["doc_id"],
                kind=d["kind"],
                start=d["start"],
                end=d["end"],
                replacement=d["replacement"],
            )
        except json.JSONDecodeError:
            raise ScrubGateError(
                "malformed scrub span: line is not valid JSON"
            ) from None
        except KeyError as exc:
            raise ScrubGateError(
                f"malformed scrub span: missing {exc.args[0]!r} field"
            ) from None
        except TypeError:
            # A valid JSON value that is not a dict (e.g. an array or scalar):
            # d["doc_id"] raises TypeError. Report the shape, never the bytes.
            raise ScrubGateError(
                "malformed scrub span: line is not a JSON object"
            ) from None
        # TYPE validation (item 3): the span fields are annotated but the JSONL
        # is attacker-controlled, so the annotations are not enforced at load.
        # A non-int start/end would later corrupt offset arithmetic; non-str
        # doc_id/kind/replacement would poison the re-scan. Reject as a clean
        # scrub-gate failure. ``bool`` is an int subclass — reject it explicitly
        # so a JSON ``true`` cannot masquerade as an offset. Report ONLY the
        # field name, never the value (which may carry residual PII).
        if isinstance(span.start, bool) or not isinstance(span.start, int):
            raise ScrubGateError("malformed scrub span: start/end must be int")
        if isinstance(span.end, bool) or not isinstance(span.end, int):
            raise ScrubGateError("malformed scrub span: start/end must be int")
        for fname in ("doc_id", "kind", "replacement"):
            if not isinstance(getattr(span, fname), str):
                raise ScrubGateError(
                    f"malformed scrub span: {fname} must be str"
                )
        out.append(span)
    return out
