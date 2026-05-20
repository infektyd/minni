#!/usr/bin/env python3
"""
Seed identity (Layer 1) for OpenClaw agents into Sovereign Memory.

Reads SOUL.md and IDENTITY.md from canonical disk locations and writes them
into the sovereign_memory.db documents/chunk_embeddings tables as whole
documents (whole_document=1, agent='identity:<agent_id>').
"""

import os
import sys
import time
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.openclaw/sovereign_memory.db")

# Canonical identity file locations (SOUL.md + IDENTITY.md per agent)
# We use the FIRST existing source per the discovery priority.
# Priority: ~/.openclaw/agents/<id>/agent/ > ~/.hermes/profiles/<id>/ > ~/.openclaw/identities/<id>/

AGENTS = {
    "forge": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/forge/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/forge/agent/IDENTITY.md"),
        ],
        "fallback_paths": [
            os.path.expanduser("~/.hermes/profiles/forge/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/forge/SOUL.md"),
        ],
    },
    "syntra": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/syntra/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/syntra/agent/IDENTITY.md"),
        ],
        "fallback_paths": [
            os.path.expanduser("~/.hermes/profiles/syntra/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/syntra/SOUL.md"),
        ],
    },
    "recon": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/recon/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/recon/agent/IDENTITY.md"),
        ],
        "fallback_paths": [
            os.path.expanduser("~/.hermes/profiles/recon/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/recon/SOUL.md"),
        ],
    },
    "pulse": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/pulse/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/pulse/agent/IDENTITY.md"),
        ],
        "fallback_paths": [
            os.path.expanduser("~/.hermes/profiles/pulse/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/pulse/SOUL.md"),
        ],
    },
    "hermes": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/hermes/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/hermes/agent/IDENTITY.md"),
        ],
        "fallback_paths": [
            os.path.expanduser("~/.openclaw/identities/hermes/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/hermes/IDENTITY.md"),
        ],
    },
    "vidar": {
        "paths": [
            os.path.expanduser("~/.hermes/profiles/vidar/SOUL.md"),
        ],
        "fallback_paths": [],
    },
    "drift": {
        "paths": [
            os.path.expanduser("~/.openclaw/agents/drift/agent/SOUL.md"),
            os.path.expanduser("~/.openclaw/agents/drift/SOUL.md"),
            os.path.expanduser("~/.openclaw/identities/drift/SOUL.md"),
            os.path.expanduser("~/.hermes/profiles/drift/SOUL.md"),
        ],
        "fallback_paths": [],
    },
}

# Get sigil from config
AGENT_SIGILS = {
    "forge": "🔨",
    "syntra": "🧠",
    "recon": "🔍",
    "pulse": "💓",
    "hermes": "🪽",
    "vidar": "🔮",
}


def discover_sources():
    """Return {agent_id: [(file_path, file_type)] or None if no sources found}."""
    discovery = {}
    for agent_id, config in AGENTS.items():
        files = []

        # Try primary paths first
        for path in config["paths"]:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                fname = os.path.basename(path).upper()
                files.append((path, fname))

        # If nothing found in primary, try fallback
        if not files and config.get("fallback_paths"):
            for path in config["fallback_paths"]:
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    fname = os.path.basename(path).upper()
                    # Check if it's an IDENTITY.md
                    if "IDENTITY" in fname:
                        files.append((path, "IDENTITY.md"))
                    else:
                        files.append((path, "SOUL.md"))
                    break  # Just take the first fallback

        if files:
            discovery[agent_id] = files
        else:
            discovery[agent_id] = None

    return discovery


def get_embedding(text, model=None):
    """Generate embedding for identity text. Uses the shared model singleton."""
    try:
        if model is None:
            # Use the process-wide singleton to avoid reloading weights
            import sys as _sys
            import os as _os
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from models import get_embedder
            model = get_embedder()
        if model is None:
            raise ImportError("embedding model unavailable")
        emb = model.encode(text)
        import numpy as np
        return emb.astype(np.float32).tobytes()
    except ImportError:
        print("WARNING: sentence-transformers not available, using zeros", file=sys.stderr)
        import numpy as np
        return np.zeros(384, dtype=np.float32).tobytes()


def _ensure_wiki_dirs():
    """
    PR-2: Ensure new vault wiki subdirectories exist on init.

    Creates wiki/procedures/, wiki/artifacts/, wiki/handoffs/ in addition
    to whatever the plugin creates.
    """
    vault_path = os.path.expanduser("~/wiki")
    new_dirs = [
        os.path.join(vault_path, "wiki", "procedures"),
        os.path.join(vault_path, "wiki", "artifacts"),
        os.path.join(vault_path, "wiki", "handoffs"),
        os.path.join(vault_path, "inbox"),
        os.path.join(vault_path, "outbox"),
    ]
    for d in new_dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            print(f"  WARNING: Could not create {d}: {e}", file=sys.stderr)


