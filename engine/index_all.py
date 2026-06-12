"""
Minni V3.1 — Unified Indexer.

Runs all source indexers (vault + wiki) in sequence, then rebuilds
the shared FAISS index from all accumulated embeddings.

Usage:
    python index_all.py              # Index everything (vault + wiki)
    python index_all.py --vault-only # Index only the Obsidian vault
    python index_all.py --wiki-only  # Index only wiki directories
    python index_all.py --vault-ingest-all
                                      # Index all ~/.minni/*-vault wiki trees
    python index_all.py --verbose    # Show per-file progress
"""

import argparse
import sys
import logging
from pathlib import Path
from typing import Dict

from config import CANONICAL_SOVEREIGN_HOME, SovereignConfig, DEFAULT_CONFIG
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


def discover_agent_vaults(minni_home: str | Path | None = None) -> list[Path]:
    """Return ~/.minni/*-vault dirs, excluding the legacy bare ~/.minni/vault."""
    home = Path(minni_home or CANONICAL_SOVEREIGN_HOME).expanduser()
    if not home.is_dir():
        return []
    return sorted(
        path
        for path in home.glob("*-vault")
        if path.is_dir() and path.name != "vault"
    )


def index_agent_vaults(
    config: SovereignConfig = None,
    *,
    minni_home: str | Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, Dict]:
    """Run vault_ingest across every discovered agent vault."""
    config = config or DEFAULT_CONFIG
    db = SovereignDB(config)
    from afm_passes.vault_ingest import run as run_vault_ingest

    combined: Dict[str, Dict] = {}
    for vault in discover_agent_vaults(minni_home):
        if verbose:
            logger.info("═══ Vault ingest: %s ═══", vault)
        stats = run_vault_ingest(
            db,
            config,
            vault_path=str(vault),
            dry_run=dry_run,
            trace_id="manual-index-all",
        )
        combined[str(vault)] = stats
        if verbose:
            logger.info("Vault ingest %s: %s", vault, stats)
    db.close()
    return combined


def _print_stats(title: str, stats: Dict) -> None:
    print(f"\n{'═' * 50}")
    print(title)
    if not stats:
        print("  no sources found")
        return
    for source, s in stats.items():
        if isinstance(s, dict):
            details = ", ".join(
                f"{k}={v}" for k, v in s.items()
                if k not in {"status", "drafts"}
            )
            print(f"  {source}: {details}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Minni indexers.")
    parser.add_argument("--vault-only", action="store_true", help="Index only the legacy configured vault")
    parser.add_argument("--wiki-only", action="store_true", help="Index only configured wiki paths")
    parser.add_argument("--vault-ingest-all", action="store_true", help="Run vault_ingest for every ~/.minni/*-vault")
    parser.add_argument("--dry-run", action="store_true", help="Report vault_ingest work without writing index stores")
    parser.add_argument("--minni-home", help="Override ~/.minni for --vault-ingest-all")
    parser.add_argument("--verbose", action="store_true", help="Show per-file progress")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.vault_ingest_all:
        stats = index_agent_vaults(
            minni_home=args.minni_home,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        _print_stats("Vault ingest complete:", stats)
        return 0

    do_vault = not args.wiki_only
    do_wiki = not args.vault_only

    stats = index_all(vault=do_vault, wiki=do_wiki, verbose=args.verbose)
    _print_stats("Index complete:", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
