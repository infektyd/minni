"""Bounded retrieval-study snapshot foundation for private-memory studies.

The study target is Hans's day-to-day cross-project memories. Nothing in
this module collects, exports, or reads live memories: the ONLY input is an
explicit, bounded ``authorized-export packet`` supplied by the parent, which
connects the governed export separately. The packet carries its own
principal/store/source identity plus record content; arbitrary filesystem
paths and vault dumps are never accepted.

Provenance honesty rules enforced here:

- Claimed authorization in the packet is a *supplied claim*, never
  authentication proof and never independently verified permission.
- Every study judgment is ``machine_proposed`` with ``human_reviewed``
  exactly ``False``. Original lifecycle/privacy/review provenance is
  preserved separately and never merged into the study judgment; machine
  judgments are never labeled human-reviewed.
- ``sm_export_pack`` is a shared-snippet, export-cap-gated RPC, not a corpus
  snapshot source; this module never calls it and bypasses no capability.
- A snapshot ID derives from the full packet digest only. Snapshot IDs are
  never assigned to the live corpus.
- Reports state scope honestly: a bounded packet study, not representative
  private-memory quality and not a retrieval-performance claim.

Isolation rules:

- :func:`prepare_snapshot` writes vault files, an opaque remapping, and a
  manifest under a private (0700 dirs / 0600 files) destination. It imports
  no database, engine, or model code and therefore cannot instantiate the
  live ``DEFAULT_CONFIG`` database or load a model. Destinations that are,
  contain, or sit inside live/default paths are rejected before anything is
  written.
- :func:`materialize_snapshot_db` re-validates the frozen files and metadata
  first, then builds the disposable SQLite/FTS corpus the way
  ``eval/fixture.py`` does (lexical rows, fixed study principal), with EVERY
  database/index/vault path inside the snapshot directory. It refuses to run
  twice into one directory so stale records can never mix with new outputs.
- ``SnapshotSearcher`` (in ``eval/retrievers.py``) re-validates frozen files
  and metadata on open and before every search, opens only the prepared
  snapshot directory, and never the live ``DEFAULT_CONFIG``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote as _url_quote

PACKET_VERSION = "minni-study-export-v1"
SNAPSHOT_VERSION = "minni-study-snapshot-v1"

MACHINE_PROPOSED = "machine_proposed"
CONTENT_KINDS = {"original", "excerpt"}
PRIVACY_LEVELS = {"safe", "local-only", "private", "blocked"}

# Hard input bounds, enforced before any hashing, writes, or DB work, so a
# hostile or malformed packet cannot turn preparation into expensive work.
MAX_RECORDS = 1000
MAX_TEXT_CHARS = 100_000
MAX_TEXT_BYTES = 400_000  # worst-case UTF-8 expansion of MAX_TEXT_CHARS
MAX_TOTAL_TEXT_CHARS = 5_000_000
MAX_SOURCE_DETAIL_KEYS = 16
MAX_SOURCE_DETAIL_KEY_CHARS = 64
MAX_SOURCE_DETAIL_STRING_CHARS = 4_096
MAX_CAPABILITIES = 32
MAX_CAPABILITY_CHARS = 64
MAX_AGENT_ID_CHARS = 128
MAX_STORE_ID_CHARS = 128
MAX_ORIGIN_CHARS = 512
MAX_CLAIMED_CHARS = 512
MAX_SOURCE_DOC_ID_CHARS = 256
MAX_AGENT_CHARS = 128
MAX_PRIVACY_CHARS = 64
MAX_LIFECYCLE_CHARS = 64
MAX_LOCATOR_CHARS = 512
MAX_ARTIFACT_PATH_CHARS = 512
MAX_TOTAL_ARTIFACT_PATH_CHARS = 256_000
# Frozen-file read caps: no unbounded read happens before a size preflight.
MAX_VAULT_FILE_BYTES = MAX_TEXT_BYTES + 1_024
MAX_METADATA_BYTES = 4_000_000

# Records are addressed in the study only through these opaque IDs. The
# original store identities stay in mapping.json for audit, never as
# retrieval addresses.
STUDY_ID_PREFIX = "study-"
CONTENT_GROUP_PREFIX = "cg-"

_SCOPE_NOTE = (
    "Bounded authorized-export packet study over day-to-day cross-project "
    "memories; not representative private-memory quality, not a retrieval "
    "performance claim, and not a default-change signal."
)

_AUTHORIZATION_NOTE = (
    "Supplied packet claim only; authorization stated by the packet is "
    "recorded as supplied provenance, not authentication proof and not "
    "independently verified permission."
)


class StudySnapshotError(ValueError):
    """Malformed packet, integrity failure, or unsafe snapshot destination."""


def _require_dict(node: Any, label: str) -> Dict[str, Any]:
    if not isinstance(node, dict):
        raise StudySnapshotError(f"{label} must be an object")
    return node


def _require_nonempty_str(node: Any, label: str) -> str:
    if not isinstance(node, str) or not node.strip():
        raise StudySnapshotError(f"{label} must be a non-empty string")
    return node


def _require_bounded_str(node: Any, label: str, max_chars: int) -> str:
    value = _require_nonempty_str(node, label)
    if len(value) > max_chars:
        raise StudySnapshotError(f"{label} exceeds {max_chars} chars")
    return value


def _check_artifact_path(raw: Any) -> str:
    """Relative markdown study paths only; canonical segments, no aliases.

    Segments are checked raw so ``a/./n.md``, ``a//n.md``, absolute paths,
    and traversals are all rejected instead of silently normalizing to the
    same vault file under different names.
    """
    value = _require_nonempty_str(raw, "record artifact_path")
    if len(value) > MAX_ARTIFACT_PATH_CHARS:
        raise StudySnapshotError(
            f"record artifact_path exceeds {MAX_ARTIFACT_PATH_CHARS} chars"
        )
    if "\\" in value:
        raise StudySnapshotError("record artifact_path must use forward slashes")
    segments = value.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise StudySnapshotError(
            "record artifact_path must be a canonical relative path without "
            "empty, dot, or traversal segments"
        )
    if not segments[-1].endswith(".md"):
        raise StudySnapshotError("record artifact_path must name a markdown file")
    return value


def _check_source_detail(raw: Any, label: str) -> Dict[str, Any] | None:
    """Optional original-provenance detail: small, finite, scalar-only, verbatim."""
    if raw is None:
        return None
    detail = _require_dict(raw, f"{label} source_detail")
    if len(detail) > MAX_SOURCE_DETAIL_KEYS:
        raise StudySnapshotError(
            f"{label}: source_detail holds at most {MAX_SOURCE_DETAIL_KEYS} keys"
        )
    for key, value in detail.items():
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_SOURCE_DETAIL_KEY_CHARS:
            raise StudySnapshotError(f"{label}: source_detail keys must be short strings")
        if isinstance(value, str):
            if len(value) > MAX_SOURCE_DETAIL_STRING_CHARS:
                raise StudySnapshotError(
                    f"{label}: source_detail strings hold at most "
                    f"{MAX_SOURCE_DETAIL_STRING_CHARS} chars"
                )
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise StudySnapshotError(
                    f"{label}: source_detail floats must be finite (original "
                    "provenance is preserved, never executed)"
                )
        elif not isinstance(value, (int, bool)) and value is not None:
            raise StudySnapshotError(
                f"{label}: source_detail values must be finite scalars (original "
                "provenance is preserved, never executed)"
            )
    return dict(detail)


def _reject_json_constant(value: str) -> Any:
    raise StudySnapshotError(f"snapshot JSON must be strict (nonfinite constant {value!r})")


def _no_dup_object(pairs: list) -> Dict[str, Any]:
    """Reject duplicate object keys so frozen evidence is unambiguous."""
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StudySnapshotError(f"snapshot JSON has duplicate key {key!r}")
        obj[key] = value
    return obj


def _parse_strict_json(text: str, label: str) -> Any:
    """Strict JSON: no NaN/Infinity constants, no duplicate object keys."""
    try:
        return json.loads(text, parse_constant=_reject_json_constant,
                          object_pairs_hook=_no_dup_object)
    except StudySnapshotError:
        raise
    except ValueError as exc:
        raise StudySnapshotError(f"{label} is not valid JSON") from exc


def _assert_no_symlink_components(path: Path, root: Path, label: str) -> None:
    """Reject symlinks in ANY component, not just the leaf file.

    A symlinked ancestor directory (e.g. ``vault/project-a`` pointing outside
    the snapshot) makes leaf checks meaningless, so every component from the
    snapshot root down is lstat-checked without following links.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StudySnapshotError(f"{label} {path} escapes the snapshot directory") from exc
    current = root
    if os.path.islink(current):
        raise StudySnapshotError(f"{label} snapshot root {root} must not be a symlink")
    for segment in relative.parts:
        current = current / segment
        if os.path.islink(current):
            raise StudySnapshotError(
                f"{label} component {current} must not be a symlink"
            )


