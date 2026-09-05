"""Bounded retrieval-study snapshot foundation for private-memory studies.

The study target is Hans's day-to-day cross-project memories. Nothing in
this module collects, exports, or reads live memories: the ONLY input is an
explicit, bounded ``authorized-export packet`` supplied by the parent, which
connects the governed export separately. The packet carries its own
principal/store/source identity plus record content; arbitrary filesystem
paths and vault dumps are never accepted.

Provenance honesty rules enforced here:

- Claimed authorization in the packet is recorded as *supplied provenance*,
  never as independently verified permission.
- Every record review state must be ``machine_proposed`` with
  ``human_reviewed`` exactly ``False``. Machine judgments are never labeled
  human-reviewed.
- ``sm_export_pack`` is a shared-snippet, export-cap-gated RPC, not a corpus
  snapshot source; this module never calls it and bypasses no capability.
- A snapshot ID is derived from the packet manifest digest only. Snapshot
  IDs are never assigned to the live corpus.
- Reports state scope honestly: a bounded packet study, not representative
  private-memory quality and not a retrieval-performance claim.

Isolation rules:

- :func:`prepare_snapshot` writes vault files, an opaque remapping, and a
  manifest under a private (0700 dirs / 0600 files) destination. It imports
  no database, engine, or model code and therefore cannot instantiate the
  live ``DEFAULT_CONFIG`` database or load a model.
- :func:`materialize_snapshot_db` builds the disposable SQLite/FTS corpus
  the way ``eval/fixture.py`` does (lexical rows, fixed study principal),
  with EVERY database/index/vault path inside the snapshot directory.
- ``SnapshotSearcher`` (in ``eval/retrievers.py``) opens only the prepared
  snapshot directory and never the live ``DEFAULT_CONFIG``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List

PACKET_VERSION = "minni-study-export-v1"
SNAPSHOT_VERSION = "minni-study-snapshot-v1"

MACHINE_PROPOSED = "machine_proposed"
CONTENT_KINDS = {"original", "excerpt"}

# Records are addressed in the study only through these opaque IDs. The
# original store identities stay in mapping.json for audit, never as
# retrieval addresses.
STUDY_ID_PREFIX = "study-"

_SCOPE_NOTE = (
    "Bounded authorized-export packet study over day-to-day cross-project "
    "memories; not representative private-memory quality, not a retrieval "
    "performance claim, and not a default-change signal."
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


def _check_artifact_path(raw: Any) -> str:
    """Relative markdown study paths only; never absolute or traversing."""
    value = _require_nonempty_str(raw, "record artifact_path")
    ref = Path(value)
    if ref.is_absolute() or ".." in ref.parts or ref.suffix != ".md":
        raise StudySnapshotError(
            "record artifact_path must be a relative markdown path without traversal"
        )
    if "\\" in value:
        raise StudySnapshotError("record artifact_path must use forward slashes")
    return value


def _canonical_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Fixed-shape record used for the manifest digest."""
    return {
        "agent": record["agent"],
        "artifact_path": record["artifact_path"],
        "content_kind": record["content_kind"],
        "content_sha256": record["content_sha256"],
        "expected_eligible": record["expected_eligible"],
        "human_reviewed": record["human_reviewed"],
        "origin": record["origin"],
        "privacy_level": record["privacy_level"],
        "review_state": record["review_state"],
        "source_doc_id": record["source_doc_id"],
        "source_locator": record.get("source_locator"),
        "store": record["store"],
        "text": record["text"],
    }