def seed_identity():
    """Main seeding function."""
    # PR-2: Ensure new wiki dirs exist
    _ensure_wiki_dirs()

    discovery = discover_sources()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Ensure whole_document column exists
    c.execute("PRAGMA table_info(documents)")
    columns = {row["name"] for row in c.fetchall()}
    if "whole_document" not in columns:
        print("ERROR: whole_document column not found in documents table")
        sys.exit(1)

    now = time.time()
    seeded_at = datetime.now(timezone.utc).isoformat()

    results = {}
    model = None

    for agent_id in sorted(discovery.keys()):
        sources = discovery[agent_id]
        sigil = AGENT_SIGILS.get(agent_id, "❓")

        if sources is None:
            results[agent_id] = {"status": "skipped", "reason": "No identity source files found"}
            print(f"  SKIP {agent_id}: no source files")
            continue

        # Check if already seeded
        c.execute(
            "SELECT doc_id, path FROM documents WHERE agent = ? AND whole_document = 1",
            (f"identity:{agent_id}",)
        )
        existing = c.fetchall()

        if existing:
            results[agent_id] = {"status": "already_exists", "docs": [dict(r) for r in existing]}
            print(f"  SKIP {agent_id}: already has {len(existing)} identity doc(s)")
            continue

        doc_ids = []
        for source_path, file_type in sources:
            try:
                with open(source_path, "r", encoding="utf-8") as f:
                    content = f.read()

                if not content.strip():
                    print(f"  SKIP {agent_id}/{file_type}: file is empty")
                    continue

                # Generate path for unique doc identification
                doc_path = source_path

                # Insert into documents table
                c.execute(
                    """INSERT INTO documents
                       (path, agent, sigil, last_modified, indexed_at, whole_document)
                       VALUES (?, ?, ?, ?, ?, 1)""",
                    (doc_path, f"identity:{agent_id}", sigil, now, now)
                )
                doc_id = c.lastrowid

                # Generate embedding (lazy load model)
                emb_bytes = get_embedding(content, model)

                # Insert into chunk_embeddings as a single whole chunk
                c.execute(
                    """INSERT INTO chunk_embeddings
                       (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at)
                       VALUES (?, 0, ?, ?, 'all-MiniLM-L6-v2', ?)""",
                    (doc_id, content, emb_bytes, now)
                )

                # Also insert into vault_fts for keyword search
                c.execute(
                    """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, doc_path, content, f"identity:{agent_id}", sigil)
                )

                doc_ids.append(doc_id)
                print(f"  SEED {agent_id}/{file_type}: {os.path.basename(source_path)} -> doc_id={doc_id} ({len(content)} bytes)")

            except Exception as e:
                print(f"  ERROR {agent_id}/{file_type}: {e}")
                results[agent_id] = {"status": "error", "error": str(e)}
                continue

        if doc_ids:
            results[agent_id] = {
                "status": "seeded",
                "source_paths": [s[0] for s in sources],
                "doc_ids": doc_ids,
                "seeded_at": seeded_at,
            }

    conn.commit()
    conn.close()
    return results


def verify_identity():
    """Verify round-trip: read back identity from DB for each seeded agent."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent_api import SovereignAgent

    verifications = {}
    for agent_id in sorted(AGENTS.keys()):
        try:
            agent = SovereignAgent(agent_id)
            identity = agent.identity_context()
            agent.close()

            if identity:
                # Check that it contains the expected header
                has_header = f"## Agent Identity: {agent_id.title()}" in identity
                verifications[agent_id] = {
                    "status": "verified",
                    "has_header": has_header,
                    "content_length": len(identity),
                }
                print(f"  VERIFY {agent_id}: OK ({len(identity)} bytes, header={has_header})")
            else:
                verifications[agent_id] = {
                    "status": "empty",
                    "content_length": 0,
                }
                print(f"  VERIFY {agent_id}: EMPTY")
        except Exception as e:
            verifications[agent_id] = {
                "status": "error",
                "error": str(e),
            }
            print(f"  VERIFY {agent_id}: ERROR - {e}")

    return verifications


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "seed"

    if action == "seed":
        print("=== Seeding identities ===")
        results = seed_identity()
        print()
        print("=== Results ===")
        for agent_id, result in sorted(results.items()):
            print(f"  {agent_id}: {result['status']}")

    elif action == "verify":
        print("=== Verifying identities ===")
        results = verify_identity()
        print()
        print("=== Results ===")
        for agent_id, result in sorted(results.items()):
            print(f"  {agent_id}: {result['status']}")

    elif action == "both":
        print("=== Seeding identities ===")
        seed_results = seed_identity()
        print()
        print("=== Verifying identities ===")
        verify_results = verify_identity()
        print()
        print("=== Summary ===")
        for agent_id in sorted(AGENTS.keys()):
            sr = seed_results.get(agent_id, {"status": "unknown"})
            vr = verify_results.get(agent_id, {"status": "unknown"})
            print(f"  {agent_id}: seed={sr['status']}, verify={vr['status']}")
    else:
        print(f"Usage: seed_identity.py [seed|verify|both]")
        sys.exit(1)