def canonical_identity(packet: Any) -> Dict[str, Any]:
    """Canonical source/principal/authorization identity bound into the digest."""
    top = _require_dict(packet, "study packet")
    if top.get("packet_version") != PACKET_VERSION:
        raise StudySnapshotError(
            f"study packet packet_version must be {PACKET_VERSION!r}"
        )
    principal = _require_dict(top.get("principal"), "packet principal")
    store = _require_dict(top.get("store"), "packet store")
    source = _require_dict(top.get("source"), "packet source")
    authorization = _require_dict(top.get("authorization"), "packet authorization")
    capabilities = principal.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, list) or len(capabilities) > MAX_CAPABILITIES:
            raise StudySnapshotError(
                f"packet principal capabilities hold at most {MAX_CAPABILITIES} entries"
            )
        for item in capabilities:
            _require_bounded_str(item, "packet principal capability", MAX_CAPABILITY_CHARS)
    return {
        "packet_version": PACKET_VERSION,
        "principal": {
            "agent_id": _require_bounded_str(
                principal.get("agent_id"), "packet principal agent_id", MAX_AGENT_ID_CHARS),
            "capabilities": capabilities,
        },
        "store": {
            "store_id": _require_bounded_str(
                store.get("store_id"), "packet store store_id", MAX_STORE_ID_CHARS),
            "origin": _require_bounded_str(
                store.get("origin"), "packet store origin", MAX_ORIGIN_CHARS),
        },
        "source": {
            "origin": _require_bounded_str(
                source.get("origin"), "packet source origin", MAX_ORIGIN_CHARS),
        },
        "authorization": {
            "claimed": _require_bounded_str(
                authorization.get("claimed"), "packet authorization claimed", MAX_CLAIMED_CHARS),
        },
    }