def manifest_digest_for(records: List[Dict[str, Any]]) -> str:
    """Deterministic manifest digest over canonical records sorted by identity."""
    canonical = sorted(
        (_canonical_record(record) for record in records),
        key=lambda row: (row["store"], row["source_doc_id"]),
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_id_for(manifest_digest: str) -> str:
    """Opaque study ID derived from packet content only; never a live-corpus ID."""
    return f"{STUDY_ID_PREFIX}{manifest_digest[:16]}"


def validate_export_packet(packet: Any) -> List[Dict[str, Any]]:
    """Validate a bounded authorized-export packet; return normalized records.

    Raises :class:`StudySnapshotError` on malformed data, manifest tampering,
    duplicate source identity across stores, duplicate content digests,
    unsafe artifact paths, missing excerpt/original labels, or any
    human-reviewed claim.
    """
    top = _require_dict(packet, "study packet")
    if top.get("packet_version") != PACKET_VERSION:
        raise StudySnapshotError(
            f"study packet packet_version must be {PACKET_VERSION!r}"
        )

    principal = _require_dict(top.get("principal"), "packet principal")
    _require_nonempty_str(principal.get("agent_id"), "packet principal agent_id")

    store = _require_dict(top.get("store"), "packet store")
    _require_nonempty_str(store.get("store_id"), "packet store store_id")
    _require_nonempty_str(store.get("origin"), "packet store origin")

    source = _require_dict(top.get("source"), "packet source")
    _require_nonempty_str(source.get("origin"), "packet source origin")

    authorization = _require_dict(top.get("authorization"), "packet authorization")
    _require_nonempty_str(authorization.get("claimed"), "packet authorization claimed")

    records = top.get("records")
    if not isinstance(records, list) or not records:
        raise StudySnapshotError("packet records must be a non-empty list")

    manifest = _require_dict(top.get("manifest"), "packet manifest")
    expected_digest = _require_nonempty_str(
        manifest.get("manifest_digest"), "packet manifest manifest_digest"
    )

    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(records):
        label = f"record[{index}]"
        row = _require_dict(raw, label)
        source_doc_id = _require_nonempty_str(row.get("source_doc_id"), f"{label} source_doc_id")
        store_id = _require_nonempty_str(row.get("store"), f"{label} store")
        artifact_path = _check_artifact_path(row.get("artifact_path"))
        text = _require_nonempty_str(row.get("text"), f"{label} text")

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
            _require_nonempty_str(source_locator, f"{label} source_locator (excerpts must cite their origin)")
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

        agent = _require_nonempty_str(row.get("agent"), f"{label} agent (source ownership)")
        privacy_level = _require_nonempty_str(
            row.get("privacy_level"), f"{label} privacy_level (privacy metadata)"
        )
        origin = _require_nonempty_str(row.get("origin"), f"{label} origin")
        if type(row.get("expected_eligible")) is not bool:
            raise StudySnapshotError(
                f"{label}: expected_eligible must be an explicit boolean "
                "(cross-project eligibility is annotated, never inferred)"
            )
        normalized.append({
            "agent": agent,
            "artifact_path": artifact_path,
            "content_kind": content_kind,
            "content_sha256": actual_digest,
            "expected_eligible": row["expected_eligible"],
            "human_reviewed": False,
            "origin": origin,
            "privacy_level": privacy_level,
            "review_state": MACHINE_PROPOSED,
            "source_doc_id": source_doc_id,
            "source_locator": source_locator if isinstance(source_locator, str) else None,
            "store": store_id,
            "text": text,
            "page_status": row.get("page_status", "active"),
            "page_type": row.get("page_type", "note"),
        })
    for row in normalized:
        if not isinstance(row["page_status"], str) or not row["page_status"].strip():
            raise StudySnapshotError("record page_status must be a non-empty string when present")
        if not isinstance(row["page_type"], str) or not row["page_type"].strip():
            raise StudySnapshotError("record page_type must be a non-empty string when present")

    # Source identity is globally unique: the same source_doc_id in a second
    # store is a duplicate cross-store identity, not a second document.
    seen_sources = set()
    seen_artifacts = set()
    seen_digests: Dict[str, str] = {}
    for row in normalized:
        if row["source_doc_id"] in seen_sources:
            raise StudySnapshotError(
                f"duplicate source identity {row['source_doc_id']!r} across stores"
            )
        seen_sources.add(row["source_doc_id"])
        if row["artifact_path"] in seen_artifacts:
            raise StudySnapshotError(
                f"duplicate artifact path {row['artifact_path']!r} in study packet"
            )
        seen_artifacts.add(row["artifact_path"])
        owner = seen_digests.get(row["content_sha256"])
        if owner is not None:
            raise StudySnapshotError(
                f"duplicate content digest for {row['source_doc_id']!r} "
                f"(already used by {owner!r}); identical bytes need one identity"
            )
        seen_digests[row["content_sha256"]] = row["source_doc_id"]

    actual = manifest_digest_for(normalized)
    if not hmac.compare_digest(actual, expected_digest.lower()):
        raise StudySnapshotError("packet manifest_digest does not match record content (tamper?)")
    return normalized


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


def deterministic_remapping(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Stable opaque study-ID remapping, sorted by (store, source_doc_id)."""
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
            "review_state": row["review_state"],
            "human_reviewed": False,
            "agent": row["agent"],
            "privacy_level": row["privacy_level"],
            "origin": row["origin"],
            "expected_eligible": row["expected_eligible"],
        }
    return mapping


def prepare_snapshot(packet: Any, dest: Path) -> Dict[str, Any]:
    """Validate the packet and freeze it into ``dest``; no DB/model imports.

    Writes ``vault/`` record files, ``mapping.json`` (opaque study IDs), and
    ``snapshot.json`` (identity + integrity), all 0700/0600. Returns the
    manifest dict. This function never imports database, engine, or model
    code, so preparation cannot call model or live-database constructors.
    """
    records = validate_export_packet(packet)
    dest = Path(dest)
    _ensure_private_dir(dest)

    top = packet
    manifest_digest = manifest_digest_for(records)
    snapshot_id = snapshot_id_for(manifest_digest)
    mapping = deterministic_remapping(records)

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

    _write_private_file(dest / "mapping.json", json.dumps(mapping, indent=2, sort_keys=True))
    manifest = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "manifest_digest": manifest_digest,
        "record_count": len(records),
        "packet_version": PACKET_VERSION,
        "principal": {
            "agent_id": top["principal"]["agent_id"],
            "capabilities": top["principal"].get("capabilities"),
            "provenance_note": (
                "Supplied packet provenance; authorization claimed by the "
                "packet, not independently verified permission."
            ),
        },
        "store": {
            "store_id": top["store"]["store_id"],
            "origin": top["store"]["origin"],
        },
        "source": top["source"],
        "authorization_claimed": top["authorization"]["claimed"],
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

    Mirrors ``eval/fixture.py`` isolated construction: a ``SovereignConfig``
    with DB, vault, FAISS index, graph, and writeback paths ALL inside the
    snapshot directory, writeback disabled, and source ownership/privacy
    metadata preserved per record. No model is loaded; rows are lexical
    (documents + vault_fts with sigil ``"T"``), exactly like the fixture's
    lexical profile. Never touches the live ``DEFAULT_CONFIG``.
    """
    from minni.config import SovereignConfig
    from minni.db import SovereignDB

    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
    mapping = json.loads((snapshot_dir / "mapping.json").read_text(encoding="utf-8"))
    vault_root = snapshot_dir / "vault"
    if manifest.get("snapshot_version") != SNAPSHOT_VERSION:
        raise StudySnapshotError("snapshot.json version mismatch; re-prepare the snapshot")
    if manifest.get("human_reviewed") is not False:
        raise StudySnapshotError("snapshot manifest must keep human_reviewed=false")

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
    db = SovereignDB(config)
    try:
        doc_ids: Dict[str, int] = {}
        for study_id in sorted(mapping):
            row = mapping[study_id]
            target = vault_root / row["artifact_path"]
            text = target.read_text(encoding="utf-8")
            with db.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO documents(path,agent,privacy_level,page_status,page_type,sigil)"
                    " VALUES(?,?,?,?,?,?)",
                    (str(target), row["agent"], row["privacy_level"],
                     "active", "note", "T"),
                )
                doc_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO vault_fts(doc_id,path,content,agent,sigil) VALUES(?,?,?,?,?)",
                    (doc_id, str(target), text, row["agent"], "T"),
                )
            doc_ids[study_id] = doc_id
    finally:
        db.close()
    return {
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": manifest["manifest_digest"],
        "document_ids": doc_ids,
        "db_path": config.db_path,
        "vault_path": config.vault_path,
    }
