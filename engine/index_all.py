"""
Sovereign Memory V3.1 — Unified Indexer.

Runs all source indexers (vault + wiki) in sequence, then rebuilds
the shared FAISS index from all accumulated embeddings.

Usage:
    python index_all.py              # Index everything (vault + wiki)
    python index_all.py --vault-only # Index only the Obsidian vault
    python index_all.py --wiki-only  # Index only wiki directories
    python index_all.py --verbose    # Show per-file progress
"""

import sys
import logging
from typing import Dict

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from indexer import VaultIndexer
from wiki_indexer import WikiIndexer

logger = logging.getLogger("sovereign.index_all")


def index_all(
    config: SovereignConfig = None,
    vault: bool = True,
    wiki: bool = True,
    verbose: bool = False,
) -> Dict:
    """
    Run all indexers and rebuild the shared FAISS index.

    Returns combined stats dict.
    """
    config = config or DEFAULT_CONFIG
    db = SovereignDB(config)

    combined_stats = {}

    # 1. Index the Obsidian vault
    if vault:
        logger.info("═══ Indexing vault: %s ═══", config.vault_path)
        vault_idx = VaultIndexer(db, config)
        vault_stats = vault_idx.index_vault(verbose=verbose)
        combined_stats["vault"] = vault_stats
        logger.info("Vault: %s", vault_stats)

    # 2. Index all wiki directories
    if wiki:
        for wiki_path in config.wiki_paths:
            logger.info("═══ Indexing wiki: %s ═══", wiki_path)
            wiki_idx = WikiIndexer(db, config)
            wiki_stats = wiki_idx.index_wiki(wiki_path, verbose=verbose)
            combined_stats[f"wiki:{wiki_path}"] = wiki_stats
            logger.info("Wiki %s: %s", wiki_path, wiki_stats)

    # 3. Rebuild shared FAISS index from ALL embeddings (vault + wiki)
    logger.info("═══ Rebuilding FAISS index ═══")
    from faiss_index import FAISSIndex
    import numpy as np

    faiss = FAISSIndex(config)
    chunk_ids = []
    embeddings = []

    with db.cursor() as c:
        c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
        for row in c.fetchall():
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.shape[0] == config.embedding_dim:
                chunk_ids.append(row["chunk_id"])
                embeddings.append(vec)

    if chunk_ids:
        all_vecs = np.array(embeddings, dtype=np.float32)
        faiss.build_from_vectors(chunk_ids, all_vecs)
        logger.info("FAISS index rebuilt: %d vectors (%s)",
                    len(chunk_ids), faiss._current_type)
    else:
        logger.warning("No embeddings found — FAISS index empty")

    combined_stats["faiss"] = {"vectors": len(chunk_ids)}

    db.close()
    return combined_stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    args = set(sys.argv[1:])
    verbose = "--verbose" in args
    vault_only = "--vault-only" in args
    wiki_only = "--wiki-only" in args

    do_vault = not wiki_only
    do_wiki = not vault_only

    stats = index_all(vault=do_vault, wiki=do_wiki, verbose=verbose)
    print(f"\n{'═' * 50}")
    print("Index complete:")
    for source, s in stats.items():
        if isinstance(s, dict):
            details = ", ".join(
                f"{k}={v}" for k, v in s.items()
                if k != "status"
            )
            print(f"  {source}: {details}")