def _canonical_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Full canonical record: every digest-bound field, including lifecycle."""
    return {
        "agent": record["agent"],
        "artifact_path": record["artifact_path"],
        "content_kind": record["content_kind"],
        "content_sha256": record["content_sha256"].lower(),
        "expected_eligible": record["expected_eligible"],
        "human_reviewed": record["human_reviewed"],
        "origin": record["origin"],
        "page_status": record.get("page_status", "active"),
        "page_type": record.get("page_type", "note"),
        "privacy_level": record["privacy_level"],
        "review_state": record["review_state"],
        "source_detail": record.get("source_detail"),
        "source_doc_id": record["source_doc_id"],
        "source_locator": record.get("source_locator"),
        "store": record["store"],
        "text": record["text"],
    }


def manifest_digest_for(records: List[Dict[str, Any]], identity: Dict[str, Any]) -> str:
    """Deterministic digest over canonical identity AND canonical records.

    Source/principal/authorization metadata and lifecycle fields are bound
    into the snapshot identity; swapping them invalidates the manifest.
    """
    canonical = {
        "identity": identity,
        "records": sorted(
            (_canonical_record(record) for record in records),
            key=lambda row: (row["store"], row["source_doc_id"]),
        ),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_id_for(manifest_digest: str) -> str:
    """Opaque study ID derived from packet content only; never a live-corpus ID."""
    return f"{STUDY_ID_PREFIX}{manifest_digest[:16]}"


def content_group_for(content_sha256: str) -> str:
    """Relationship label shared by every record carrying identical bytes."""
    return f"{CONTENT_GROUP_PREFIX}{content_sha256.lower()}"


_RECORD_KEYS = frozenset({
    "agent", "artifact_path", "content_kind", "content_sha256", "expected_eligible",
    "human_reviewed", "origin", "page_status", "page_type", "privacy_level",
    "review_state", "source_detail", "source_doc_id", "source_locator",
    "store", "text",
})


def validate_export_packet(packet: Any) -> List[Dict[str, Any]]:
    """Validate a bounded authorized-export packet; return normalized records.

    Enforces hard count/text/total-byte limits before expensive work, then
    raises :class:`StudySnapshotError` on malformed data, manifest tampering,
    duplicate ``(store, source_doc_id)`` identity, unsafe artifact paths,
    missing excerpt/original labels, or any human-reviewed claim. Identical
    bytes under separate ownership are allowed and linked through a shared
    content group instead of being conflated.
    """
    top = _require_dict(packet, "study packet")
    identity = canonical_identity(top)

    records = top.get("records")
    if not isinstance(records, list) or not records:
        raise StudySnapshotError("packet records must be a non-empty list")
    if len(records) > MAX_RECORDS:
        raise StudySnapshotError(
            f"packet holds {len(records)} records; at most {MAX_RECORDS} are accepted"
        )

    manifest = _require_dict(top.get("manifest"), "packet manifest")
    expected_digest = _require_nonempty_str(
        manifest.get("manifest_digest"), "packet manifest manifest_digest"
    )

    normalized: List[Dict[str, Any]] = []
    total_chars = 0
    total_artifact_path_chars = 0
    for index, raw in enumerate(records):
        label = f"record[{index}]"
        row = _require_dict(raw, label)
        unknown = set(row) - _RECORD_KEYS
        if unknown:
            raise StudySnapshotError(
                f"{label}: unknown fields {sorted(unknown)}; packets carry "
                "only digest-bound fields"
            )
        source_doc_id = _require_bounded_str(
            row.get("source_doc_id"), f"{label} source_doc_id", MAX_SOURCE_DOC_ID_CHARS)
        store_id = _require_bounded_str(
            row.get("store"), f"{label} store", MAX_STORE_ID_CHARS)
        artifact_path = _check_artifact_path(row.get("artifact_path"))
        total_artifact_path_chars += len(artifact_path)
        if total_artifact_path_chars > MAX_TOTAL_ARTIFACT_PATH_CHARS:
            raise StudySnapshotError(
                f"packet artifact paths exceed {MAX_TOTAL_ARTIFACT_PATH_CHARS} chars in total"
            )
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise StudySnapshotError(f"{label} text must be a non-empty string")
        if len(text) > MAX_TEXT_CHARS:
            raise StudySnapshotError(
                f"{label}: text holds {len(text)} chars; at most {MAX_TEXT_CHARS} are accepted"
            )
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise StudySnapshotError(
                f"{label}: text exceeds {MAX_TEXT_BYTES} UTF-8 bytes"
            )
        total_chars += len(text)
        if total_chars > MAX_TOTAL_TEXT_CHARS:
            raise StudySnapshotError(
                f"packet text exceeds {MAX_TOTAL_TEXT_CHARS} chars in total"
            )

        content_sha256 = _require_nonempty_str(row.get("content_sha256"), f"{label} content_sha256")
        actual_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, content_sha256.lower()):
            raise StudySnapshotError(f"{label}: content_sha256 does not match record text")

        content_kind = row.get("content_kind")
        if content_kind not in CONTENT_KINDS:
            raise StudySnapshotError(
                f"{label}: content_kind must be one of {sorted(CONTENT_KINDS)} (excerpt vs original)"
            )
        source_locator = row.get("source_locator")
        if content_kind == "excerpt":
            _require_bounded_str(
                source_locator, f"{label} source_locator (excerpts must cite their origin)",
                MAX_LOCATOR_CHARS,
            )
        elif source_locator is not None and (
            not isinstance(source_locator, str) or not source_locator.strip()
        ):
            raise StudySnapshotError(f"{label}: source_locator must be a non-empty string when present")

        # Machine judgments stay machine judgments: any human-reviewed claim
        # is a hard rejection, never a relabel.
        if row.get("review_state") != MACHINE_PROPOSED or row.get("human_reviewed") is not False:
            raise StudySnapshotError(
                f"{label}: review_state must be {MACHINE_PROPOSED!r} with "
                "human_reviewed=false; machine judgments are never human-reviewed"
            )

        agent = _require_bounded_str(
            row.get("agent"), f"{label} agent (source ownership)", MAX_AGENT_CHARS)
        privacy_level = _require_bounded_str(
            row.get("privacy_level"), f"{label} privacy_level (privacy metadata)",
            MAX_PRIVACY_CHARS,
        )
        if privacy_level not in PRIVACY_LEVELS:
            raise StudySnapshotError(
                f"{label}: privacy_level must be one of {sorted(PRIVACY_LEVELS)}"
            )
        origin = _require_bounded_str(
            row.get("origin"), f"{label} origin", MAX_ORIGIN_CHARS)
        if type(row.get("expected_eligible")) is not bool:
            raise StudySnapshotError(
                f"{label}: expected_eligible must be an explicit boolean "
                "(cross-project eligibility is annotated, never inferred)"
            )
        page_status = _require_bounded_str(
            row.get("page_status", "active"), f"{label} page_status", MAX_LIFECYCLE_CHARS)
        if page_status not in {"active", "draft", "candidate", "accepted", "superseded",
                               "rejected", "expired", "complete"}:
            raise StudySnapshotError(f"{label}: page_status is not a recognized lifecycle status")
        page_type = _require_bounded_str(
            row.get("page_type", "note"), f"{label} page_type", MAX_LIFECYCLE_CHARS)
        if source_locator is not None and len(source_locator) > MAX_LOCATOR_CHARS:
            raise StudySnapshotError(
                f"{label}: source_locator exceeds {MAX_LOCATOR_CHARS} chars")
        source_detail = _check_source_detail(row.get("source_detail"), label)
        normalized.append({
            "agent": agent,
            "artifact_path": artifact_path,
            "content_kind": content_kind,
            "content_sha256": actual_digest,
            "expected_eligible": row["expected_eligible"],
            "human_reviewed": False,
            "origin": origin,
            "page_status": page_status,
            "page_type": page_type,
            "privacy_level": privacy_level,
            "review_state": MACHINE_PROPOSED,
            "source_detail": source_detail,
            "source_doc_id": source_doc_id,
            "source_locator": source_locator if isinstance(source_locator, str) else None,
            "store": store_id,
            "text": text,
        })

    # Source identity is the (store, source_doc_id) tuple: the same document
    # number in two stores names two different documents. Artifact paths must
    # still be unique so frozen vault files never collide.
    seen_identities = set()
    seen_artifacts = set()
    for row in normalized:
        identity_key = (row["store"], row["source_doc_id"])
        if identity_key in seen_identities:
            raise StudySnapshotError(
                f"duplicate source identity {identity_key!r} in study packet"
            )
        seen_identities.add(identity_key)
        if row["artifact_path"] in seen_artifacts:
            raise StudySnapshotError(
                f"duplicate artifact path {row['artifact_path']!r} in study packet"
            )
        seen_artifacts.add(row["artifact_path"])

    actual = manifest_digest_for(normalized, identity)
    if not hmac.compare_digest(actual, expected_digest.lower()):
        raise StudySnapshotError("packet manifest_digest does not match record content (tamper?)")
    return normalized


def _live_path_set() -> set:
    """Live/default paths a snapshot must never target (lazy, import-free otherwise)."""
    try:
        from minni.config import DEFAULT_CONFIG
    except Exception as exc:  # noqa: BLE001 - destination safety cannot be proved
        raise StudySnapshotError(
            "cannot verify live/default paths because minni config is unavailable"
        ) from exc
    paths = set()
    for attr in ("db_path", "vault_path", "faiss_index_path", "graph_export_dir",
                 "writeback_path"):
        value = getattr(DEFAULT_CONFIG, attr, None)
        if isinstance(value, str) and value.strip():
            paths.add(value)
    return paths


def _reject_live_destination(dest: Path) -> Path:
    """Refuse destinations that are, contain, or sit inside live/default paths."""
    resolved = Path(dest).resolve()
    for raw in _live_path_set():
        try:
            live = Path(raw).resolve()
        except OSError:
            continue
        if resolved == live or live in resolved.parents or resolved in live.parents:
            raise StudySnapshotError(
                f"snapshot destination {dest} must not target live/default path {raw}"
            )
    return resolved


def _ensure_private_dir(path: Path) -> None:
    """Create or verify a user-owned 0700 directory (mirrors harness handling)."""
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise StudySnapshotError(f"snapshot destination {path} must be a real directory")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise StudySnapshotError(
                    f"snapshot destination {path} must be owned by this user and private (0700)"
                )
        finally:
            os.close(fd)
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private_file(path: Path, text: str) -> None:
    """Write 0600 bytes without following symlinks (mirrors harness handling)."""
    directory = path.parent
    _ensure_private_dir(directory)
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".minni-study-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise StudySnapshotError(f"snapshot directory {directory} must not be writable by others")
        try:
            previous = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
                raise StudySnapshotError(
                    f"snapshot destination {path} must be a regular, unlinked file"
                )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dir_fd)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temporary, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        os.close(dir_fd)


def _read_sized_bytes(path: Path, root: Path, label: str, max_bytes: int) -> bytes:
    """Read a frozen file after symlink-component and size preflights.

    No unbounded read happens: the size cap is enforced from lstat before
    any bytes are loaded, and re-checked after decoding.
    """
    _assert_no_symlink_components(path, root, label)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise StudySnapshotError(f"{label} {path} is unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise StudySnapshotError(f"{label} {path} must be a regular file")
    size = info.st_size
    if size > max_bytes:
        raise StudySnapshotError(
            f"{label} {path} holds {size} bytes; at most {max_bytes} are accepted"
        )
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise StudySnapshotError(f"{label} {path} is unreadable") from exc
    if len(raw) > max_bytes:
        raise StudySnapshotError(f"{label} {path} exceeds {max_bytes} bytes")
    return raw


def _read_sized_text(path: Path, root: Path, label: str, max_bytes: int) -> str:
    """Frozen UTF-8 text read over the sized-bytes preflight."""
    raw = _read_sized_bytes(path, root, label, max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StudySnapshotError(f"{label} {path} must be UTF-8 text") from exc


def _read_json_strict(path: Path, root: Path, label: str) -> Any:
    """Strict-size, strict-JSON metadata read for snapshot artifacts."""
    return _parse_strict_json(
        _read_sized_text(path, root, label, MAX_METADATA_BYTES), label)


def deterministic_remapping(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Stable opaque study-ID remapping, sorted by (store, source_doc_id).

    Original lifecycle/privacy/review provenance is preserved separately
    from the study machine judgment in every entry; identical bytes under
    separate ownership share a content group instead of being conflated.
    """
    ordered = sorted(records, key=lambda row: (row["store"], row["source_doc_id"]))
    mapping: Dict[str, Dict[str, Any]] = {}
    for position, row in enumerate(ordered, start=1):
        study_id = f"{STUDY_ID_PREFIX}{position:04d}"
        mapping[study_id] = {
            "store": row["store"],
            "source_doc_id": row["source_doc_id"],
            "artifact_path": row["artifact_path"],
            "content_sha256": row["content_sha256"],
            "content_kind": row["content_kind"],
            "content_group": content_group_for(row["content_sha256"]),
            "source_provenance": {
                "agent": row["agent"],
                "privacy_level": row["privacy_level"],
                "origin": row["origin"],
                "page_status": row["page_status"],
                "page_type": row["page_type"],
                "content_kind": row["content_kind"],
                "source_locator": row["source_locator"],
                "source_detail": row["source_detail"],
            },
            "study_judgment": {
                "review_state": row["review_state"],
                "human_reviewed": False,
                "expected_eligible": row["expected_eligible"],
            },
            # Flat mirrors for the disposable DB builder; the nested blocks
            # above stay authoritative for provenance.
            "agent": row["agent"],
            "privacy_level": row["privacy_level"],
            "origin": row["origin"],
            "page_status": row["page_status"],
            "page_type": row["page_type"],
            "review_state": row["review_state"],
            "human_reviewed": False,
            "expected_eligible": row["expected_eligible"],
            "source_locator": row["source_locator"],
            "source_detail": row["source_detail"],
        }
    return mapping


