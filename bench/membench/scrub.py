"""Scrub gate — secret/PII redaction + cryptographic scrub binding (§5.1).

Slice s2(a). A redaction pass over snapshot text removes/aliases secrets and PII
per a configurable denylist:

- **API-key / token patterns** — ``sk-...`` and common bearer/token shapes.
- **Email addresses** — replaced with a fixed alias.
- **Real-name -> alias map** — by default maps the operator's real name to the
  ``Infektyd`` alias.

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
DEFAULT_NAME_ALIAS = "Infektyd"

# API keys / tokens: sk-... (OpenAI-shape), generic long base62 token after a
# token-ish prefix, and bearer tokens. Ordered longest-first conceptually; the
# engine applies them in sequence.
_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # sk-... and sk-proj-... style (>=16 trailing key chars)
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
    if policy.redact_emails:
        for m in _EMAIL_PATTERN.finditer(text):
            matches.append((m.start(), m.end(), "email", REDACTED_EMAIL))
    for pat, alias in _name_patterns(policy):
        for m in pat.finditer(text):
            matches.append((m.start(), m.end(), "name", alias))

    # Resolve overlaps: sort by start, then by widest span first; greedily keep
    # non-overlapping matches (a key match subsuming an email inside it wins).
    matches.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    chosen: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, kind, repl in matches:
        if start < last_end:
            continue  # overlaps an already-chosen span; skip
        chosen.append((start, end, kind, repl))
        last_end = end

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
        d = json.loads(line)
        try:
            span = RedactionSpan(
                doc_id=d["doc_id"],
                kind=d["kind"],
                start=d["start"],
                end=d["end"],
                replacement=d["replacement"],
            )
        except KeyError as exc:
            # scrub_spans.jsonl is edit-controlled; a missing field must surface
            # as a scrub-gate failure (so the gate reports a clean refusal), not
            # a bare KeyError crash before the hash gate runs (item 4). Redact
            # context: report ONLY the missing field name, never the line bytes
            # (which may carry residual PII).
            raise ScrubGateError(
                f"malformed scrub span: missing {exc.args[0]!r} field"
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
