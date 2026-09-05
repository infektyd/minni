"""Shared identity and metadata for committed learning document projections."""
import hashlib
import os

ACTIVE_LEARNING_SQL = """superseded_by IS NULL AND
    (status IS NULL OR status NOT IN ('rejected', 'expired', 'superseded'))"""


def durable_doc_path(agent_id, key, vault_path, content=None):
    seed = content if content is not None else key
    digest = hashlib.sha1(f"{agent_id}\x00{seed}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(vault_path, "_durable", f"{agent_id}__{digest}.md")


def durable_metadata(content):
    from minni.indexer import VaultIndexer

    meta = VaultIndexer._extract_frontmatter(content)
    return {
        "sigil": meta.get("sigil", "❓"),
        "page_status": "accepted" if meta["page_status"] == "candidate" else meta["page_status"],
        "privacy_level": meta["privacy_level"],
        # Never accept model-supplied ownership or a cross-agent-readable type.
        "page_type": "learning",
        "layer": meta["layer"],
    }