def content_groups_for(mapping: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Multi-member content groups: distinct identities sharing identical bytes."""
    groups: Dict[str, List[str]] = {}
    for study_id, row in mapping.items():
        groups.setdefault(row["content_group"], []).append(study_id)
    return {group: sorted(members) for group, members in groups.items() if len(members) > 1}


def _records_from_mapping(mapping: Dict[str, Dict[str, Any]],
                          vault_root: Path, root: Path) -> List[Dict[str, Any]]:
    """Reconstruct canonical records from frozen mapping + vault bytes."""
    records = []
    for study_id in sorted(mapping):
        row = mapping[study_id]
        provenance = row.get("source_provenance") or {}
        judgment = row.get("study_judgment") or {}
        target = vault_root / row["artifact_path"]
        text = _read_sized_text(target, root, f"snapshot vault file {row['artifact_path']!r}",
                                MAX_VAULT_FILE_BYTES)
        records.append({
            "agent": provenance.get("agent", row.get("agent")),
            "artifact_path": row["artifact_path"],
            "content_kind": provenance.get("content_kind", row.get("content_kind")),
            "content_sha256": row["content_sha256"],
            "expected_eligible": judgment.get("expected_eligible", row.get("expected_eligible")),
            "human_reviewed": False,
            "origin": provenance.get("origin", row.get("origin")),
            "page_status": provenance.get("page_status", row.get("page_status")),
            "page_type": provenance.get("page_type", row.get("page_type")),
            "privacy_level": provenance.get("privacy_level", row.get("privacy_level")),
            "review_state": MACHINE_PROPOSED,
            "source_detail": provenance.get("source_detail", row.get("source_detail")),
            "source_doc_id": row["source_doc_id"],
            "source_locator": provenance.get("source_locator", row.get("source_locator")),
            "store": row["store"],
            "text": text,
        })
    return records


def _check_manifest_identity(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the bound identity block and every consumed display mirror.

    The manifest carries the digest-bound ``identity`` plus convenience
    mirrors (``principal``/``store``/``source``/``authorization_claimed``/
    ``snapshot_id``). Every mirror a consumer could read MUST equal the
    bound block; an invented snapshot ID or an edited principal is rejected
    here, not just by the digest recomputation.
    """
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise StudySnapshotError("snapshot manifest must carry the bound identity")
    for section in ("principal", "store", "source", "authorization"):
        if not isinstance(identity.get(section), dict):
            raise StudySnapshotError(f"snapshot identity section {section!r} is malformed")
    expected_digest = manifest.get("manifest_digest")
    if not isinstance(expected_digest, str) or not expected_digest.strip():
        raise StudySnapshotError("snapshot manifest digest is malformed")
    if manifest.get("snapshot_id") != snapshot_id_for(expected_digest.lower()):
        raise StudySnapshotError(
            "snapshot manifest snapshot_id does not derive from its manifest "
            "digest (invented ID?)"
        )
    principal = manifest.get("principal") or {}
    if (principal.get("agent_id") != identity["principal"].get("agent_id")
            or principal.get("capabilities") != identity["principal"].get("capabilities")):
        raise StudySnapshotError(
            "snapshot manifest principal mirror does not match bound identity"
        )
    if manifest.get("store") != identity["store"]:
        raise StudySnapshotError("snapshot manifest store mirror does not match bound identity")
    if manifest.get("source") != identity["source"]:
        raise StudySnapshotError("snapshot manifest source mirror does not match bound identity")
    if manifest.get("authorization_claimed") != identity["authorization"].get("claimed"):
        raise StudySnapshotError(
            "snapshot manifest authorization mirror does not match bound identity"
        )
    return identity


def verify_snapshot(snapshot_dir: Path) -> Dict[str, Any]:
    """Re-validate frozen files and metadata; reject symlinks, tamper, and drift.

    Checks snapshot.json/mapping.json presence and versions, the snapshot ID
    derivation, every consumed identity mirror against the bound identity
    block, digest binding over identity plus vault bytes, per-file
    symlink-component/sha/path checks, mapping consistency (counts,
    contiguous study IDs, content groups), and that the vault holds no
    unmapped markdown. Returns ``{"manifest", "mapping"}``.
    """
    root = Path(snapshot_dir)
    if os.path.islink(root):
        raise StudySnapshotError(f"snapshot directory {root} must not be a symlink")
    manifest = _read_json_strict(root / "snapshot.json", root, "snapshot manifest")
    envelope = _read_json_strict(root / "mapping.json", root, "snapshot mapping")
    if not isinstance(manifest, dict):
        raise StudySnapshotError("snapshot manifest must be an object")
    if manifest.get("snapshot_version") != SNAPSHOT_VERSION:
        raise StudySnapshotError("snapshot.json version mismatch; re-prepare the snapshot")
    if manifest.get("human_reviewed") is not False:
        raise StudySnapshotError("snapshot manifest must keep human_reviewed=false")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("records"), dict):
        raise StudySnapshotError("mapping.json must hold a records object with the manifest digest")
    if envelope.get("snapshot_version") != SNAPSHOT_VERSION:
        raise StudySnapshotError("mapping.json version mismatch; re-prepare the snapshot")
    if envelope.get("snapshot_id") != manifest.get("snapshot_id"):
        raise StudySnapshotError(
            "mapping.json snapshot ID does not match snapshot.json; outputs "
            "must not be mixed across snapshots"
        )
    mapping = envelope["records"]
    if envelope.get("manifest_digest") != manifest.get("manifest_digest"):
        raise StudySnapshotError(
            "mapping.json digest does not match snapshot.json; outputs must "
            "not be mixed across snapshots"
        )
    if manifest.get("record_count") != len(mapping):
        raise StudySnapshotError("snapshot record count does not match mapping size")
    expected_ids = [f"{STUDY_ID_PREFIX}{position:04d}" for position in range(1, len(mapping) + 1)]
    if sorted(mapping) != expected_ids:
        raise StudySnapshotError("snapshot mapping study IDs are not contiguous")

    identity = _check_manifest_identity(manifest)

    vault_root = root / "vault"
    if os.path.islink(vault_root):
        raise StudySnapshotError("snapshot vault directory must not be a symlink")
    seen_artifacts = set()
    for study_id in expected_ids:
        row = mapping[study_id]
        if not isinstance(row, dict):
            raise StudySnapshotError(f"snapshot mapping entry {study_id} must be an object")
        artifact = row.get("artifact_path")
        _check_artifact_path(artifact)
        if artifact in seen_artifacts:
            raise StudySnapshotError(f"snapshot mapping repeats artifact path {artifact!r}")
        seen_artifacts.add(artifact)
        judgment = row.get("study_judgment") or {}
        if (judgment.get("review_state") != MACHINE_PROPOSED
                or judgment.get("human_reviewed") is not False
                or row.get("human_reviewed") is not False):
            raise StudySnapshotError(
                f"snapshot entry {study_id} must keep machine_proposed judgment; "
                "machine judgments are never human-reviewed"
            )
        target = vault_root / artifact
        raw = _read_sized_bytes(target, root, f"snapshot vault file {artifact!r}",
                                MAX_VAULT_FILE_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, str(row.get("content_sha256", "")).lower()):
            raise StudySnapshotError(
                f"snapshot vault file {artifact!r} bytes do not match mapping (tamper?)"
            )
        if content_group_for(row["content_sha256"]) != row.get("content_group"):
            raise StudySnapshotError(
                f"snapshot entry {study_id} content group is inconsistent with its bytes"
            )

    try:
        candidates = list(vault_root.rglob("*.md"))
    except OSError as exc:
        raise StudySnapshotError("snapshot vault is unreadable") from exc
    extra = []
    for path in candidates:
        _assert_no_symlink_components(path, root, "snapshot vault entry")
        if path.is_file():
            extra.append(str(path.relative_to(vault_root)))
    unmapped = sorted(set(extra) - seen_artifacts)
    if unmapped:
        raise StudySnapshotError(
            f"snapshot vault holds unmapped files {unmapped}; outputs must "
            "not mix records across snapshots"
        )

    if content_groups_for(mapping) != manifest.get("content_groups", {}):
        raise StudySnapshotError("snapshot content groups are inconsistent with mapping")

    recomputed = manifest_digest_for(_records_from_mapping(mapping, vault_root, root), identity)
    if not hmac.compare_digest(recomputed, str(manifest.get("manifest_digest", "")).lower()):
        raise StudySnapshotError(
            "snapshot manifest digest does not match frozen files and metadata (tamper?)"
        )
    return {"manifest": manifest, "mapping": mapping}


