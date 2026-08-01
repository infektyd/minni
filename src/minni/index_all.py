"""
Minni V3.1 — Unified Indexer.

Runs all source indexers (vault + wiki) in sequence, then rebuilds
the shared FAISS index from all accumulated embeddings.

Usage:
    python -m minni.index_all              # Index everything (vault + wiki)
    python -m minni.index_all --vault-only # Index only the Obsidian vault
    python -m minni.index_all --wiki-only  # Index only wiki directories
    python -m minni.index_all --vault-ingest-all
                                            # Index all ~/.minni/*-vault wiki trees
    python -m minni.index_all --verbose    # Show per-file progress
"""

import argparse
import sys
import logging
from pathlib import Path
from typing import Dict

from minni.config import CANONICAL_SOVEREIGN_HOME, SovereignConfig, DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.indexer import VaultIndexer
from minni.wiki_indexer import WikiIndexer

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
    from minni.faiss_index import FAISSIndex
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
    """Return ~/.minni/*-vault dirs, excluding the bare ~/.minni/vault.

    The exclusion is correct and stays: this list feeds ``vault_ingest``, which
    indexes a vault into its OWN per-agent sidecar store and derives the owning
    agent from the ``<slug>-vault`` directory name. The bare ``vault`` has no
    slug, so ``vault_ingest`` refuses it as ``unknown_vault_slug`` regardless --
    and it is not per-agent anyway. It belongs to :class:`VaultIndexer` and the
    shared DB, which is what :func:`index_shared_vault` runs.

    Calling it "legacy" was the misleading part: it is the AFM writer's live
    output directory, and because nothing scheduled ran the shared indexer over
    it, every page the loop wrote sat unindexed. See index_shared_vault.
    """
    home = Path(minni_home or CANONICAL_SOVEREIGN_HOME).expanduser()
    if not home.is_dir():
        return []
    return sorted(
        path
        for path in home.glob("*-vault")
        if path.is_dir() and path.name != "vault"
    )


def index_shared_vault(config: SovereignConfig = None) -> Dict[str, Dict]:
    """Incrementally index the shared vault (``config.vault_path``) into the shared DB.

    ``discover_agent_vaults`` cannot cover this one (see its docstring), and
    ``index_all`` only ever ran by hand, so on a long-lived daemon the AFM
    loop's own output vault accumulated pages that no retrieval path could see.
    This is the scheduled counterpart -- same incremental VaultIndexer pass the
    CLI runs, callable from the daemon's vault-watch sweep.

    Keyed by vault path so the result merges with ``index_agent_vaults``. The
    ``deleted`` count is mirrored to ``pruned`` because that is the key the
    sweep's change detection (and its retrieval-cache invalidation) reads.
    """
    config = config or DEFAULT_CONFIG
    vault = str(config.vault_path)
    db = SovereignDB(config)
    try:
        stats = VaultIndexer(db, config).index_vault(verbose=False)
    finally:
        db.close()
    if "deleted" in stats:
        stats = {**stats, "pruned": stats["deleted"]}
    return {vault: stats}


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
    from minni.afm_passes.vault_ingest import run as run_vault_ingest

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