def prepare_snapshot(packet: Any, dest: Path) -> Dict[str, Any]:
    """Validate the packet and freeze it into ``dest``; no DB/model imports.

    Writes ``vault/`` record files, ``mapping.json`` (opaque study IDs), and
    ``snapshot.json`` (identity + integrity), all 0700/0600. Returns the
    manifest dict. This function never imports database, engine, or model
    code, so preparation cannot call model or live-database constructors.
    """
    records = validate_export_packet(packet)
    identity = canonical_identity(packet)
    dest = _reject_live_destination(Path(dest))
    _ensure_private_dir(dest)
    try:
        preexisting = list(dest.iterdir())
    except OSError as exc:
        raise StudySnapshotError(f"snapshot destination {dest} is unreadable") from exc
    if preexisting:
        raise StudySnapshotError(
            f"snapshot destination {dest} is not empty; a second packet is "
            "never written over an existing snapshot — prepare a fresh directory"
        )

    manifest_digest = manifest_digest_for(records, identity)
    snapshot_id = snapshot_id_for(manifest_digest)
    mapping = deterministic_remapping(records)
    groups = content_groups_for(mapping)

    envelope = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "manifest_digest": manifest_digest,
        "records": mapping,
    }
    mapping_text = json.dumps(envelope, indent=2, sort_keys=True)
    if len(mapping_text.encode("utf-8")) > MAX_METADATA_BYTES:
        raise StudySnapshotError(
            f"snapshot mapping metadata exceeds {MAX_METADATA_BYTES} bytes"
        )

    vault_root = dest / "vault"
    for study_id, row in mapping.items():
        record = next(
            r for r in records
            if r["store"] == row["store"] and r["source_doc_id"] == row["source_doc_id"]
        )
        target = vault_root / row["artifact_path"]
        if target.is_symlink() or target.exists():
            raise StudySnapshotError(
                f"snapshot vault collision at {row['artifact_path']!r}; "
                "refreshing means a new snapshot, never silent file swaps"
            )
        _write_private_file(target, record["text"])

    _write_private_file(dest / "mapping.json", mapping_text)
    manifest = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "manifest_digest": manifest_digest,
        "record_count": len(records),
        "packet_version": PACKET_VERSION,
        "identity": identity,
        "principal": {
            "agent_id": identity["principal"]["agent_id"],
            "capabilities": identity["principal"]["capabilities"],
            "provenance_note": _AUTHORIZATION_NOTE,
        },
        "store": identity["store"],
        "source": identity["source"],
        "authorization_claimed": identity["authorization"]["claimed"],
        "content_groups": groups,
        "review_state": MACHINE_PROPOSED,
        "human_reviewed": False,
        "scope": _SCOPE_NOTE,
        "export_pack_note": (
            "sm_export_pack is shared snippets under an export capability, "
            "not a corpus snapshot source; this snapshot derives only from "
            "the authorized-export packet and bypasses no capability."
        ),
    }
    _write_private_file(dest / "snapshot.json", json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def materialize_snapshot_db(snapshot_dir: Path) -> Dict[str, Any]:
    """Build the disposable lexical SQLite/FTS corpus inside ``snapshot_dir``.

    Re-validates frozen files and metadata first (rejecting symlinks,
    tampered bytes, inconsistent mappings, and stale-output mixing), then
    mirrors ``eval/fixture.py`` isolated construction: a ``SovereignConfig``
    with DB, vault, FAISS index, graph, and writeback paths ALL inside the
    snapshot directory, writeback disabled, and original ownership/lifecycle/
    privacy metadata preserved per record. No model is loaded; rows are
    lexical (documents + vault_fts with sigil ``"T"``), exactly like the
    fixture's lexical profile. Never touches the live ``DEFAULT_CONFIG``.
    """
    from minni.config import SovereignConfig
    from minni.db import SovereignDB

    snapshot_dir = _reject_live_destination(Path(snapshot_dir))
    verified = verify_snapshot(snapshot_dir)
    manifest, mapping = verified["manifest"], verified["mapping"]
    vault_root = snapshot_dir / "vault"

    _assert_no_symlink_components(snapshot_dir / "study.db", snapshot_dir,
                                  "materialized snapshot database")
    _assert_no_symlink_components(snapshot_dir / "materialized.json", snapshot_dir,
                                  "materialized outputs")
    if (snapshot_dir / "study.db").exists() or (snapshot_dir / "materialized.json").exists():
        raise StudySnapshotError(
            "snapshot outputs already exist; materialization runs once per "
            "prepared directory so stale records can never mix with new ones"
        )

    config = SovereignConfig(
        db_path=str(snapshot_dir / "study.db"),
        vault_path=str(vault_root),
        faiss_index_path=str(snapshot_dir / "study.faiss"),
        graph_export_dir=str(snapshot_dir / "graphs"),
        writeback_path=str(snapshot_dir / "learnings"),
        writeback_enabled=False,
        reranker_enabled=False,
        hyde_enabled=False,
    )
    previous_umask = os.umask(0o077)
    try:
        db = SovereignDB(config)
    finally:
        os.umask(previous_umask)
    try:
        doc_ids: Dict[str, int] = {}
        for study_id in sorted(mapping):
            row = mapping[study_id]
            provenance = row.get("source_provenance") or {}
            target = vault_root / row["artifact_path"]
            text = _read_sized_text(target, snapshot_dir,
                                    f"snapshot vault file {row['artifact_path']!r}",
                                    MAX_VAULT_FILE_BYTES)
            with db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO documents(path,agent,privacy_level,page_status,page_type,sigil)"
                    " VALUES(?,?,?,?,?,?)",
                    (str(target), provenance.get("agent", row.get("agent")),
                     provenance.get("privacy_level", row.get("privacy_level")),
                     provenance.get("page_status", "active"),
                     provenance.get("page_type", "note"), "T"),
                )
                doc_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO vault_fts(doc_id,path,content,agent,sigil) VALUES(?,?,?,?,?)",
                    (doc_id, str(target), text,
                     provenance.get("agent", row.get("agent")), "T"),
                )
            doc_ids[study_id] = doc_id
    finally:
        db.close()
    for artifact in (Path(config.db_path), Path(f"{config.db_path}-wal"),
                     Path(f"{config.db_path}-shm")):
        if artifact.exists():
            os.chmod(artifact, 0o600)
    schema_digest = _materialized_schema_digest(Path(config.db_path))
    materialized = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": manifest["manifest_digest"],
        "document_ids": doc_ids,
        "schema_digest": schema_digest,
    }
    _write_private_file(
        snapshot_dir / "materialized.json", json.dumps(materialized, indent=2, sort_keys=True))
    return {
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": manifest["manifest_digest"],
        "document_ids": doc_ids,
        "db_path": config.db_path,
        "vault_path": config.vault_path,
    }


def _immutable_db_rows(db_path: Path) -> Dict[str, Dict[str, Any]]:
    """Immutable retrieval content per doc_id from a read-only handle.

    Only content-bearing columns are compared (path, agent, privacy,
    lifecycle, sigil, FTS text). Runtime counters and timestamps are
    deliberately excluded: normal governed search/drill access effects must
    never read as an integrity failure.
    """
    uri = "file:" + _url_quote(str(db_path), safe="/:") + "?mode=ro"
    try:
        import sqlite3

        handle = sqlite3.connect(uri, uri=True)
        try:
            handle.execute("PRAGMA query_only = ON")
            documents = handle.execute(
                "SELECT doc_id, path, agent, privacy_level, page_status, page_type, sigil, decay_score"
                " FROM documents"
            ).fetchall()
            fts_rows = handle.execute(
                "SELECT doc_id, path, content, agent FROM vault_fts"
            ).fetchall()
        finally:
            handle.close()
    except Exception as exc:  # noqa: BLE001 - any DB problem is an integrity failure
        raise StudySnapshotError(f"materialized snapshot database is unreadable: {type(exc).__name__}") from exc
    # Exact one-to-one identities: no dict collapse. Duplicate document rows,
    # duplicate FTS rows, and orphan FTS rows (retrieval content with no
    # document) are all rejected instead of letting last-wins hide extras.
    document_ids = [row[0] for row in documents]
    if len(set(document_ids)) != len(document_ids):
        raise StudySnapshotError("materialized database holds duplicate document rows")
    fts_ids = [row[0] for row in fts_rows]
    if len(set(fts_ids)) != len(fts_rows):
        raise StudySnapshotError("materialized database holds duplicate FTS rows")
    orphans = set(fts_ids) - set(document_ids)
    if orphans:
        raise StudySnapshotError(
            "materialized database holds orphan FTS rows with no document"
        )
    fts = {doc_id: (path, content, agent) for doc_id, path, content, agent in fts_rows}
    rows: Dict[str, Dict[str, Any]] = {}
    for doc_id, path, agent, privacy_level, page_status, page_type, sigil, decay_score in documents:
        rows[str(doc_id)] = {
            "path": path, "agent": agent, "privacy_level": privacy_level,
            "page_status": page_status, "page_type": page_type, "sigil": sigil,
            "decay_score": decay_score,
            "fts": fts.get(doc_id),
        }
    return rows


def _materialized_schema_digest(db_path: Path) -> str:
    """Digest the SQLite schema that determines snapshot retrieval behavior."""
    import sqlite3

    uri = "file:" + _url_quote(str(db_path), safe="/:") + "?mode=ro"
    try:
        handle = sqlite3.connect(uri, uri=True)
        try:
            schema = handle.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        finally:
            handle.close()
    except Exception as exc:  # noqa: BLE001 - schema is integrity evidence
        raise StudySnapshotError("materialized snapshot schema is unreadable") from exc
    return hashlib.sha256(
        json.dumps(schema, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def check_materialized(snapshot_dir: Path) -> Dict[str, Any]:
    """Confirm the disposable DB outputs belong to the verified snapshot.

    Beyond the manifest digest, every ``document_ids`` entry is bound to the
    actual immutable SQLite content: altered rows, swapped IDs, or edited FTS
    text all fail. Runtime access counters are excluded from the comparison
    so normal governed search effects are never misread as tampering.
    """
    root = Path(snapshot_dir)
    verified = verify_snapshot(root)
    manifest, mapping = verified["manifest"], verified["mapping"]
    materialized = _read_json_strict(root / "materialized.json", root, "materialized outputs")
    if not isinstance(materialized, dict):
        raise StudySnapshotError("materialized outputs must be an object")
    if materialized.get("manifest_digest") != manifest["manifest_digest"]:
        raise StudySnapshotError(
            "materialized outputs do not belong to this snapshot; never mix "
            "outputs across snapshots"
        )
    if materialized.get("snapshot_id") != manifest["snapshot_id"]:
        raise StudySnapshotError("materialized snapshot ID does not match the snapshot")
    db_path = root / "study.db"
    _assert_no_symlink_components(db_path, root, "materialized snapshot database")
    try:
        is_file = db_path.is_file()
    except OSError as exc:
        raise StudySnapshotError("materialized snapshot database is unreadable") from exc
    if not is_file:
        raise StudySnapshotError("materialized snapshot database is missing")

    document_ids = materialized.get("document_ids")
    if not isinstance(document_ids, dict) or sorted(document_ids) != sorted(mapping):
        raise StudySnapshotError(
            "materialized document IDs do not cover exactly the snapshot mapping"
        )
    schema_digest = materialized.get("schema_digest")
    if not isinstance(schema_digest, str) or schema_digest != _materialized_schema_digest(db_path):
        raise StudySnapshotError("materialized database schema does not match the frozen snapshot")
    rows = _immutable_db_rows(db_path)
    if len(rows) != len(mapping):
        raise StudySnapshotError("materialized database row count does not match the snapshot")
    vault_root = root / "vault"
    seen_doc_ids = set()
    for study_id, row in mapping.items():
        doc_id = document_ids.get(study_id)
        # Exact type check: isinstance(True, int) is True, and a boolean ID
        # is never a valid document identity.
        if type(doc_id) is not int or doc_id < 1:
            raise StudySnapshotError(
                f"materialized document ID for {study_id} is not a positive integer"
            )
        if doc_id in seen_doc_ids:
            raise StudySnapshotError(
                f"materialized document ID {doc_id} is claimed by two study records"
            )
        seen_doc_ids.add(doc_id)
        actual = rows.get(str(doc_id))
        if actual is None:
            raise StudySnapshotError(
                f"materialized database has no row for {study_id} (doc_id {doc_id})"
            )
        provenance = row.get("source_provenance") or {}
        expected_path = str(vault_root / row["artifact_path"])
        expected_text = _read_sized_text(
            vault_root / row["artifact_path"], root,
            f"snapshot vault file {row['artifact_path']!r}", MAX_VAULT_FILE_BYTES)
        expected_agent = provenance.get("agent", row.get("agent"))
        if actual["path"] != expected_path or actual["fts"] is None:
            raise StudySnapshotError(
                f"materialized database row for {study_id} does not match frozen files (tamper?)"
            )
        fts_path, fts_content, fts_agent = actual["fts"]
        if (actual["agent"] != expected_agent
                or actual["privacy_level"] != provenance.get("privacy_level", row.get("privacy_level"))
                or actual["page_status"] != provenance.get("page_status", "active")
                or actual["page_type"] != provenance.get("page_type", "note")
                or actual["decay_score"] != 1.0
                or actual["sigil"] != "T"
                or fts_path != expected_path
                or fts_content != expected_text
                or fts_agent != expected_agent):
            raise StudySnapshotError(
                f"materialized database content for {study_id} does not match "
                "frozen files and metadata (tamper?)"
            )
    return materialized


def snapshot_config_paths(snapshot_dir: Path) -> Dict[str, str]:
    """Disposable backend paths for a snapshot directory (all inside it)."""
    root = _reject_live_destination(Path(snapshot_dir))
    return {
        "db_path": str(root / "study.db"),
        "vault_path": str(root / "vault"),
        "faiss_index_path": str(root / "study.faiss"),
        "graph_export_dir": str(root / "graphs"),
        "writeback_path": str(root / "learnings"),
    }


def source_identity_key(store: str, source_doc_id: str) -> Tuple[str, str]:
    """Source identity is the (store, source_doc_id) tuple, never a bare number."""
    return (store, source_doc_id)
